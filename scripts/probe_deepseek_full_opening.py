#!/usr/bin/env python3
"""Run one isolated Chapter One opening through the real HTTP boundary.

The probe never touches the production port or campaign directory. It shares one
official DeepSeek client across every injected model role and writes only a
sanitized summary: public replies, tool names, timings, and provider metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from probe_deepseek_session_prep_json import provider_config, read_dotenv

from fu_gm.http_server import FUGMHttpService, make_server
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_client_bundle import TestLLMClientBundle
from fu_gm.testing.kariba_fixture import (
    KARIBA_INVITATION,
    seed_kariba_ready_campaign,
)


CAMPAIGN_ID = "probe-deepseek-opening"
SESSION_ID = "probe-opening-session"
CHANNEL_ID = "probe-opening-channel"


def request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    timeout: float = 240.0,
) -> tuple[int, dict[str, Any], int]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    headers = {"Content-Type": "application/json"} if body is not None else {}
    started = time.monotonic()
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        parsed = json.loads(raw) if raw.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("HTTP response is not a JSON object")
        return response.status, parsed, int((time.monotonic() - started) * 1000)
    finally:
        connection.close()


def snapshot_hash(root: Path) -> str:
    candidates = sorted(root.rglob("snapshot.json"))
    if not candidates:
        return ""
    digest = hashlib.sha256()
    for path in candidates:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def install_expressor_spies(expressor: Any) -> dict[str, int]:
    counts = {
        "render": 0,
        "render_agent_message": 0,
        "render_scene_moment": 0,
    }
    for name in tuple(counts):
        original = getattr(expressor, name, None)
        if not callable(original):
            continue

        def wrapper(
            *args: object,
            _name: str = name,
            _original: Any = original,
            **kwargs: object,
        ) -> Any:
            counts[_name] += 1
            return _original(*args, **kwargs)

        setattr(expressor, name, wrapper)
    return counts


def record_activity(
    host: str,
    port: int,
    *,
    message_id: str,
    activity_version: int,
) -> None:
    status, body, _elapsed = request_json(
        host,
        port,
        "POST",
        "/v1/message/activity",
        {
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "channel_id": CHANNEL_ID,
            "message_id": message_id,
            "activity_version": activity_version,
            "activity_token": f"probe-token-{activity_version}",
            "is_private": False,
        },
        timeout=10.0,
    )
    if status != 200 or not bool(body.get("tracked")):
        raise RuntimeError(f"message activity was not tracked: {status}")


def route_message(
    host: str,
    port: int,
    *,
    message_id: str,
    activity_version: int,
    message: str,
    reply_to_invitation: bool,
) -> tuple[int, dict[str, Any], int]:
    record_activity(
        host,
        port,
        message_id=message_id,
        activity_version=activity_version,
    )
    payload: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "channel_id": CHANNEL_ID,
        "speaker": "测试玩家甲",
        "speaker_id": "probe-player-one",
        "message": message,
        "message_id": message_id,
        "activity_version": activity_version,
        "activity_token": f"probe-token-{activity_version}",
        "is_private": False,
        "is_at_bot": not reply_to_invitation,
        "is_reply_to_bot": reply_to_invitation,
    }
    if reply_to_invitation:
        payload["quoted_message"] = {
            "message_id": "kariba-invitation",
            "sender_id": "gm-shiyou",
            "text": KARIBA_INVITATION,
            "source": "astrbot",
        }
    return request_json(
        host,
        port,
        "POST",
        "/v1/message/route",
        payload,
    )


def confirm_deliveries(
    host: str,
    port: int,
    response: dict[str, Any],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for envelope in list(response.get("reply_envelopes") or []):
        if not isinstance(envelope, dict):
            continue
        envelope_id = str(envelope.get("envelope_id") or "")
        if not envelope_id:
            continue
        status, body, elapsed_ms = request_json(
            host,
            port,
            "POST",
            "/v1/message/delivered",
            {
                "envelope_id": envelope_id,
                "campaign_id": CAMPAIGN_ID,
                "platform": "isolated-probe",
            },
            timeout=10.0,
        )
        results.append(
            {
                "status": status,
                "ok": bool(body.get("ok")),
                "already_confirmed": bool(body.get("already_confirmed")),
                "elapsed_ms": elapsed_ms,
            }
        )
    return results


def first_offset(
    events: list[dict[str, Any]],
    *,
    kind: str,
    phase: str = "",
) -> int | None:
    for event in events:
        if str(event.get("kind") or "") != kind:
            continue
        if phase and str(event.get("phase") or "") != phase:
            continue
        return int(event.get("offset_ms") or 0)
    return None


def difference(end: int | None, start: int | None) -> int | None:
    if end is None or start is None:
        return None
    return max(0, end - start)


def live_run_summary(
    host: str,
    port: int,
    *,
    message_id: str,
) -> dict[str, object]:
    query = urllib.parse.urlencode(
        {
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "channel_id": CHANNEL_ID,
            # message_id is intentionally omitted from public live-run views.
            # This isolated process has no private user data; request the full
            # view only so the probe can select its exact run, then persist a
            # strictly sanitized timing projection below.
            "include_private": "true",
            "limit": "10",
        }
    )
    status, body, query_ms = request_json(
        host,
        port,
        "GET",
        f"/v1/audit/live-runs?{query}",
        timeout=10.0,
    )
    if status != 200:
        raise RuntimeError(f"live-run query failed: {status}")
    run = next(
        (
            dict(item)
            for item in list(body.get("recent_runs") or [])
            if isinstance(item, dict)
            and str(item.get("message_id") or "") == message_id
        ),
        {},
    )
    events = [
        dict(item)
        for item in list(run.get("events") or [])
        if isinstance(item, dict)
    ]
    state_observed = first_offset(events, kind="state_observed")
    checking_freshness = first_offset(
        events,
        kind="coordinator_phase",
        phase="checking_freshness",
    )
    expression_start = first_offset(
        events,
        kind="coordinator_phase",
        phase="rendering_expression",
    )
    expression_end = first_offset(events, kind="expression_finished")
    provider_calls: list[dict[str, object]] = []
    for event in events:
        if str(event.get("kind") or "") != "provider_attempt_finished":
            continue
        details = dict(event.get("details") or {})
        provider_calls.append(
            {
                "model": str(details.get("model") or ""),
                "operation": str(details.get("operation") or "chat_completion"),
                "attempt": int(details.get("attempt") or 0),
                "ok": bool(details.get("ok")),
                "elapsed_ms": int(details.get("elapsed_ms") or 0),
                "error_category": str(details.get("error_category") or ""),
                "finish_reason": str(details.get("finish_reason") or ""),
                "response_chars": int(details.get("response_chars") or 0),
                "reasoning_chars": int(details.get("reasoning_chars") or 0),
            }
        )
    return {
        "query_ms": query_ms,
        "elapsed_ms": int(run.get("elapsed_ms") or 0),
        "status": str(run.get("status") or ""),
        "core_agent_ms": difference(checking_freshness, state_observed),
        "public_expression_ms": difference(expression_end, expression_start),
        "provider_calls": provider_calls,
    }


def response_summary(
    *,
    status: int,
    body: dict[str, Any],
    wall_ms: int,
    live_run: dict[str, object],
    deliveries: list[dict[str, object]],
) -> dict[str, object]:
    receipts = [
        {
            "tool_name": str(item.get("tool_name") or ""),
            "ok": bool(item.get("ok")),
            "state_changed": bool(item.get("state_changed")),
            "error_code": str(item.get("error_code") or ""),
            "lock_public_reply": bool(item.get("lock_public_reply")),
        }
        for item in list(body.get("tool_receipts") or [])
        if isinstance(item, dict)
    ]
    public_expression = dict(body.get("public_expression") or {})
    agent_loop = dict(body.get("agent_loop") or {})
    agent_trace: list[dict[str, object]] = []
    for raw_step in list(body.get("agent_trace") or []):
        if not isinstance(raw_step, dict):
            continue
        receipt = dict(raw_step.get("receipt") or {})
        grounding = [
            {
                "tool_name": str(item.get("tool_name") or ""),
                "valid": bool(item.get("valid")),
                "category": str(item.get("category") or ""),
                "unsupported_claims": [
                    str(claim or "")[:240]
                    for claim in list(item.get("unsupported_claims") or [])[:6]
                ],
            }
            for item in list(raw_step.get("tool_proposal_grounding") or [])
            if isinstance(item, dict)
        ]
        agent_trace.append(
            {
                "iteration": int(raw_step.get("iteration") or 0),
                "decision": str(raw_step.get("decision") or ""),
                "tool_name": str(raw_step.get("tool_name") or ""),
                "protocol_error": str(raw_step.get("protocol_error") or ""),
                "grounding": grounding,
                "receipt": {
                    "tool_name": str(receipt.get("tool_name") or ""),
                    "ok": bool(receipt.get("ok")),
                    "error_code": str(receipt.get("error_code") or ""),
                }
                if receipt
                else {},
            }
        )
    return {
        "http_status": status,
        "wall_ms": wall_ms,
        "route": str(body.get("route") or ""),
        "target": str(body.get("target") or ""),
        "send_reply": bool(body.get("send_reply")),
        "reply": str(body.get("reply") or ""),
        "agent_error": str(body.get("agent_error") or ""),
        "stale_discarded": bool(body.get("stale_discarded")),
        "public_expression": {
            "attempted": bool(public_expression.get("attempted")),
            "author": str(public_expression.get("author") or ""),
            "merged_into_core": bool(public_expression.get("merged_into_core")),
            "expression_mode": str(public_expression.get("expression_mode") or ""),
        },
        "agent_loop": {
            "elapsed_ms": int(agent_loop.get("elapsed_ms") or 0),
            "iterations": int(agent_loop.get("iteration") or 0),
            "terminal_reason": str(agent_loop.get("terminal_reason") or ""),
            "phase_durations_ms": dict(agent_loop.get("phase_durations_ms") or {}),
        },
        "agent_trace": agent_trace,
        "tool_receipts": receipts,
        "deliveries": deliveries,
        "live_run": live_run,
    }


def sanitized_client_calls(client: OpenAICompatibleClient) -> list[dict[str, object]]:
    allowed = (
        "model",
        "ok",
        "elapsed_ms",
        "response_format",
        "max_tokens",
        "reasoning_effort",
        "thinking_enabled",
        "operation",
        "attempt",
        "finish_reason",
        "response_chars",
        "reasoning_chars",
        "error_category",
        "usage",
    )
    return [
        {key: record[key] for key in allowed if key in record}
        for record in list(client.recent_calls)
    ]


def role_snapshot(service: FUGMHttpService, runtime: Any) -> dict[str, object]:
    tool_agent = service.gm_tool_agent
    requester = getattr(tool_agent, "_decision_requester", None)
    grounding = getattr(tool_agent, "reply_grounding_verifier", None)
    app = runtime.app
    return {
        "core": str(service.gm_agent_runtime.llm_model or ""),
        "tool": str(getattr(tool_agent, "model", "") or ""),
        "protocol_repair": str(getattr(requester, "repair_model", "") or ""),
        "reply_grounding": str(getattr(grounding, "model", "") or ""),
        "expressor": str(getattr(app.expressor, "model", "") or ""),
        "creative": str(getattr(app.scene_creative_writer, "model", "") or ""),
        "creative_audit": str(
            getattr(app.scene_creative_writer, "audit_model", "") or ""
        ),
        "npc_blueprint": str(
            getattr(app.npc_blueprint_designer, "model", "") or ""
        ),
        "npc_voice": str(getattr(app.npc_voice_renderer, "model", "") or ""),
        "summarizer": str(getattr(runtime.log_manager.summarizer, "model", "") or ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe an isolated all-DeepSeek Chapter One opening."
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=Path("artifacts"))
    return parser.parse_args()


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    config = provider_config(read_dotenv(args.dotenv))
    client = OpenAICompatibleClient(config)
    bundle = TestLLMClientBundle.shared(client, model=config.action_model)
    os.environ["FU_GM_DOTENV_PATH"] = "/dev/null"
    os.environ["FU_GM_TOOL_AGENT_TIMEOUT_SECONDS"] = "180"
    os.environ["FU_GM_CORE_GM_TIMEOUT_SECONDS"] = "180"
    os.environ["FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS"] = "45"
    os.environ["FU_GM_PUBLIC_EXPRESSION_MODE"] = "core"
    os.environ["FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED"] = "0"
    os.environ["FU_GM_DEEPSEEK_ROLEPLAY_MODE"] = "default"
    os.environ["FU_GM_NPC_VOICE_AUDIT_MODE"] = "off"
    os.environ["FU_GM_IMAGE_ENABLED"] = "0"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_root / f"deepseek_full_opening_probe_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    summary_path = output_dir / "summary.json"
    summary: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": config.chat_completions_url(),
        "expected_model": config.action_model,
        "thinking": "disabled",
        "production_port_touched": False,
    }
    exit_code = 1
    try:
        with tempfile.TemporaryDirectory(prefix="fu-gm-deepseek-opening-") as temp:
            data_root = Path(temp)
            service = FUGMHttpService(
                data_root=data_root,
                use_llm=True,
                rules_seed=0,
                public_expression_mode="core",
                test_llm_bundle=bundle,
            )
            runtime = seed_kariba_ready_campaign(
                service,
                campaign_id=CAMPAIGN_ID,
                session_id=SESSION_ID,
                channel_id=CHANNEL_ID,
                skip_map_render=True,
            )
            before_hash = snapshot_hash(data_root)
            expressor_calls = install_expressor_spies(runtime.app.expressor)
            summary["roles"] = role_snapshot(service, runtime)

            server = make_server("127.0.0.1", 0, service=service)
            host, port = server.server_address
            if int(port) == 8765:
                raise RuntimeError("isolated server unexpectedly selected production port")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            summary["isolated_port"] = int(port)
            try:
                health_status, health, health_ms = request_json(
                    str(host),
                    int(port),
                    "GET",
                    "/health",
                    timeout=10.0,
                )
                summary["health"] = {
                    "status": health_status,
                    "ok": bool(health.get("ok")),
                    "elapsed_ms": health_ms,
                    "public_expression_mode": str(
                        dict(health.get("runtime") or {}).get(
                            "public_expression_mode"
                        )
                        or ""
                    ),
                }

                opening_id = "probe-opening-consent-1"
                status, body, wall_ms = route_message(
                    str(host),
                    int(port),
                    message_id=opening_id,
                    activity_version=1,
                    message="嗯，进入第一章吧。",
                    reply_to_invitation=True,
                )
                opening_live = live_run_summary(
                    str(host),
                    int(port),
                    message_id=opening_id,
                )
                opening_deliveries = confirm_deliveries(
                    str(host),
                    int(port),
                    body,
                )
                opening = response_summary(
                    status=status,
                    body=body,
                    wall_ms=wall_ms,
                    live_run=opening_live,
                    deliveries=opening_deliveries,
                )
                summary["opening"] = opening

                if (
                    status == 200
                    and str(opening.get("route") or "")
                    != "gm_agent_message_transaction_rolled_back"
                    and not str(opening.get("agent_error") or "")
                ):
                    direct_id = "probe-opening-direct-2"
                    direct_status, direct_body, direct_wall_ms = route_message(
                        str(host),
                        int(port),
                        message_id=direct_id,
                        activity_version=2,
                        message=(
                            "时悠，请只用一句话确认第一章已经开始，"
                            "不要改变任何状态。"
                        ),
                        reply_to_invitation=False,
                    )
                    direct_live = live_run_summary(
                        str(host),
                        int(port),
                        message_id=direct_id,
                    )
                    direct_deliveries = confirm_deliveries(
                        str(host),
                        int(port),
                        direct_body,
                    )
                    summary["direct_core_reply"] = response_summary(
                        status=direct_status,
                        body=direct_body,
                        wall_ms=direct_wall_ms,
                        live_run=direct_live,
                        deliveries=direct_deliveries,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3.0)

            after_hash = snapshot_hash(data_root)
            gate = service.session_gates.get(
                CAMPAIGN_ID,
                CHANNEL_ID,
                SESSION_ID,
            )
            scene = runtime.app.scene_manager.current_scene
            summary["state"] = {
                "snapshot_changed": bool(after_hash and after_hash != before_hash),
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "gate_status": str(getattr(gate, "status", "") or ""),
                "scene_name": str(getattr(scene, "name", "") or ""),
                "scene_location": str(getattr(scene, "location", "") or ""),
                "session_active": bool(runtime.app.session_ledger.active),
            }
            summary["expressor_calls"] = dict(expressor_calls)
            summary["provider_calls"] = sanitized_client_calls(client)

            reloaded = FUGMHttpService(
                data_root=data_root,
                use_llm=False,
                rules_seed=0,
            )
            reloaded_runtime = reloaded._runtime(CAMPAIGN_ID)
            reloaded_gate = reloaded.session_gates.get(
                CAMPAIGN_ID,
                CHANNEL_ID,
                SESSION_ID,
            )
            reloaded_scene = reloaded_runtime.app.scene_manager.current_scene
            summary["reload"] = {
                "gate_status": str(
                    getattr(reloaded_gate, "status", "") or ""
                ),
                "scene_name": str(getattr(reloaded_scene, "name", "") or ""),
                "session_active": bool(
                    reloaded_runtime.app.session_ledger.active
                ),
            }

            roles = dict(summary.get("roles") or {})
            models_ok = bool(roles) and all(
                value == config.action_model for value in roles.values()
            )
            calls = list(summary.get("provider_calls") or [])
            calls_ok = bool(calls) and all(
                isinstance(call, dict)
                and call.get("model") == config.action_model
                and call.get("thinking_enabled") is False
                for call in calls
            )
            opening = dict(summary.get("opening") or {})
            direct = dict(summary.get("direct_core_reply") or {})
            opening_tools = {
                str(item.get("tool_name") or "")
                for item in list(opening.get("tool_receipts") or [])
                if isinstance(item, dict) and bool(item.get("ok"))
            }
            opening_expression = dict(opening.get("public_expression") or {})
            direct_expression = dict(direct.get("public_expression") or {})
            state = dict(summary.get("state") or {})
            reload_state = dict(summary.get("reload") or {})
            checks = {
                "all_roles_deepseek": models_ok,
                "all_provider_calls_deepseek_non_thinking": calls_ok,
                "opening_http_ok": opening.get("http_status") == 200,
                "opening_not_rolled_back": opening.get("route")
                != "gm_agent_message_transaction_rolled_back",
                "opening_agent_error_empty": not bool(opening.get("agent_error")),
                "opening_not_stale": not bool(opening.get("stale_discarded")),
                "start_session_succeeded": "start_session" in opening_tools,
                "start_scene_succeeded": "start_scene" in opening_tools,
                "opening_outer_expressor_skipped": not bool(
                    opening_expression.get("attempted")
                ),
                "direct_core_outer_expressor_skipped": bool(direct)
                and not bool(direct_expression.get("attempted"))
                and direct_expression.get("author") == "core_gm",
                "expressor_methods_unused": not any(expressor_calls.values()),
                "snapshot_changed": bool(state.get("snapshot_changed")),
                "adventure_gate_persisted": state.get("gate_status")
                == "adventure",
                "scene_started": bool(state.get("scene_name")),
                "reload_preserved_adventure": reload_state.get("gate_status")
                == "adventure",
                "reload_preserved_scene": bool(reload_state.get("scene_name")),
            }
            summary["checks"] = checks
            summary["passed"] = all(checks.values())
            exit_code = 0 if summary["passed"] else 1
    except Exception as exc:
        summary["passed"] = False
        summary["probe_error_type"] = type(exc).__name__
        summary["probe_error"] = " ".join(str(exc).split())[:500]
    finally:
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(summary_path, 0o600)
        close = getattr(getattr(client, "transport", None), "close", None)
        if callable(close):
            close()

    print(
        f"summary={summary_path} passed={bool(summary.get('passed'))}",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
