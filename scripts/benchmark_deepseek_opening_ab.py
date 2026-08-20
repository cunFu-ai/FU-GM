#!/usr/bin/env python3
"""Compare legacy and optimized Chapter One opening flows on real DeepSeek.

The benchmark is intentionally isolated from the deployed service: every arm
uses a temporary campaign root, a fresh client and an ephemeral loopback port.
It reports preparation work, the player's critical path and background work as
separate buckets so moving preparation earlier cannot masquerade as free work.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from probe_deepseek_full_opening import (
    CAMPAIGN_ID,
    CHANNEL_ID,
    SESSION_ID,
    confirm_deliveries,
    install_expressor_spies,
    live_run_summary,
    request_json,
    response_summary,
    role_snapshot,
    route_message,
    sanitized_client_calls,
    snapshot_hash,
)
from probe_deepseek_session_prep_json import provider_config, read_dotenv

from fu_gm.http_server import FUGMHttpService, make_server
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_client_bundle import TestLLMClientBundle
from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.testing.kariba_fixture import seed_kariba_ready_campaign


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated real-chain DeepSeek opening A/B pairs."
    )
    parser.add_argument("--pairs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--prewarm-timeout", type=float, default=90.0)
    return parser.parse_args()


def _production_snapshot() -> dict[str, object]:
    result: dict[str, object] = {"port": 8765, "reachable": False, "pid": ""}
    try:
        status, body, elapsed_ms = request_json(
            "127.0.0.1", 8765, "GET", "/health", timeout=3.0
        )
        result.update(
            {
                "reachable": status == 200,
                "http_status": status,
                "health_ok": bool(body.get("ok")),
                "elapsed_ms": elapsed_ms,
                "started_at": str(
                    body.get("started_at")
                    or dict(body.get("runtime") or {}).get("started_at")
                    or ""
                ),
            }
        )
    except Exception as exc:
        result["health_error"] = type(exc).__name__
    try:
        process = subprocess.run(
            ["lsof", "-nP", "-tiTCP:8765", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        result["pid"] = ",".join(
            line.strip() for line in process.stdout.splitlines() if line.strip()
        )
    except Exception as exc:
        result["pid_error"] = type(exc).__name__
    return result


def _usage_summary(calls: Iterable[dict[str, object]]) -> dict[str, object]:
    records = [dict(item) for item in calls]
    prompt = cached = missed = completion = model_ms = 0
    hit_calls = reported = retries = failures = 0
    for call in records:
        usage = dict(call.get("usage") or {})
        prompt += int(usage.get("prompt_tokens") or 0)
        cached += int(usage.get("cached_tokens") or 0)
        missed += int(usage.get("cache_miss_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        model_ms += int(call.get("elapsed_ms") or 0)
        if bool(usage.get("cache_usage_reported")):
            reported += 1
        if int(usage.get("cached_tokens") or 0) > 0:
            hit_calls += 1
        if int(call.get("attempt") or 1) > 1:
            retries += 1
        if not bool(call.get("ok")):
            failures += 1
    return {
        "call_count": len(records),
        "model_elapsed_ms": model_ms,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cache_miss_tokens": missed,
        "cache_token_hit_rate": round(cached / prompt, 6) if prompt else None,
        "cache_hit_calls": hit_calls,
        "cache_usage_reported_calls": reported,
        "retry_attempts": retries,
        "failed_attempts": failures,
        "operations": [str(item.get("operation") or "") for item in records],
    }


def _wait_background(
    runtime: Any,
    *,
    timeout_seconds: float = 120.0,
) -> dict[str, object]:
    designer = getattr(runtime.app, "npc_blueprint_designer", None)
    if designer is None:
        return {
            "remaining_wait_ms": 0,
            "measurement_scope": "wait_started_after_player_route",
            "jobs": [],
            "settled": True,
        }
    started = time.monotonic()
    jobs: list[dict[str, object]] = []
    error = ""
    try:
        jobs = [
            {
                "status": str(item.get("status") or ""),
                "reused": bool(item.get("reused")),
                "has_error": bool(str(item.get("error") or "").strip()),
            }
            for item in designer.wait_for_all(timeout=timeout_seconds)
            if isinstance(item, dict)
        ]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        jobs = []
        for job_id in list(getattr(designer, "_jobs", {}) or {}):
            try:
                item = dict(designer.poll(job_id))
                jobs.append(
                    {
                        "status": str(item.get("status") or ""),
                        "reused": bool(item.get("reused")),
                        "has_error": bool(
                            str(item.get("error") or "").strip()
                        ),
                    }
                )
            except Exception:
                pass
    statuses = [str(item.get("status") or "") for item in jobs]
    return {
        # The blueprint jobs may begin during start_scene.  This measures only
        # the tail still outstanding after the player route has completed; it
        # must not be reported as the jobs' end-to-end wall time.
        "remaining_wait_ms": int((time.monotonic() - started) * 1000),
        "measurement_scope": "wait_started_after_player_route",
        "jobs": jobs,
        "settled": all(status not in {"queued", "running"} for status in statuses),
        "failed_jobs": sum(status == "failed" for status in statuses),
        "error": error,
    }


def _is_background_call(call: dict[str, object]) -> bool:
    return str(call.get("operation") or "").startswith("npc_blueprint")


def _ordinary_fact_provider_proof(
    live_run: dict[str, object],
) -> dict[str, object]:
    """Prove the local reply fast path from provider-attempt telemetry.

    ``response_summary`` intentionally keeps only a sanitized agent trace and
    does not retain the private ``reply_grounding`` object.  The provider
    operations are a stronger boundary for this benchmark: a successful fact
    confirmation must have exactly one core decision call and no semantic
    grounding call of any kind.
    """

    calls = [
        dict(item)
        for item in list(dict(live_run or {}).get("provider_calls") or [])
        if isinstance(item, dict)
    ]
    operations = [str(item.get("operation") or "") for item in calls]
    core_operations = [
        operation
        for operation in operations
        if operation.startswith("gm_tool_agent.iteration_")
    ]
    grounding_operations = [
        operation for operation in operations if "grounding" in operation
    ]
    only_one_core_call = bool(
        len(calls) == 1
        and len(core_operations) == 1
        and operations == core_operations
    )
    provider_call_succeeded = bool(
        len(calls) == 1
        and bool(calls[0].get("ok"))
        and int(calls[0].get("response_chars") or 0) > 0
    )
    no_model_grounding = not grounding_operations
    return {
        "source": "live_run.provider_attempt_finished",
        "provider_call_count": len(calls),
        "operations": operations,
        "core_operations": core_operations,
        "grounding_operations": grounding_operations,
        "only_one_core_call": only_one_core_call,
        "no_model_grounding": no_model_grounding,
        "provider_call_succeeded": provider_call_succeeded,
        "proved": bool(
            only_one_core_call
            and no_model_grounding
            and provider_call_succeeded
        ),
    }


def _partition_opening_provider_calls(
    calls: list[dict[str, object]],
    *,
    route_start_index: int,
    route_end_index: int,
    background_end_index: int,
) -> dict[str, object]:
    """Separate opening-critical and NPC background provider attempts."""

    route_window = calls[route_start_index:route_end_index]
    settle_window = calls[route_end_index:background_end_index]
    route_background = [
        call for call in route_window if _is_background_call(call)
    ]
    critical = [
        call for call in route_window if not _is_background_call(call)
    ]
    settle_background = [
        call for call in settle_window if _is_background_call(call)
    ]
    unclassified_settle = [
        call for call in settle_window if not _is_background_call(call)
    ]
    return {
        "critical": critical,
        "background": [*route_background, *settle_background],
        "unclassified_settle": unclassified_settle,
        "route_partition_complete": (
            len(route_window) == len(critical) + len(route_background)
        ),
        "settle_only_background": not unclassified_settle,
    }


def _opening_cache_receipt(body: dict[str, Any]) -> dict[str, object]:
    for raw in list(body.get("tool_receipts") or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("tool_name") or "") not in {
            "start_session",
            "start_adventure",
        }:
            continue
        result = dict(raw.get("result") or {})
        cache = result.get("session_prep_cache")
        if isinstance(cache, dict):
            return dict(cache)
    return {}


def _reloaded_prefetch_fingerprint_evidence(
    service: FUGMHttpService,
    runtime: Any,
) -> dict[str, object]:
    """Compare the persisted envelope with freshly derived authority input."""

    with runtime.transaction_lock:
        envelope = (
            runtime.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        persisted = str(getattr(envelope, "fingerprint", "") or "").strip()
        current = str(
            service.adventure_opening_prefetcher._current_fingerprint_locked(
                runtime
            )
            or ""
        ).strip()
    return {
        # Fingerprints are non-secret digests. Persist only a short diagnostic
        # prefix and never serialize the private session contract itself.
        "persisted_prefix": persisted[:12],
        "current_prefix": current[:12],
        "persisted_present": bool(persisted),
        "current_present": bool(current),
        "matches": bool(persisted and current and persisted == current),
    }


def _authoritative_state(service: FUGMHttpService, runtime: Any) -> dict[str, object]:
    gate = service.session_gates.get(CAMPAIGN_ID, CHANNEL_ID, SESSION_ID)
    scene = runtime.app.scene_manager.current_scene
    characters = []
    for actor in runtime.app.character_manager.all():
        if "pc" not in list(getattr(actor, "traits", []) or []):
            continue
        characters.append(
            {
                "name": actor.name,
                "equipped_main_hand": actor.equipped_main_hand,
                "unavailable_equipment": sorted(actor.unavailable_equipment),
            }
        )
    return {
        "gate_status": str(getattr(gate, "status", "") or ""),
        "session_active": bool(runtime.app.session_ledger.active),
        "scene_name": str(getattr(scene, "name", "") or ""),
        "scene_location": str(getattr(scene, "location", "") or ""),
        "scene_participants": list(getattr(scene, "participants", []) or []),
        "characters": sorted(characters, key=lambda item: item["name"]),
    }


def _new_service(
    *,
    data_root: Path,
    bundle: TestLLMClientBundle,
    flow_mode: str,
) -> FUGMHttpService:
    return FUGMHttpService(
        data_root=data_root,
        use_llm=True,
        rules_seed=0,
        public_expression_mode="core",
        adventure_opening_flow_mode=flow_mode,
        test_llm_bundle=bundle,
    )


def _run_arm(
    *,
    arm: str,
    pair_index: int,
    config: Any,
    prewarm_timeout: float,
) -> dict[str, object]:
    flow_mode = "legacy" if arm == "A" else "optimized"
    prep_client = OpenAICompatibleClient(config)
    prep_bundle = TestLLMClientBundle.shared(
        prep_client,
        model=config.action_model,
    )
    route_client: OpenAICompatibleClient | None = None
    row: dict[str, object] = {
        "pair": pair_index,
        "arm": arm,
        "flow_mode": flow_mode,
        "model": config.action_model,
        "endpoint": config.chat_completions_url(),
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"fu-gm-opening-ab-{pair_index}-{arm.lower()}-"
        ) as temporary_root:
            data_root = Path(temporary_root)
            # Keep even accidental map rendering inside this arm's disposable
            # root.  The fixture currently skips rendering, but the guard must
            # be active before seeding and prefetch, not only before routing.
            os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(
                data_root / "nortantis_maps"
            )
            seeding_service = _new_service(
                data_root=data_root,
                bundle=prep_bundle,
                flow_mode=flow_mode,
            )
            seeded_runtime = seed_kariba_ready_campaign(
                seeding_service,
                campaign_id=CAMPAIGN_ID,
                session_id=SESSION_ID,
                channel_id=CHANNEL_ID,
                skip_map_render=True,
            )
            state_before_prep = _authoritative_state(
                seeding_service, seeded_runtime
            )
            prep_start_index = len(prep_client.recent_calls)
            prep_started = time.monotonic()
            if arm == "B":
                scheduled = seeding_service.adventure_opening_prefetcher.schedule(
                    campaign_id=CAMPAIGN_ID,
                    session_id=SESSION_ID,
                    channel_id=CHANNEL_ID,
                )
                prep_status = seeding_service.adventure_opening_prefetcher.wait(
                    CAMPAIGN_ID,
                    timeout_seconds=prewarm_timeout,
                )
            else:
                scheduled = {"status": "disabled", "flow_mode": "legacy"}
                prep_status = dict(scheduled)
            # Ensure no provider worker can outlive the preparation bucket or
            # race the reloaded route service on the same temporary root.
            seeding_service.adventure_opening_prefetcher._executor.shutdown(
                wait=True
            )
            if arm == "B":
                prep_status = (
                    seeding_service.adventure_opening_prefetcher.status(
                        CAMPAIGN_ID
                    )
                )
                if not (
                    prep_status.get("status") == "ready"
                    and prep_status.get("quality_status") == "model_reviewed"
                ):
                    raise RuntimeError(
                        "optimized prewarm did not produce a model-reviewed "
                        f"persistent envelope: {prep_status}"
                    )
            prep_wall_ms = int((time.monotonic() - prep_started) * 1000)
            prep_end_index = len(prep_client.recent_calls)
            prep_calls = sanitized_client_calls(prep_client)[
                prep_start_index:prep_end_index
            ]
            state_after_prep = _authoritative_state(
                seeding_service, seeded_runtime
            )
            pre_route_snapshot = snapshot_hash(data_root)

            # Reconstruct the whole service from disk for both arms.  For B,
            # this is the proof that the optimized route consumes a persisted
            # cache envelope rather than a pointer left in the old object.
            route_client = OpenAICompatibleClient(config)
            route_bundle = TestLLMClientBundle.shared(
                route_client,
                model=config.action_model,
            )
            service = _new_service(
                data_root=data_root,
                bundle=route_bundle,
                flow_mode=flow_mode,
            )
            runtime = service._runtime(CAMPAIGN_ID)
            runtime.app.ensure_world_map_for_adventure = lambda **_kwargs: {
                "status": "existing",
                "reason": "isolated A/B fixture",
            }
            expressor_calls = install_expressor_spies(runtime.app.expressor)
            row["roles"] = role_snapshot(service, runtime)
            if arm == "B":
                fingerprint_evidence = _reloaded_prefetch_fingerprint_evidence(
                    service,
                    runtime,
                )
                row["route_prefetch_fingerprint"] = fingerprint_evidence
                if not bool(fingerprint_evidence.get("matches")):
                    raise RuntimeError(
                        "reloaded prefetch fingerprint mismatch before route: "
                        f"persisted={fingerprint_evidence.get('persisted_prefix')} "
                        f"current={fingerprint_evidence.get('current_prefix')}"
                    )
            else:
                row["route_prefetch_fingerprint"] = {
                    "status": "not_applicable",
                    "arm": "legacy",
                }
            row["prep_phase"] = {
                "scheduled": scheduled,
                "status": prep_status,
                "wall_ms": prep_wall_ms,
                "provider": _usage_summary(prep_calls),
                "provider_calls": prep_calls,
                "authoritative_state_unchanged": (
                    state_before_prep == state_after_prep
                ),
                "state_before": state_before_prep,
                "state_after": state_after_prep,
            }

            server = make_server("127.0.0.1", 0, service=service)
            host, port = server.server_address
            if int(port) == 8765:
                raise RuntimeError("ephemeral server selected production port")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            row["isolated_port"] = int(port)
            background_status: dict[str, object] = {
                "remaining_wait_ms": 0,
                "measurement_scope": "wait_started_after_player_route",
                "jobs": [],
                "settled": True,
            }
            background_end_index = 0
            fast_fact_start_index = 0
            fast_fact_end_index = 0
            try:
                health_status, health, health_ms = request_json(
                    str(host), int(port), "GET", "/health", timeout=10.0
                )
                row["health"] = {
                    "status": health_status,
                    "ok": bool(health.get("ok")),
                    "elapsed_ms": health_ms,
                    "flow_mode": str(
                        dict(health.get("runtime") or {}).get(
                            "adventure_opening_flow_mode"
                        )
                        or ""
                    ),
                }
                route_start_index = len(route_client.recent_calls)
                message_id = f"opening-ab-{pair_index}-{arm.lower()}"
                status, body, wall_ms = route_message(
                    str(host),
                    int(port),
                    message_id=message_id,
                    activity_version=1,
                    message="嗯，进入第一章吧。",
                    reply_to_invitation=True,
                )
                route_end_index = len(route_client.recent_calls)
                live = live_run_summary(
                    str(host), int(port), message_id=message_id
                )
                deliveries = confirm_deliveries(
                    str(host), int(port), body
                )
                public = response_summary(
                    status=status,
                    body=body,
                    wall_ms=wall_ms,
                    live_run=live,
                    deliveries=deliveries,
                )
                for step in list(public.get("agent_trace") or []):
                    if not isinstance(step, dict):
                        continue
                    for review in list(step.get("grounding") or []):
                        if not isinstance(review, dict):
                            continue
                        claims = list(review.pop("unsupported_claims", []) or [])
                        review["unsupported_claim_count"] = len(claims)
                route_calls = sanitized_client_calls(route_client)[
                    route_start_index:route_end_index
                ]
                row["player_critical_path"] = {
                    **public,
                    "application_cache": _opening_cache_receipt(body),
                    "provider": _usage_summary(route_calls),
                    "provider_calls": route_calls,
                }

                # Let NPC blueprint jobs settle before measuring the ordinary
                # fact-confirmation path.  Otherwise shared-client completion
                # order could make a background call look like foreground
                # grounding latency.
                background_status = _wait_background(runtime)
                background_end_index = len(route_client.recent_calls)
                fast_state_before = _authoritative_state(service, runtime)
                fast_fact_start_index = background_end_index
                fast_message_id = f"opening-ab-{pair_index}-{arm.lower()}-fact"
                fast_status, fast_body, fast_wall_ms = route_message(
                    str(host),
                    int(port),
                    message_id=fast_message_id,
                    activity_version=2,
                    message=(
                        "时悠，请只用一句话确认第一章已经开始，"
                        "不要改变任何状态。"
                    ),
                    reply_to_invitation=False,
                )
                fast_fact_end_index = len(route_client.recent_calls)
                fast_live = live_run_summary(
                    str(host), int(port), message_id=fast_message_id
                )
                fast_deliveries = confirm_deliveries(
                    str(host), int(port), fast_body
                )
                fast_public = response_summary(
                    status=fast_status,
                    body=fast_body,
                    wall_ms=fast_wall_ms,
                    live_run=fast_live,
                    deliveries=fast_deliveries,
                )
                fast_calls = sanitized_client_calls(route_client)[
                    fast_fact_start_index:fast_fact_end_index
                ]
                row["ordinary_fast_fact"] = {
                    **fast_public,
                    "provider": _usage_summary(fast_calls),
                    "provider_calls": fast_calls,
                    "local_grounding_provider_proof": (
                        _ordinary_fact_provider_proof(fast_live)
                    ),
                    "authoritative_state_unchanged": (
                        fast_state_before
                        == _authoritative_state(service, runtime)
                    ),
                }
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3.0)

            route_all_calls = sanitized_client_calls(route_client)
            opening_partition = _partition_opening_provider_calls(
                route_all_calls,
                route_start_index=route_start_index,
                route_end_index=route_end_index,
                background_end_index=background_end_index,
            )
            route_calls = list(opening_partition["critical"])
            background_calls = list(opening_partition["background"])
            fast_fact_calls = route_all_calls[
                fast_fact_start_index:fast_fact_end_index
            ]
            critical_path = dict(row.get("player_critical_path") or {})
            critical_provider = _usage_summary(route_calls)
            critical_provider["live_run_attempt_count"] = len(
                list(
                    dict(critical_path.get("live_run") or {}).get(
                        "provider_calls"
                    )
                    or []
                )
            )
            critical_path["provider"] = critical_provider
            critical_path["provider_calls"] = route_calls
            row["player_critical_path"] = critical_path
            post_route_snapshot = snapshot_hash(data_root)
            state = _authoritative_state(service, runtime)
            reloaded = FUGMHttpService(
                data_root=data_root,
                use_llm=False,
                rules_seed=0,
                adventure_opening_flow_mode=flow_mode,
            )
            reloaded_runtime = reloaded._runtime(CAMPAIGN_ID)
            reload_state = _authoritative_state(reloaded, reloaded_runtime)
            row["background"] = {
                **background_status,
                "provider": _usage_summary(background_calls),
                "provider_calls": background_calls,
                "slice_evidence": {
                    "route_partition_complete": bool(
                        opening_partition["route_partition_complete"]
                    ),
                    "settle_only_background": bool(
                        opening_partition["settle_only_background"]
                    ),
                    "unclassified_settle_operations": [
                        str(item.get("operation") or "")
                        for item in list(
                            opening_partition["unclassified_settle"]
                        )
                    ],
                },
            }
            row["total_work"] = {
                # This remains the total work required to establish the
                # opening.  The subsequent ordinary fact probe is reported in
                # its own phase and intentionally excluded here.
                "provider": _usage_summary(
                    [*prep_calls, *route_all_calls[:background_end_index]]
                ),
                "provider_calls": [
                    *prep_calls,
                    *route_all_calls[:background_end_index],
                ],
                "telemetry_complete": (
                    int(prep_client.total_calls) == len(prep_calls)
                    and int(route_client.total_calls) == len(route_all_calls)
                ),
            }
            row["state"] = {
                "snapshot_changed": bool(
                    post_route_snapshot
                    and post_route_snapshot != pre_route_snapshot
                ),
                "after_opening": state,
                "after_reload": reload_state,
                "reload_matches": state == reload_state,
            }
            row["expressor_calls"] = dict(expressor_calls)

            path = dict(row.get("player_critical_path") or {})
            public_reply = str(path.get("reply") or "").strip()
            delivery_records = [
                item
                for item in list(path.get("deliveries") or [])
                if isinstance(item, dict)
            ]
            successful_tools = {
                str(item.get("tool_name") or "")
                for item in list(path.get("tool_receipts") or [])
                if isinstance(item, dict) and bool(item.get("ok"))
            }
            provider_calls = list(
                dict(row.get("total_work") or {}).get("provider_calls") or []
            )
            all_measured_provider_calls = [
                *provider_calls,
                *fast_fact_calls,
            ]
            roles = dict(row.get("roles") or {})
            fast_path = dict(row.get("ordinary_fast_fact") or {})
            fast_deliveries = [
                item
                for item in list(fast_path.get("deliveries") or [])
                if isinstance(item, dict)
            ]
            fast_grounding_proof = dict(
                fast_path.get("local_grounding_provider_proof") or {}
            )
            fast_live_calls = [
                dict(item)
                for item in list(
                    dict(fast_path.get("live_run") or {}).get(
                        "provider_calls"
                    )
                    or []
                )
                if isinstance(item, dict)
            ]
            fast_client_operations = [
                str(item.get("operation") or "") for item in fast_fact_calls
            ]
            fast_live_operations = [
                str(item.get("operation") or "") for item in fast_live_calls
            ]
            checks = {
                "all_roles_expected_model": bool(roles)
                and all(value == config.action_model for value in roles.values()),
                "all_calls_non_thinking": bool(all_measured_provider_calls)
                and all(
                    call.get("model") == config.action_model
                    and call.get("thinking_enabled") is False
                    and int(call.get("reasoning_chars") or 0) == 0
                    for call in all_measured_provider_calls
                    if isinstance(call, dict)
                ),
                "no_failed_provider_attempt": all(
                    bool(call.get("ok"))
                    and int(call.get("response_chars") or 0) > 0
                    for call in all_measured_provider_calls
                    if isinstance(call, dict)
                ),
                "http_ok": path.get("http_status") == 200,
                "reply_sent": bool(path.get("send_reply"))
                and bool(public_reply),
                "delivery_confirmed": bool(delivery_records)
                and all(
                    int(item.get("status") or 0) == 200
                    and bool(item.get("ok"))
                    for item in delivery_records
                ),
                "player_agency_preserved": not bool(
                    SceneMomentPolicy.player_agency_violation(public_reply)
                ),
                "open_player_handoff": public_reply.endswith(("？", "?")),
                "no_backstage_formula": not (
                    SceneMomentPolicy.looks_like_backstage_formula(public_reply)
                ),
                "not_rolled_back": path.get("route")
                != "gm_agent_message_transaction_rolled_back",
                "agent_error_empty": not bool(path.get("agent_error")),
                "not_stale": not bool(path.get("stale_discarded")),
                "outer_expressor_unused": not any(expressor_calls.values()),
                "state_committed": state.get("gate_status") == "adventure"
                and bool(state.get("session_active"))
                and bool(state.get("scene_name")),
                "state_persisted": bool(dict(row.get("state") or {}).get("reload_matches")),
                "arm_tool_contract": (
                    {"start_session", "start_scene"}.issubset(successful_tools)
                    if arm == "A"
                    else "start_adventure" in successful_tools
                ),
                "prep_did_not_start_adventure": (
                    state_before_prep == state_after_prep
                ),
                "persistent_cache_consumed": (
                    True
                    if arm == "A"
                    else (
                        dict(path.get("application_cache") or {}).get("status")
                        == "persistent_hit"
                        and bool(
                            dict(path.get("application_cache") or {}).get(
                                "consumed"
                            )
                        )
                    )
                ),
                "background_jobs_settled": bool(
                    dict(row.get("background") or {}).get("settled")
                )
                and not int(
                    dict(row.get("background") or {}).get("failed_jobs") or 0
                )
                and not bool(
                    dict(row.get("background") or {}).get("error")
                ),
                "provider_telemetry_complete": bool(
                    dict(row.get("total_work") or {}).get(
                        "telemetry_complete"
                    )
                ),
                "critical_provider_matches_live_run": int(
                    dict(path.get("provider") or {}).get("call_count") or 0
                )
                == int(
                    dict(path.get("provider") or {}).get(
                        "live_run_attempt_count"
                    )
                    or 0
                ),
                "ordinary_fact_http_ok": fast_path.get("http_status") == 200,
                "ordinary_fact_exact_reply": "".join(
                    str(fast_path.get("reply") or "").split()
                ).rstrip("。！？!?")
                == "第一章已经开始了",
                "ordinary_fact_delivery_confirmed": bool(fast_deliveries)
                and all(
                    int(item.get("status") or 0) == 200
                    and bool(item.get("ok"))
                    for item in fast_deliveries
                ),
                "ordinary_fact_local_grounding": bool(
                    fast_grounding_proof.get("proved")
                ),
                "ordinary_fact_only_core_provider_operation": bool(
                    fast_grounding_proof.get("only_one_core_call")
                ),
                "ordinary_fact_no_model_grounding_operation": bool(
                    fast_grounding_proof.get("no_model_grounding")
                ),
                "ordinary_fact_one_provider_call": int(
                    fast_grounding_proof.get("provider_call_count") or 0
                )
                == 1,
                "ordinary_fact_client_matches_live_run": (
                    fast_client_operations == fast_live_operations
                ),
                "ordinary_fact_state_unchanged": bool(
                    fast_path.get("authoritative_state_unchanged")
                ),
                "critical_path_excludes_npc_background": not any(
                    _is_background_call(item) for item in route_calls
                ),
                "background_slice_contains_only_npc_calls": all(
                    _is_background_call(item) for item in background_calls
                ),
                "background_provider_slice_complete": bool(
                    opening_partition["route_partition_complete"]
                )
                and bool(opening_partition["settle_only_background"]),
                "ordinary_fact_excludes_npc_background": not any(
                    _is_background_call(item) for item in fast_fact_calls
                ),
            }
            row["checks"] = checks
            provider_clean = bool(checks["no_failed_provider_attempt"])
            optimization_applied = bool(
                checks["persistent_cache_consumed"]
                and checks["arm_tool_contract"]
            )
            functional_checks = {
                key: value
                for key, value in checks.items()
                if key
                not in {
                    "no_failed_provider_attempt",
                    "persistent_cache_consumed",
                }
            }
            row["provider_clean"] = provider_clean
            row["optimization_applied"] = optimization_applied
            row["functional_passed"] = all(functional_checks.values())
            row["passed"] = bool(
                row["functional_passed"] and optimization_applied
            )
    except Exception as exc:
        row["passed"] = False
        row["error_type"] = type(exc).__name__
        row["error"] = " ".join(str(exc).split())[:800]
    finally:
        for active_client in (prep_client, route_client):
            close = getattr(
                getattr(active_client, "transport", None),
                "close",
                None,
            )
            if callable(close):
                close()
    return row


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return round(float(ordered[index]), 3)


def _arm_summary(rows: list[dict[str, object]], arm: str) -> dict[str, object]:
    selected = [row for row in rows if row.get("arm") == arm]
    successful = [
        row
        for row in selected
        if bool(row.get("functional_passed"))
        and bool(row.get("optimization_applied"))
    ]
    clean = [row for row in successful if bool(row.get("provider_clean"))]
    walls = [
        float(dict(row.get("player_critical_path") or {}).get("wall_ms") or 0)
        for row in successful
    ]
    calls = [
        float(
            dict(
                dict(row.get("player_critical_path") or {}).get("provider") or {}
            ).get("call_count")
            or 0
        )
        for row in successful
    ]
    prep_walls = [
        float(dict(row.get("prep_phase") or {}).get("wall_ms") or 0)
        for row in successful
    ]
    clean_walls = [
        float(dict(row.get("player_critical_path") or {}).get("wall_ms") or 0)
        for row in clean
    ]

    def phase_totals(phase: str) -> dict[str, object]:
        providers = [
            dict(dict(row.get(phase) or {}).get("provider") or {})
            for row in successful
        ]
        prompt = sum(int(item.get("prompt_tokens") or 0) for item in providers)
        cached = sum(int(item.get("cached_tokens") or 0) for item in providers)
        return {
            "provider_attempts": sum(
                int(item.get("call_count") or 0) for item in providers
            ),
            "model_elapsed_ms": sum(
                int(item.get("model_elapsed_ms") or 0) for item in providers
            ),
            "prompt_tokens": prompt,
            "completion_tokens": sum(
                int(item.get("completion_tokens") or 0) for item in providers
            ),
            "cached_tokens": cached,
            "cache_miss_tokens": sum(
                int(item.get("cache_miss_tokens") or 0) for item in providers
            ),
            "cache_token_hit_rate": (
                round(cached / prompt, 6) if prompt else None
            ),
            "failed_attempts": sum(
                int(item.get("failed_attempts") or 0) for item in providers
            ),
        }
    return {
        "runs": len(selected),
        "functional_successes": len(successful),
        "provider_clean_successes": len(clean),
        "critical_wall_ms": {
            "min": min(walls) if walls else None,
            "median": round(statistics.median(walls), 3) if walls else None,
            "p90": _percentile(walls, 0.9) if len(walls) >= 5 else None,
            "max": max(walls) if walls else None,
        },
        "critical_provider_calls_median": (
            round(statistics.median(calls), 3) if calls else None
        ),
        "prep_wall_ms_median": (
            round(statistics.median(prep_walls), 3) if prep_walls else None
        ),
        "clean_only_critical_wall_ms_median": (
            round(statistics.median(clean_walls), 3) if clean_walls else None
        ),
        "phase_totals": {
            phase: phase_totals(phase)
            for phase in (
                "prep_phase",
                "player_critical_path",
                "background",
                "ordinary_fast_fact",
                "total_work",
            )
        },
    }


def _paired_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    pairs: dict[int, dict[str, dict[str, object]]] = {}
    for row in rows:
        pairs.setdefault(int(row.get("pair") or 0), {})[str(row.get("arm"))] = row
    deltas: list[dict[str, object]] = []
    for pair_index, arms in sorted(pairs.items()):
        if "A" not in arms or "B" not in arms:
            continue
        a = arms["A"]
        b = arms["B"]
        if not (
            bool(a.get("functional_passed"))
            and bool(a.get("optimization_applied"))
            and bool(b.get("functional_passed"))
            and bool(b.get("optimization_applied"))
        ):
            continue
        a_path = dict(a.get("player_critical_path") or {})
        b_path = dict(b.get("player_critical_path") or {})
        a_calls = dict(a_path.get("provider") or {})
        b_calls = dict(b_path.get("provider") or {})
        a_wall = int(a_path.get("wall_ms") or 0)
        b_wall = int(b_path.get("wall_ms") or 0)
        deltas.append(
            {
                "pair": pair_index,
                "critical_wall_ms_b_minus_a": b_wall - a_wall,
                "critical_speedup_fraction": (
                    round((a_wall - b_wall) / a_wall, 6) if a_wall else None
                ),
                "critical_calls_b_minus_a": int(
                    b_calls.get("call_count") or 0
                )
                - int(a_calls.get("call_count") or 0),
            }
        )
    wall_deltas = [
        float(item["critical_wall_ms_b_minus_a"]) for item in deltas
    ]
    speedups = [
        float(item["critical_speedup_fraction"])
        for item in deltas
        if item.get("critical_speedup_fraction") is not None
    ]
    return {
        "complete_successful_pairs": len(deltas),
        "rows": deltas,
        "median_wall_ms_b_minus_a": (
            round(statistics.median(wall_deltas), 3) if wall_deltas else None
        ),
        "median_speedup_fraction": (
            round(statistics.median(speedups), 6) if speedups else None
        ),
    }


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.pairs < 1:
        raise ValueError("--pairs must be at least 1")
    config = provider_config(read_dotenv(args.dotenv))
    os.environ.update(
        {
            "FU_GM_DOTENV_PATH": "/dev/null",
            "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS": "180",
            "FU_GM_CORE_GM_TIMEOUT_SECONDS": "180",
            "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": "45",
            "FU_GM_PUBLIC_EXPRESSION_MODE": "core",
            "FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED": "0",
            "FU_GM_DEEPSEEK_ROLEPLAY_MODE": "default",
            "FU_GM_NPC_VOICE_AUDIT_MODE": "off",
            "FU_GM_NPC_BLUEPRINT_MAX_WORKERS": "1",
            "FU_GM_NPC_BLUEPRINT_BACKGROUND_DEFER_SECONDS": "20",
            "FU_GM_IMAGE_ENABLED": "0",
        }
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_root / f"deepseek_opening_ab_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    runs_path = output_dir / "runs.jsonl"
    summary_path = output_dir / "summary.json"
    production_before = _production_snapshot()

    rng = random.Random(args.seed)
    pair_orders: list[list[str]] = []
    reverse_first = rng.random() < 0.5
    for pair_index in range(1, args.pairs + 1):
        order = ["A", "B"]
        if reverse_first == (pair_index % 2 == 1):
            order.reverse()
        pair_orders.append(order)

    rows: list[dict[str, object]] = []
    for pair_index, order in enumerate(pair_orders, start=1):
        for arm in order:
            row = _run_arm(
                arm=arm,
                pair_index=pair_index,
                config=config,
                prewarm_timeout=args.prewarm_timeout,
            )
            rows.append(row)
            with runs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.chmod(runs_path, 0o600)
            print(
                "pair={} arm={} passed={} critical_ms={} prep_ms={}".format(
                    pair_index,
                    arm,
                    bool(row.get("passed")),
                    dict(row.get("player_critical_path") or {}).get("wall_ms"),
                    dict(row.get("prep_phase") or {}).get("wall_ms"),
                ),
                flush=True,
            )

    production_after = _production_snapshot()
    production_unchanged = (
        production_before.get("pid") == production_after.get("pid")
        and production_before.get("reachable") == production_after.get("reachable")
        and production_before.get("health_ok") == production_after.get("health_ok")
        and production_before.get("started_at") == production_after.get("started_at")
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs": args.pairs,
        "sample_kind": "smoke" if args.pairs == 1 else "balanced_small_sample",
        "seed": args.seed,
        "pair_orders": pair_orders,
        "model": config.action_model,
        "endpoint": config.chat_completions_url(),
        "thinking": "disabled",
        "arm_definition": {
            "A": "legacy foreground preparation plus start_session/start_scene",
            "B": "persisted invitation-time prefetch plus atomic start_adventure",
        },
        "production_before": production_before,
        "production_after": production_after,
        "production_pid_and_reachability_unchanged": production_unchanged,
        "arms": {
            "A": _arm_summary(rows, "A"),
            "B": _arm_summary(rows, "B"),
        },
        "paired": _paired_summary(rows),
        "all_runs_passed": all(bool(row.get("passed")) for row in rows),
    }
    summary["passed"] = bool(
        summary["all_runs_passed"] and production_unchanged
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(summary_path, 0o600)

    forbidden = ("Bearer ", "Authorization:", "sk-")
    artifact_text = runs_path.read_text(encoding="utf-8") + summary_path.read_text(
        encoding="utf-8"
    )
    if any(marker in artifact_text for marker in forbidden):
        raise RuntimeError("artifact secret scan failed")
    print(f"summary={summary_path} passed={summary['passed']}", flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
