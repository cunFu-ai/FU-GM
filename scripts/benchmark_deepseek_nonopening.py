#!/usr/bin/env python3
"""Run an isolated real-DeepSeek smoke benchmark after Chapter One opens.

The script deliberately uses the normal HTTP message boundary.  It prepares
and opens a disposable Kariba campaign, waits for opening NPC work to settle,
then measures ordinary facts, a terminal public rule lookup, check declaration
and the player's explicit roll confirmation as separate scenarios.  Optional
NPC dialogue and session close probes run only when the live authoritative
state provides the required anchor.

No production mutation endpoint is used.  Port 8765 is observed only through
the same read-only health/PID sentinel as the opening A/B benchmark.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from benchmark_deepseek_opening_ab import (
    _authoritative_state,
    _ordinary_fact_provider_proof,
    _production_snapshot,
    _usage_summary,
    _wait_background,
)
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
from fu_gm.testing.kariba_fixture import seed_kariba_ready_campaign


EXPECTED_RULE_TOOL = "get_rule_reference"
EXPECTED_CHECK_TOOL = "declare_check_action"
CORE_OPERATION_PREFIX = "gm_tool_agent.iteration_"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated real-chain DeepSeek non-opening smoke benchmark."
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--prewarm-timeout", type=float, default=120.0)
    parser.add_argument("--background-timeout", type=float, default=120.0)
    parser.add_argument(
        "--capability-routing-mode",
        choices=("baseline", "shadow", "intent"),
        default=os.environ.get("FU_GM_CAPABILITY_ROUTING_MODE", "shadow"),
    )
    parser.add_argument(
        "--state-context-mode",
        choices=("full", "summary_delta"),
        default=os.environ.get("FU_GM_STATE_CONTEXT_MODE", "full"),
    )
    parser.add_argument(
        "--skip-npc-dialogue",
        action="store_true",
        help="Do not run the optional anchored NPC-dialogue scenario.",
    )
    parser.add_argument(
        "--skip-session-close",
        action="store_true",
        help="Do not run the optional session-close scenario.",
    )
    return parser.parse_args()


def _production_unchanged(
    before: dict[str, object],
    after: dict[str, object],
) -> bool:
    return all(
        before.get(key) == after.get(key)
        for key in ("pid", "reachable", "health_ok", "started_at")
    )


def _pending_windows(runtime: Any) -> list[dict[str, object]]:
    manager = runtime.app.interceptor.decision_window_manager
    return [
        {
            "kind": str(getattr(window, "kind", "") or ""),
            "owner": str(getattr(window, "owner", "") or ""),
            "blocking": bool(getattr(window, "blocking", False)),
        }
        for window in manager.pending()
    ]


def _state_probe(
    service: FUGMHttpService,
    runtime: Any,
) -> dict[str, object]:
    state = _authoritative_state(service, runtime)
    state.update(
        {
            "state_version": int(getattr(runtime, "state_version", 0) or 0),
            "pending_windows": _pending_windows(runtime),
            "session_settled": bool(runtime.app.session_ledger.settled),
        }
    )
    return state


def _gameplay_state_projection(state: dict[str, object]) -> dict[str, object]:
    """Exclude persistence/activity counters from read-only state checks."""

    return {
        key: state.get(key)
        for key in (
            "gate_status",
            "session_active",
            "scene_name",
            "scene_location",
            "scene_participants",
            "characters",
            "pending_windows",
            "session_settled",
        )
    }


def _new_optimized_service(
    *,
    data_root: Path,
    bundle: TestLLMClientBundle,
    rules_seed: int,
    capability_routing_mode: str,
    state_context_mode: str,
) -> FUGMHttpService:
    return FUGMHttpService(
        data_root=data_root,
        use_llm=True,
        rules_seed=rules_seed,
        public_expression_mode="core",
        adventure_opening_flow_mode="optimized",
        capability_routing_mode=capability_routing_mode,
        state_context_mode=state_context_mode,
        test_llm_bundle=bundle,
    )


def _authoritative_npc_anchor(runtime: Any) -> str:
    """Return only a non-PC who is actually present in the current scene."""

    scene = runtime.app.scene_manager.current_scene
    if scene is None:
        return ""
    pc_names = {
        str(character.name)
        for character in runtime.app.character_manager.all()
        if "pc" in list(getattr(character, "traits", []) or [])
    }
    for participant in list(getattr(scene, "participants", []) or []):
        name = str(participant or "").strip()
        if name and name not in pc_names:
            return name
    return ""


def _receipt_summaries(body: dict[str, Any]) -> list[dict[str, object]]:
    """Keep proof flags and result shape without serializing private payloads."""

    rows: list[dict[str, object]] = []
    for raw in list(body.get("tool_receipts") or []):
        if not isinstance(raw, dict):
            continue
        result = raw.get("result")
        result = result if isinstance(result, dict) else {}
        rows.append(
            {
                "tool_name": str(raw.get("tool_name") or ""),
                "ok": bool(raw.get("ok")),
                "state_changed": bool(raw.get("state_changed")),
                "error_code": str(raw.get("error_code") or ""),
                "lock_public_reply": bool(raw.get("lock_public_reply")),
                "terminal_public_result": (
                    result.get("terminal_public_result") is True
                ),
                "result_keys": sorted(str(key) for key in result),
            }
        )
    return rows


def _call_key(call: dict[str, object]) -> tuple[str, int, bool, int]:
    return (
        str(call.get("operation") or "chat_completion"),
        int(call.get("attempt") or 1),
        bool(call.get("ok")),
        int(call.get("elapsed_ms") or 0),
    )


def _align_client_calls_to_live_run(
    client_calls: Iterable[dict[str, object]],
    live_calls: Iterable[dict[str, object]],
) -> dict[str, object]:
    """Partition a shared-client time slice using exact live-run telemetry.

    Background workers share the injected DeepSeek client.  Calls can finish
    inside an HTTP wall-time slice without belonging to that message's context.
    Matching the live-run operation/attempt/result/latency tuple prevents those
    calls from being silently charged to the foreground scenario.
    """

    remaining = [dict(item) for item in client_calls]
    live = [dict(item) for item in live_calls]
    aligned: list[dict[str, object]] = []
    unmatched_live: list[dict[str, object]] = []
    for expected in live:
        key = _call_key(expected)
        match_index = next(
            (index for index, item in enumerate(remaining) if _call_key(item) == key),
            None,
        )
        if match_index is None:
            unmatched_live.append(expected)
            continue
        aligned.append(remaining.pop(match_index))
    return {
        "aligned": aligned,
        "background_or_unclassified": remaining,
        "unmatched_live": unmatched_live,
        "complete": not remaining and not unmatched_live,
        "aligned_operations": [
            str(item.get("operation") or "") for item in aligned
        ],
        "live_operations": [str(item.get("operation") or "") for item in live],
    }


def _successful_receipt(
    receipts: Iterable[dict[str, object]],
    tool_name: str,
) -> dict[str, object]:
    return next(
        (
            dict(item)
            for item in receipts
            if str(item.get("tool_name") or "") == tool_name
            and bool(item.get("ok"))
        ),
        {},
    )


def _delivery_confirmed(deliveries: Iterable[dict[str, object]]) -> bool:
    rows = [dict(item) for item in deliveries]
    return bool(rows) and all(
        int(item.get("status") or 0) == 200 and bool(item.get("ok"))
        for item in rows
    )


def _drop_private_grounding_text(public: dict[str, object]) -> None:
    """Retain grounding counts/categories, never claim text from private trace."""

    for step in list(public.get("agent_trace") or []):
        if not isinstance(step, dict):
            continue
        for review in list(step.get("grounding") or []):
            if not isinstance(review, dict):
                continue
            claims = list(review.pop("unsupported_claims", []) or [])
            review["unsupported_claim_count"] = len(claims)


def _scenario_checks(
    *,
    kind: str,
    public: dict[str, object],
    receipts: list[dict[str, object]],
    live_calls: list[dict[str, object]],
    aligned_calls: list[dict[str, object]],
    alignment_complete: bool,
    before_state: dict[str, object],
    after_state: dict[str, object],
    before_hash: str,
    after_hash: str,
    expressor_delta: dict[str, int],
    expected_model: str,
) -> dict[str, bool]:
    reply = str(public.get("reply") or "").strip()
    live_operations = [str(item.get("operation") or "") for item in live_calls]
    common = {
        "http_ok": int(public.get("http_status") or 0) == 200,
        "reply_sent": bool(public.get("send_reply")) and bool(reply),
        "delivery_confirmed": _delivery_confirmed(
            list(public.get("deliveries") or [])
        ),
        "not_rolled_back": str(public.get("route") or "")
        != "gm_agent_message_transaction_rolled_back",
        "agent_error_empty": not bool(str(public.get("agent_error") or "")),
        "not_stale": not bool(public.get("stale_discarded")),
        "no_failed_provider_attempt": all(
            bool(item.get("ok")) and int(item.get("response_chars") or 0) > 0
            for item in live_calls
        ),
        "client_live_telemetry_aligned": alignment_complete,
        "all_aligned_calls_expected_model": all(
            str(item.get("model") or "") == expected_model
            for item in aligned_calls
        ),
        "thinking_disabled": all(
            item.get("thinking_enabled") is False
            and int(item.get("reasoning_chars") or 0) == 0
            for item in aligned_calls
        ),
        "expressor_unused": _llm_expressor_unused(
            expressor_delta,
            live_calls,
        ),
    }

    # Message logs and delivery ledgers are expected to persist even for a
    # read.  Only authoritative gameplay state belongs in this assertion; the
    # full snapshot change remains separately reported for audit.
    state_unchanged = _gameplay_state_projection(
        before_state
    ) == _gameplay_state_projection(after_state)
    no_grounding = not any("grounding" in operation for operation in live_operations)

    if kind == "opening_setup":
        common.update(
            {
                "start_adventure_succeeded": bool(
                    _successful_receipt(receipts, "start_adventure")
                ),
                "adventure_committed": (
                    after_state.get("gate_status") == "adventure"
                    and bool(after_state.get("session_active"))
                    and bool(after_state.get("scene_name"))
                ),
            }
        )
    elif kind == "ordinary_fact":
        proof = _ordinary_fact_provider_proof(
            {"provider_calls": live_calls}
        )
        common.update(
            {
                "state_unchanged": state_unchanged,
                "one_core_call_without_grounding": bool(proof.get("proved")),
            }
        )
    elif kind == "terminal_rule_read":
        receipt = _successful_receipt(receipts, EXPECTED_RULE_TOOL)
        common.update(
            {
                "state_unchanged": state_unchanged,
                "rule_tool_succeeded": bool(receipt),
                "terminal_public_result_signed": bool(
                    receipt.get("terminal_public_result")
                    and receipt.get("lock_public_reply")
                    and not receipt.get("state_changed")
                ),
                "no_post_tool_grounding_call": no_grounding,
                "rule_reply_mentions_anchor": "碎骨"
                in str(public.get("reply") or ""),
                "at_least_one_core_call": any(
                    operation.startswith(CORE_OPERATION_PREFIX)
                    for operation in live_operations
                ),
            }
        )
    elif kind == "declare_observation_check":
        pending_kinds = {
            str(item.get("kind") or "")
            for item in list(after_state.get("pending_windows") or [])
            if isinstance(item, dict)
        }
        common.update(
            {
                "declaration_tool_succeeded": bool(
                    _successful_receipt(receipts, EXPECTED_CHECK_TOOL)
                ),
                "roll_confirmation_window_open": (
                    "check_roll_confirmation" in pending_kinds
                ),
                "authoritative_state_changed": not state_unchanged,
            }
        )
    elif kind == "resolve_observation_check":
        before_pending = {
            str(item.get("kind") or "")
            for item in list(before_state.get("pending_windows") or [])
            if isinstance(item, dict)
        }
        after_pending = {
            str(item.get("kind") or "")
            for item in list(after_state.get("pending_windows") or [])
            if isinstance(item, dict)
        }
        common.update(
            {
                "roll_confirmation_existed": (
                    "check_roll_confirmation" in before_pending
                ),
                "roll_confirmation_consumed": (
                    "check_roll_confirmation" not in after_pending
                ),
                "authoritative_state_changed": not state_unchanged,
            }
        )
    elif kind == "session_close":
        common.update(
            {
                "end_session_succeeded": bool(
                    _successful_receipt(receipts, "end_session")
                ),
                "session_inactive": not bool(after_state.get("session_active")),
                "authoritative_state_changed": not state_unchanged,
            }
        )
    elif kind == "npc_dialogue":
        common["authoritative_state_available"] = bool(
            before_state.get("scene_name")
        )
    return common


def _llm_expressor_unused(
    expressor_calls: dict[str, int],
    provider_calls: Iterable[dict[str, object]],
) -> bool:
    """Distinguish deterministic receipt rendering from an LLM rewrite."""

    if any(
        int(expressor_calls.get(name) or 0)
        for name in ("render_agent_message", "render_scene_moment")
    ):
        return False
    # ``Expressor.render`` is also the deterministic renderer for signed rule
    # receipts. Calling that Python method is not an LLM Expressor round. A
    # model-backed legacy prose render would show up in provider telemetry as
    # a generic/expressor chat completion and must still fail this gate.
    return not any(
        str(item.get("operation") or "") == "chat_completion"
        or str(item.get("operation") or "").startswith("expressor")
        or str(item.get("operation") or "").startswith("public_expression")
        for item in provider_calls
    )


def _run_scenario(
    *,
    kind: str,
    required: bool,
    message: str,
    message_id: str,
    activity_version: int,
    host: str,
    port: int,
    service: FUGMHttpService,
    runtime: Any,
    client: OpenAICompatibleClient,
    expressor_calls: dict[str, int],
    expected_model: str,
    reply_to_invitation: bool = False,
) -> dict[str, object]:
    before_state = _state_probe(service, runtime)
    before_hash = snapshot_hash(service.data_root)
    expressor_before = dict(expressor_calls)
    client_start = len(client.recent_calls)
    status, body, wall_ms = route_message(
        host,
        port,
        message_id=message_id,
        activity_version=activity_version,
        message=message,
        reply_to_invitation=reply_to_invitation,
    )
    client_end = len(client.recent_calls)
    live = live_run_summary(host, port, message_id=message_id)
    deliveries = confirm_deliveries(host, port, body)
    public = response_summary(
        status=status,
        body=body,
        wall_ms=wall_ms,
        live_run=live,
        deliveries=deliveries,
    )
    safe_context_manifests: list[dict[str, object]] = []
    allowed_manifest_fields = {
        "capability_routing_mode",
        "capability_profile_ids",
        "shadow_profile_ids",
        "schema_count",
        "schema_chars",
        "schema_names_hash",
        "state_context_mode",
        "state_delta_status",
        "state_base_hash",
        "state_effective_hash",
        "state_delta_operations",
        "state_delta_chars",
        "state_reset_reason",
        "prompt_layout_version",
        "layout_fingerprint",
        "projected_chars",
        "approximate_tokens",
    }
    for raw_step in list(body.get("agent_trace") or []):
        if not isinstance(raw_step, dict):
            continue
        manifest = raw_step.get("context_manifest")
        if not isinstance(manifest, dict):
            continue
        safe_context_manifests.append(
            {
                key: manifest[key]
                for key in sorted(allowed_manifest_fields)
                if key in manifest
            }
        )
    public["context_manifests"] = safe_context_manifests
    _drop_private_grounding_text(public)
    receipts = _receipt_summaries(body)
    client_slice = sanitized_client_calls(client)[client_start:client_end]
    live_calls = [
        dict(item)
        for item in list(live.get("provider_calls") or [])
        if isinstance(item, dict)
    ]
    alignment = _align_client_calls_to_live_run(client_slice, live_calls)
    aligned_calls = [
        dict(item) for item in list(alignment.get("aligned") or [])
    ]
    after_state = _state_probe(service, runtime)
    after_hash = snapshot_hash(service.data_root)
    expressor_delta = {
        key: int(expressor_calls.get(key, 0)) - int(expressor_before.get(key, 0))
        for key in sorted(set(expressor_calls) | set(expressor_before))
    }
    checks = _scenario_checks(
        kind=kind,
        public=public,
        receipts=receipts,
        live_calls=live_calls,
        aligned_calls=aligned_calls,
        alignment_complete=bool(alignment.get("complete")),
        before_state=before_state,
        after_state=after_state,
        before_hash=before_hash,
        after_hash=after_hash,
        expressor_delta=expressor_delta,
        expected_model=expected_model,
    )
    return {
        "kind": kind,
        "required": required,
        "message_id": message_id,
        "public_output": str(public.get("reply") or ""),
        "http_wall_ms": int(public.get("wall_ms") or 0),
        "response": public,
        "receipts": receipts,
        "provider": {
            "live_run": {
                "elapsed_ms": int(live.get("elapsed_ms") or 0),
                "core_agent_ms": live.get("core_agent_ms"),
                "public_expression_ms": live.get("public_expression_ms"),
                "operations": live_calls,
            },
            "client": {
                "usage": _usage_summary(aligned_calls),
                "operations": aligned_calls,
            },
            "alignment": alignment,
        },
        "state": {
            "before": before_state,
            "after": after_state,
            "snapshot_changed": before_hash != after_hash,
        },
        "expressor_delta": expressor_delta,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _skipped_scenario(kind: str, reason: str) -> dict[str, object]:
    return {
        "kind": kind,
        "required": False,
        "skipped": True,
        "skip_reason": reason,
        "passed": True,
    }


def _has_blocking_window(runtime: Any) -> bool:
    return any(bool(item.get("blocking")) for item in _pending_windows(runtime))


def _wait_post_close_background(
    *,
    service: FUGMHttpService,
    runtime: Any,
    timeout_seconds: float,
) -> dict[str, object]:
    """Observe optional post-close jobs with one shared wait budget."""

    started = time.monotonic()
    deadline = started + max(0.0, timeout_seconds)
    summary_wait = getattr(runtime.log_manager, "wait_for_summary_enrichment", None)
    if callable(summary_wait):
        summary = summary_wait(
            CAMPAIGN_ID,
            SESSION_ID,
            timeout=max(0.0, deadline - time.monotonic()),
        )
    else:
        summary = {"status": "unsupported"}
    next_wait = getattr(
        service.adventure_opening_prefetcher,
        "wait_next_session",
        None,
    )
    if callable(next_wait):
        next_contract = next_wait(
            CAMPAIGN_ID,
            timeout_seconds=max(0.0, deadline - time.monotonic()),
        )
    else:
        next_contract = {"status": "unsupported"}
    return {
        "wall_ms": int((time.monotonic() - started) * 1000),
        "shared_timeout_seconds": max(0.0, timeout_seconds),
        "summary_enrichment": summary,
        "next_session_contract": next_contract,
    }


def _shutdown_background_workers(
    service: FUGMHttpService | None,
    runtime: Any | None,
) -> None:
    if runtime is not None:
        designer = getattr(runtime.app, "npc_blueprint_designer", None)
        if designer is not None:
            try:
                designer.wait_for_all(timeout=60.0)
            except Exception:
                pass
            executor = getattr(designer, "_executor", None)
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=False)
        shutdown_summary = getattr(
            runtime.log_manager,
            "shutdown_summary_enrichment",
            None,
        )
        if callable(shutdown_summary):
            shutdown_summary(wait=True)
    if service is not None:
        prefetcher = getattr(service, "adventure_opening_prefetcher", None)
        executor = getattr(prefetcher, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)


def _close_client(client: OpenAICompatibleClient | None) -> None:
    close = getattr(getattr(client, "transport", None), "close", None)
    if callable(close):
        close()


def _measure_opening_background(
    runtime: Any,
    client: OpenAICompatibleClient,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], bool]:
    """Wait for opening jobs and include every call completed in that wait."""

    call_start = len(client.recent_calls)
    summary = _wait_background(
        runtime,
        timeout_seconds=timeout_seconds,
    )
    call_end = len(client.recent_calls)
    calls = sanitized_client_calls(client)[call_start:call_end]
    summary["provider"] = _usage_summary(calls)
    summary["provider_calls"] = calls
    clean = (
        bool(summary.get("settled"))
        and not int(summary.get("failed_jobs") or 0)
        and not bool(summary.get("error"))
    )
    return summary, clean


def _prewarm_diagnostics(
    runtime: Any,
    *,
    model_reviewed: bool,
) -> dict[str, object]:
    """Read the worker-persisted diagnostics before the idle live objects.

    The opening prefetcher performs model preparation on a detached campaign
    manager.  Its errors are therefore persisted on the prepared envelope and
    are not guaranteed to be present on the original runtime's concretizer.
    """

    concretizer = (
        runtime.app.campaign_pacing_manager.contract_planner.concretizer
    )
    reviewer = concretizer.reachability_reviewer
    prepared = (
        runtime.app.session_zero_manager.state.prepared_chapter_one_session
    )
    persisted = dict(getattr(prepared, "diagnostics", {}) or {})
    reachability_status = str(
        persisted.get("reachability_last_status")
        or getattr(reviewer, "last_status", "")
        or ""
    )[:80]
    persisted_reachability_error = str(
        persisted.get("reachability_last_error") or ""
    )[:500]
    if persisted_reachability_error:
        reachability_error = persisted_reachability_error
    elif persisted and reachability_status.startswith("fallback_"):
        # Older persisted envelopes retained the bounded fallback status but
        # not a separate reviewer error string.  Preserve that exact reason
        # rather than reporting an unrelated idle-runtime diagnostic.
        reachability_error = reachability_status
    else:
        reachability_error = str(
            getattr(reviewer, "last_error", "") or ""
        )[:500]
    return {
        "model_reviewed": bool(model_reviewed),
        "diagnostic_source": (
            "prepared_chapter_one_session"
            if persisted
            else "live_runtime_fallback"
        ),
        "last_error": str(
            persisted.get("last_error")
            or getattr(concretizer, "last_error", "")
            or ""
        )[:500],
        "last_gatekeeper_repair_status": str(
            persisted.get("last_gatekeeper_repair_status")
            or getattr(concretizer, "last_gatekeeper_repair_status", "")
            or ""
        )[:80],
        "reachability_last_status": reachability_status,
        "reachability_last_error": reachability_error,
    }


def _smoke_run_result(
    *,
    config: Any,
    service: FUGMHttpService,
    runtime: Any,
    route_client: OpenAICompatibleClient,
    preparation: dict[str, object],
    health_summary: dict[str, object],
    isolated_port: int,
    opening_background: dict[str, object],
    opening_background_clean: bool,
    post_close_background: dict[str, object],
    expressor_calls: dict[str, int],
    abort_error: str = "",
) -> dict[str, object]:
    """Finalize telemetry even when a required scenario fails acceptance."""

    prep_calls = [
        dict(item)
        for item in list(preparation.get("provider_calls") or [])
        if isinstance(item, dict)
    ]
    roles = role_snapshot(service, runtime)
    route_provider_calls = sanitized_client_calls(route_client)
    run_checks = {
        "isolated_health_ok": int(health_summary.get("http_status") or 0) == 200
        and bool(health_summary.get("ok")),
        "all_preparation_calls_expected_model": bool(prep_calls)
        and all(item.get("model") == config.action_model for item in prep_calls),
        "all_preparation_calls_non_thinking": bool(prep_calls)
        and all(
            item.get("thinking_enabled") is False
            and int(item.get("reasoning_chars") or 0) == 0
            for item in prep_calls
        ),
        "all_roles_expected_model": bool(roles)
        and all(value == config.action_model for value in roles.values()),
        "all_route_calls_expected_model": bool(route_provider_calls)
        and all(
            item.get("model") == config.action_model
            for item in route_provider_calls
        ),
        "all_route_calls_non_thinking": bool(route_provider_calls)
        and all(
            item.get("thinking_enabled") is False
            and int(item.get("reasoning_chars") or 0) == 0
            for item in route_provider_calls
        ),
        "no_failed_route_provider_attempts": all(
            bool(item.get("ok"))
            and int(item.get("response_chars") or 0) > 0
            for item in route_provider_calls
        ),
        "route_provider_telemetry_complete": (
            int(route_client.total_calls) == len(route_provider_calls)
        ),
        "opening_background_settled_cleanly": bool(
            opening_background_clean
        ),
        "outer_expressor_unused": _llm_expressor_unused(
            expressor_calls,
            route_provider_calls,
        ),
        "required_scenarios_completed": not bool(abort_error),
    }
    return {
        "isolated_port": int(isolated_port),
        "health": dict(health_summary),
        "roles": roles,
        "preparation": preparation,
        "opening_background": opening_background,
        "post_close_background": post_close_background,
        "final_state": _state_probe(service, runtime),
        "expressor_calls": dict(expressor_calls),
        "route_provider_total": _usage_summary(route_provider_calls),
        "route_provider_calls": route_provider_calls,
        "route_provider_telemetry_complete": (
            int(route_client.total_calls) == len(route_provider_calls)
        ),
        "aborted": bool(abort_error),
        "abort_error": str(abort_error or "")[:800],
        "checks": run_checks,
    }


def _run_smoke(
    *,
    config: Any,
    args: argparse.Namespace,
    scenario_rows: list[dict[str, object]],
) -> dict[str, object]:
    prep_client: OpenAICompatibleClient | None = None
    route_client: OpenAICompatibleClient | None = None
    seeding_service: FUGMHttpService | None = None
    service: FUGMHttpService | None = None
    runtime: Any | None = None
    server: Any | None = None
    server_thread: threading.Thread | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="fu-gm-nonopening-smoke-") as temp:
            data_root = Path(temp)
            os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(
                data_root / "nortantis_maps"
            )

            prep_client = OpenAICompatibleClient(config)
            prep_bundle = TestLLMClientBundle.shared(
                prep_client,
                model=config.action_model,
            )
            seeding_service = _new_optimized_service(
                data_root=data_root,
                bundle=prep_bundle,
                rules_seed=int(args.seed),
                capability_routing_mode=str(args.capability_routing_mode),
                state_context_mode=str(args.state_context_mode),
            )
            seeding_runtime = seed_kariba_ready_campaign(
                seeding_service,
                campaign_id=CAMPAIGN_ID,
                session_id=SESSION_ID,
                channel_id=CHANNEL_ID,
                skip_map_render=True,
            )
            prep_start = time.monotonic()
            scheduled = seeding_service.adventure_opening_prefetcher.schedule(
                campaign_id=CAMPAIGN_ID,
                session_id=SESSION_ID,
                channel_id=CHANNEL_ID,
            )
            prep_status = seeding_service.adventure_opening_prefetcher.wait(
                CAMPAIGN_ID,
                timeout_seconds=float(args.prewarm_timeout),
            )
            seeding_service.adventure_opening_prefetcher._executor.shutdown(
                wait=True,
                cancel_futures=False,
            )
            prep_status = seeding_service.adventure_opening_prefetcher.status(
                CAMPAIGN_ID
            )
            prep_model_reviewed = bool(
                prep_status.get("status") == "ready"
                and prep_status.get("quality_status") == "model_reviewed"
            )
            prep_diagnostics = _prewarm_diagnostics(
                seeding_runtime,
                model_reviewed=prep_model_reviewed,
            )
            if not prep_model_reviewed and not bool(
                getattr(args, "allow_degraded_prewarm", False)
            ):
                raise RuntimeError(
                    "optimized prewarm did not create a model-reviewed envelope: "
                    f"{prep_status}; diagnostics={prep_diagnostics}"
                )
            prep_calls = sanitized_client_calls(prep_client)
            preparation = {
                "scheduled": scheduled,
                "status": prep_status,
                "diagnostics": prep_diagnostics,
                "wall_ms": int((time.monotonic() - prep_start) * 1000),
                "provider": _usage_summary(prep_calls),
                "provider_calls": prep_calls,
            }

            route_client = OpenAICompatibleClient(config)
            route_bundle = TestLLMClientBundle.shared(
                route_client,
                model=config.action_model,
            )
            service = _new_optimized_service(
                data_root=data_root,
                bundle=route_bundle,
                rules_seed=int(args.seed),
                capability_routing_mode=str(args.capability_routing_mode),
                state_context_mode=str(args.state_context_mode),
            )
            runtime = service._runtime(CAMPAIGN_ID)
            runtime.app.ensure_world_map_for_adventure = lambda **_kwargs: {
                "status": "existing",
                "reason": "isolated non-opening smoke fixture",
            }
            expressor_calls = install_expressor_spies(runtime.app.expressor)

            server = make_server("127.0.0.1", 0, service=service)
            host, port = server.server_address
            if int(port) == 8765:
                raise RuntimeError("ephemeral server selected production port")
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            server_thread.start()
            health_status, health, health_ms = request_json(
                str(host),
                int(port),
                "GET",
                "/health",
                timeout=10.0,
            )
            health_summary = {
                "http_status": health_status,
                "ok": bool(health.get("ok")),
                "wall_ms": health_ms,
                "flow_mode": str(
                    dict(health.get("runtime") or {}).get(
                        "adventure_opening_flow_mode"
                    )
                    or ""
                ),
            }
            opening_background: dict[str, object] = {
                "status": "not_started",
                "provider": _usage_summary([]),
                "provider_calls": [],
            }
            opening_background_clean = False
            post_close_background: dict[str, object] = {
                "status": "not_applicable"
            }

            activity_version = 1
            opening = _run_scenario(
                kind="opening_setup",
                required=True,
                message="嗯，进入第一章吧。",
                message_id="nonopening-smoke-setup-opening",
                activity_version=activity_version,
                host=str(host),
                port=int(port),
                service=service,
                runtime=runtime,
                client=route_client,
                expressor_calls=expressor_calls,
                expected_model=config.action_model,
                reply_to_invitation=True,
            )
            scenario_rows.append(opening)
            if not bool(opening.get("passed")):
                opening_background, opening_background_clean = (
                    _measure_opening_background(
                        runtime,
                        route_client,
                        timeout_seconds=float(args.background_timeout),
                    )
                )
                return _smoke_run_result(
                    config=config,
                    service=service,
                    runtime=runtime,
                    route_client=route_client,
                    preparation=preparation,
                    health_summary=health_summary,
                    isolated_port=int(port),
                    opening_background=opening_background,
                    opening_background_clean=opening_background_clean,
                    post_close_background=post_close_background,
                    expressor_calls=expressor_calls,
                    abort_error=(
                        "optimized opening setup failed benchmark checks"
                    ),
                )

            opening_background, opening_background_clean = (
                _measure_opening_background(
                    runtime,
                    route_client,
                    timeout_seconds=float(args.background_timeout),
                )
            )
            if not opening_background_clean:
                return _smoke_run_result(
                    config=config,
                    service=service,
                    runtime=runtime,
                    route_client=route_client,
                    preparation=preparation,
                    health_summary=health_summary,
                    isolated_port=int(port),
                    opening_background=opening_background,
                    opening_background_clean=False,
                    post_close_background=post_close_background,
                    expressor_calls=expressor_calls,
                    abort_error=(
                        "opening NPC background work did not settle cleanly"
                    ),
                )

            mandatory = (
                (
                    "ordinary_fact",
                    "时悠，请只用一句话确认第一章已经开始，不要改变任何状态。",
                ),
                (
                    "terminal_rule_read",
                    "请查规则库并直接告诉我技能【碎骨】的规则，不要改变任何角色或场景状态。",
                ),
                (
                    "declare_observation_check",
                    "诺艾尔仔细观察牢门、走廊和裂缝，寻找可利用的逃生线索。请按规则先声明一次洞察检定并等我确认，不要替我投骰。",
                ),
                ("resolve_observation_check", "投。"),
            )
            for kind, message in mandatory:
                activity_version += 1
                row = _run_scenario(
                    kind=kind,
                    required=True,
                    message=message,
                    message_id=f"nonopening-smoke-{activity_version}-{kind}",
                    activity_version=activity_version,
                    host=str(host),
                    port=int(port),
                    service=service,
                    runtime=runtime,
                    client=route_client,
                    expressor_calls=expressor_calls,
                    expected_model=config.action_model,
                )
                scenario_rows.append(row)
                if not bool(row.get("passed")):
                    return _smoke_run_result(
                        config=config,
                        service=service,
                        runtime=runtime,
                        route_client=route_client,
                        preparation=preparation,
                        health_summary=health_summary,
                        isolated_port=int(port),
                        opening_background=opening_background,
                        opening_background_clean=opening_background_clean,
                        post_close_background=post_close_background,
                        expressor_calls=expressor_calls,
                        abort_error=f"required scenario failed: {kind}",
                    )

            if bool(args.skip_npc_dialogue):
                scenario_rows.append(
                    _skipped_scenario("npc_dialogue", "disabled_by_cli")
                )
            elif _has_blocking_window(runtime):
                scenario_rows.append(
                    _skipped_scenario(
                        "npc_dialogue",
                        "blocking_decision_window_after_roll",
                    )
                )
            else:
                npc_name = _authoritative_npc_anchor(runtime)
                if not npc_name:
                    scenario_rows.append(
                        _skipped_scenario(
                            "npc_dialogue",
                            "no_non_pc_in_current_scene",
                        )
                    )
                else:
                    activity_version += 1
                    scenario_rows.append(
                        _run_scenario(
                            kind="npc_dialogue",
                            required=False,
                            message=(
                                f"诺艾尔问{npc_name}：“你刚才看见什么了？”"
                                f"请让{npc_name}只依据当前公开局面回应。"
                            ),
                            message_id=(
                                f"nonopening-smoke-{activity_version}-npc-dialogue"
                            ),
                            activity_version=activity_version,
                            host=str(host),
                            port=int(port),
                            service=service,
                            runtime=runtime,
                            client=route_client,
                            expressor_calls=expressor_calls,
                            expected_model=config.action_model,
                        )
                    )

            close_executed = False
            if bool(args.skip_session_close):
                scenario_rows.append(
                    _skipped_scenario("session_close", "disabled_by_cli")
                )
            elif _has_blocking_window(runtime):
                scenario_rows.append(
                    _skipped_scenario(
                        "session_close",
                        "blocking_decision_window_after_roll",
                    )
                )
            elif not runtime.app.session_ledger.active:
                scenario_rows.append(
                    _skipped_scenario("session_close", "session_not_active")
                )
            else:
                activity_version += 1
                close_row = _run_scenario(
                    kind="session_close",
                    required=False,
                    message=(
                        "今天先收团并保存当前进度。未完成的逃生目标保持未完成，"
                        "不要写成我们已经逃出了监狱。"
                    ),
                    message_id=f"nonopening-smoke-{activity_version}-session-close",
                    activity_version=activity_version,
                    host=str(host),
                    port=int(port),
                    service=service,
                    runtime=runtime,
                    client=route_client,
                    expressor_calls=expressor_calls,
                    expected_model=config.action_model,
                )
                scenario_rows.append(close_row)
                close_executed = bool(
                    _successful_receipt(
                        list(close_row.get("receipts") or []),
                        "end_session",
                    )
                )

            if close_executed:
                close_background_start = len(route_client.recent_calls)
                post_close_background = _wait_post_close_background(
                    service=service,
                    runtime=runtime,
                    timeout_seconds=float(args.background_timeout),
                )
                close_background_end = len(route_client.recent_calls)
                close_background_calls = sanitized_client_calls(route_client)[
                    close_background_start:close_background_end
                ]
                post_close_background["provider"] = _usage_summary(
                    close_background_calls
                )
                post_close_background["provider_calls"] = close_background_calls

            return _smoke_run_result(
                config=config,
                service=service,
                runtime=runtime,
                route_client=route_client,
                preparation=preparation,
                health_summary=health_summary,
                isolated_port=int(port),
                opening_background=opening_background,
                opening_background_clean=opening_background_clean,
                post_close_background=post_close_background,
                expressor_calls=expressor_calls,
            )
    finally:
        if server is not None and server_thread is not None:
            server.shutdown()
        if server is not None:
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=3.0)
        _shutdown_background_workers(service, runtime)
        if seeding_service is not None:
            try:
                _shutdown_background_workers(
                    seeding_service,
                    locals().get("seeding_runtime"),
                )
            except Exception:
                pass
        _close_client(route_client)
        _close_client(prep_client)


def _write_json_secure(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _write_jsonl_secure(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def _secret_scan(
    paths: Iterable[Path],
    *,
    api_key: str,
) -> dict[str, object]:
    markers = ["Bearer ", "Authorization:", "sk-"]
    clean_key = str(api_key or "").strip()
    if clean_key:
        markers.append(clean_key)
    checked: list[str] = []
    found: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        checked.append(path.name)
        for marker in markers:
            if marker and marker in text:
                found.append("configured_api_key" if marker == clean_key else marker)
    return {
        "checked_files": checked,
        "forbidden_marker_count": len(found),
        "passed": not found,
    }


def _scenario_latency_summary(
    rows: Iterable[dict[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for row in rows:
        kind = str(row.get("kind") or "")
        if not kind:
            continue
        if bool(row.get("skipped")):
            result[kind] = {
                "skipped": True,
                "reason": str(row.get("skip_reason") or ""),
            }
            continue
        provider = dict(row.get("provider") or {})
        client = dict(provider.get("client") or {})
        usage = dict(client.get("usage") or {})
        result[kind] = {
            "http_wall_ms": int(row.get("http_wall_ms") or 0),
            "provider_call_count": int(usage.get("call_count") or 0),
            "provider_elapsed_ms": int(usage.get("model_elapsed_ms") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "cache_miss_tokens": int(usage.get("cache_miss_tokens") or 0),
            "cache_token_hit_rate": usage.get("cache_token_hit_rate"),
            "operations": list(usage.get("operations") or []),
            "passed": bool(row.get("passed")),
        }
    return result


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.prewarm_timeout <= 0 or args.background_timeout <= 0:
        raise ValueError("timeouts must be positive")
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
    output_dir = args.output_root / f"deepseek_nonopening_smoke_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    runs_path = output_dir / "scenarios.jsonl"
    summary_path = output_dir / "summary.json"

    production_before = _production_snapshot()
    rows: list[dict[str, object]] = []
    run: dict[str, object] = {}
    fatal_error = ""
    try:
        run = _run_smoke(config=config, args=args, scenario_rows=rows)
        fatal_error = str(run.get("abort_error") or "")
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:800]
    production_after = _production_snapshot()
    production_is_unchanged = _production_unchanged(
        production_before,
        production_after,
    )

    required_rows = [row for row in rows if bool(row.get("required"))]
    executed_optional = [
        row
        for row in rows
        if not bool(row.get("required")) and not bool(row.get("skipped"))
    ]
    summary: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_kind": "isolated_real_chain_smoke",
        "rules_seed": int(args.seed),
        "model": config.action_model,
        "endpoint": config.chat_completions_url(),
        "thinking": "disabled",
        "public_expression_mode": "core",
        "capability_routing_mode": str(args.capability_routing_mode),
        "state_context_mode": str(args.state_context_mode),
        "production_port_mutation_attempted": False,
        "production_before": production_before,
        "production_after": production_after,
        "production_pid_and_reachability_unchanged": production_is_unchanged,
        "run": run,
        "scenario_latency": _scenario_latency_summary(rows),
        "required_scenarios_passed": bool(required_rows)
        and all(bool(row.get("passed")) for row in required_rows),
        "executed_optional_scenarios_passed": all(
            bool(row.get("passed")) for row in executed_optional
        ),
        "fatal_error": fatal_error,
    }
    summary["passed"] = bool(
        not fatal_error
        and summary["required_scenarios_passed"]
        and summary["executed_optional_scenarios_passed"]
        and production_is_unchanged
        and all(bool(value) for value in dict(run.get("checks") or {}).values())
        and all(
            bool(dict(row.get("checks") or {}).get("expressor_unused", True))
            for row in rows
        )
    )

    _write_jsonl_secure(runs_path, rows)
    _write_json_secure(summary_path, summary)
    first_secret_scan = _secret_scan(
        (runs_path, summary_path),
        api_key=config.api_key,
    )
    summary["artifact_security"] = {
        "directory_mode": oct(output_dir.stat().st_mode & 0o777),
        "scenarios_mode": oct(runs_path.stat().st_mode & 0o777),
        "summary_mode": oct(summary_path.stat().st_mode & 0o777),
        "secret_scan": first_secret_scan,
    }
    _write_json_secure(summary_path, summary)
    final_secret_scan = _secret_scan(
        (runs_path, summary_path),
        api_key=config.api_key,
    )
    if not bool(first_secret_scan.get("passed")) or not bool(
        final_secret_scan.get("passed")
    ):
        raise RuntimeError("artifact secret scan failed")
    print(
        f"summary={summary_path} passed={summary['passed']} "
        f"required={len(required_rows)} optional={len(executed_optional)}",
        flush=True,
    )
    return 0 if bool(summary["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
