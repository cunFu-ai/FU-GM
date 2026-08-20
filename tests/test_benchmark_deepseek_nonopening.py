from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import benchmark_deepseek_nonopening as benchmark


def _live_call(operation: str) -> dict[str, object]:
    return {
        "model": "deepseek-v4-flash",
        "operation": operation,
        "attempt": 1,
        "ok": True,
        "elapsed_ms": 123,
        "response_chars": 30,
        "reasoning_chars": 0,
    }


def _client_call(operation: str) -> dict[str, object]:
    return {
        **_live_call(operation),
        "thinking_enabled": False,
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cached_tokens": 80,
            "cache_miss_tokens": 20,
            "cache_usage_reported": True,
        },
    }


def _public(reply: str = "公开结果。") -> dict[str, object]:
    return {
        "http_status": 200,
        "send_reply": True,
        "reply": reply,
        "deliveries": [{"status": 200, "ok": True}],
        "route": "gm_agent_tool",
        "agent_error": "",
        "stale_discarded": False,
    }


def _state(*, pending: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "gate_status": "adventure",
        "session_active": True,
        "scene_name": "牢墙崩塌",
        "pending_windows": [
            {"kind": kind, "owner": "诺艾尔", "blocking": True}
            for kind in pending
        ],
    }


def test_alignment_separates_shared_client_background_call() -> None:
    core = _client_call("gm_tool_agent.iteration_1")
    grounding = _client_call("gm_reply_grounding_verification")
    background = _client_call("npc_blueprint_design")
    live = [
        _live_call("gm_tool_agent.iteration_1"),
        _live_call("gm_reply_grounding_verification"),
    ]

    result = benchmark._align_client_calls_to_live_run(
        [background, core, grounding],
        live,
    )

    assert [item["operation"] for item in result["aligned"]] == [
        "gm_tool_agent.iteration_1",
        "gm_reply_grounding_verification",
    ]
    assert [
        item["operation"] for item in result["background_or_unclassified"]
    ] == ["npc_blueprint_design"]
    assert result["unmatched_live"] == []
    assert result["complete"] is False


def test_alignment_is_complete_for_exact_live_slice() -> None:
    calls = [
        _client_call("gm_tool_agent.iteration_1"),
        _client_call("scene_response"),
    ]

    result = benchmark._align_client_calls_to_live_run(
        calls,
        [
            _live_call("gm_tool_agent.iteration_1"),
            _live_call("scene_response"),
        ],
    )

    assert result["complete"] is True
    assert result["background_or_unclassified"] == []
    assert result["unmatched_live"] == []


def test_receipt_summary_keeps_terminal_proof_but_not_private_values() -> None:
    rows = benchmark._receipt_summaries(
        {
            "tool_receipts": [
                {
                    "tool_name": "get_rule_reference",
                    "ok": True,
                    "state_changed": False,
                    "lock_public_reply": True,
                    "result": {
                        "terminal_public_result": True,
                        "private_rule_payload": "must-not-be-copied",
                    },
                }
            ]
        }
    )

    assert rows == [
        {
            "tool_name": "get_rule_reference",
            "ok": True,
            "state_changed": False,
            "error_code": "",
            "lock_public_reply": True,
            "terminal_public_result": True,
            "result_keys": ["private_rule_payload", "terminal_public_result"],
        }
    ]
    assert "must-not-be-copied" not in repr(rows)


def test_private_grounding_claim_text_is_replaced_by_count() -> None:
    public = {
        "agent_trace": [
            {
                "grounding": [
                    {
                        "category": "unsupported",
                        "unsupported_claims": ["private claim one", "private claim two"],
                    }
                ]
            }
        ]
    }

    benchmark._drop_private_grounding_text(public)

    review = public["agent_trace"][0]["grounding"][0]
    assert review["unsupported_claim_count"] == 2
    assert "unsupported_claims" not in review
    assert "private claim" not in repr(public)


def test_terminal_rule_read_checks_prove_fast_public_receipt() -> None:
    live = [_live_call("gm_tool_agent.iteration_1")]
    aligned = [_client_call("gm_tool_agent.iteration_1")]
    state = _state()
    receipts = [
        {
            "tool_name": "get_rule_reference",
            "ok": True,
            "state_changed": False,
            "lock_public_reply": True,
            "terminal_public_result": True,
        }
    ]

    checks = benchmark._scenario_checks(
        kind="terminal_rule_read",
        public=_public("【碎骨】命中后会造成额外效果。"),
        receipts=receipts,
        live_calls=live,
        aligned_calls=aligned,
        alignment_complete=True,
        before_state=state,
        after_state=dict(state),
        before_hash="same",
        after_hash="same",
        expressor_delta={"render": 0},
        expected_model="deepseek-v4-flash",
    )

    assert all(checks.values()), checks
    assert checks["terminal_public_result_signed"] is True
    assert checks["no_post_tool_grounding_call"] is True


def test_terminal_rule_read_rejects_a_grounding_operation() -> None:
    live = [
        _live_call("gm_tool_agent.iteration_1"),
        _live_call("gm_reply_grounding_verification"),
    ]
    aligned = [
        _client_call("gm_tool_agent.iteration_1"),
        _client_call("gm_reply_grounding_verification"),
    ]

    checks = benchmark._scenario_checks(
        kind="terminal_rule_read",
        public=_public("【碎骨】命中后会造成额外效果。"),
        receipts=[
            {
                "tool_name": "get_rule_reference",
                "ok": True,
                "state_changed": False,
                "lock_public_reply": True,
                "terminal_public_result": True,
            }
        ],
        live_calls=live,
        aligned_calls=aligned,
        alignment_complete=True,
        before_state=_state(),
        after_state=_state(),
        before_hash="same",
        after_hash="same",
        expressor_delta={},
        expected_model="deepseek-v4-flash",
    )

    assert checks["no_post_tool_grounding_call"] is False


def test_declare_and_resolve_checks_follow_authoritative_window() -> None:
    provider_live = [_live_call("gm_tool_agent.iteration_1")]
    provider_client = [_client_call("gm_tool_agent.iteration_1")]
    declare = benchmark._scenario_checks(
        kind="declare_observation_check",
        public=_public(),
        receipts=[
            {
                "tool_name": "declare_check_action",
                "ok": True,
                "state_changed": True,
            }
        ],
        live_calls=provider_live,
        aligned_calls=provider_client,
        alignment_complete=True,
        before_state=_state(),
        after_state=_state(pending=("check_roll_confirmation",)),
        before_hash="before",
        after_hash="declared",
        expressor_delta={},
        expected_model="deepseek-v4-flash",
    )
    resolve = benchmark._scenario_checks(
        kind="resolve_observation_check",
        public=_public(),
        receipts=[],
        live_calls=[],
        aligned_calls=[],
        alignment_complete=True,
        before_state=_state(pending=("check_roll_confirmation",)),
        after_state=_state(),
        before_hash="declared",
        after_hash="resolved",
        # Rule-result publication uses Expressor.render as a deterministic
        # Python formatter. It must not be confused with an LLM Expressor
        # call when provider telemetry contains only the core decision.
        expressor_delta={"render": 1},
        expected_model="deepseek-v4-flash",
    )

    assert all(declare.values()), declare
    assert all(resolve.values()), resolve


def test_deterministic_render_does_not_hide_model_expressor_call() -> None:
    checks = benchmark._scenario_checks(
        kind="resolve_observation_check",
        public=_public(),
        receipts=[],
        live_calls=[_live_call("chat_completion")],
        aligned_calls=[_client_call("chat_completion")],
        alignment_complete=True,
        before_state=_state(pending=("check_roll_confirmation",)),
        after_state=_state(),
        before_hash="declared",
        after_hash="resolved",
        expressor_delta={"render": 1},
        expected_model="deepseek-v4-flash",
    )

    assert checks["expressor_unused"] is False


def test_llm_expressor_gate_allows_only_deterministic_rule_render() -> None:
    core_call = [_live_call("gm_tool_agent.iteration_1")]

    assert benchmark._llm_expressor_unused({"render": 1}, core_call) is True
    assert (
        benchmark._llm_expressor_unused(
            {"render": 1, "render_agent_message": 1},
            core_call,
        )
        is False
    )
    assert (
        benchmark._llm_expressor_unused(
            {"render": 1},
            [_live_call("chat_completion")],
        )
        is False
    )


def test_npc_anchor_requires_current_non_pc_participant() -> None:
    pc = SimpleNamespace(name="诺艾尔", traits=["pc"])
    npc = SimpleNamespace(name="巴尔多", traits=["npc"])
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(participants=["诺艾尔", "巴尔多"])
            ),
            character_manager=SimpleNamespace(all=lambda: [pc, npc]),
        )
    )

    assert benchmark._authoritative_npc_anchor(runtime) == "巴尔多"
    runtime.app.scene_manager.current_scene.participants = ["诺艾尔"]
    assert benchmark._authoritative_npc_anchor(runtime) == ""


def test_secure_artifacts_are_private_and_secret_scan_detects_key(
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifact"
    output.mkdir(mode=0o700)
    summary = output / "summary.json"
    runs = output / "scenarios.jsonl"
    benchmark._write_json_secure(summary, {"reply": "ok"})
    benchmark._write_jsonl_secure(runs, [{"kind": "fact"}])

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(summary.stat().st_mode) == 0o600
    assert stat.S_IMODE(runs.stat().st_mode) == 0o600
    assert benchmark._secret_scan(
        (summary, runs),
        api_key="secret-value",
    )["passed"] is True

    benchmark._write_json_secure(summary, {"value": "secret-value"})
    scan = benchmark._secret_scan((summary, runs), api_key="secret-value")
    assert scan["passed"] is False
    assert scan["forbidden_marker_count"] == 1


def test_production_sentinel_compares_pid_health_and_start_time() -> None:
    before = {
        "pid": "123",
        "reachable": True,
        "health_ok": True,
        "started_at": "same-start",
    }

    assert benchmark._production_unchanged(before, dict(before)) is True
    changed = dict(before)
    changed["pid"] = "456"
    assert benchmark._production_unchanged(before, changed) is False


def test_prewarm_diagnostics_prefer_detached_worker_envelope() -> None:
    reviewer = SimpleNamespace(
        last_status="live_runtime_status",
        last_error="live runtime reviewer error",
    )
    concretizer = SimpleNamespace(
        last_error="live runtime concretizer error",
        last_gatekeeper_repair_status="live_runtime_repair",
        reachability_reviewer=reviewer,
    )
    prepared = SimpleNamespace(
        diagnostics={
            "last_error": "detached worker error",
            "last_gatekeeper_repair_status": "fallback_after_llm_failure",
            "reachability_last_status": "fallback_invalid_contract",
        }
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            campaign_pacing_manager=SimpleNamespace(
                contract_planner=SimpleNamespace(concretizer=concretizer)
            ),
            session_zero_manager=SimpleNamespace(
                state=SimpleNamespace(
                    prepared_chapter_one_session=prepared
                )
            ),
        )
    )

    diagnostics = benchmark._prewarm_diagnostics(
        runtime,
        model_reviewed=False,
    )

    assert diagnostics["diagnostic_source"] == "prepared_chapter_one_session"
    assert diagnostics["last_error"] == "detached worker error"
    assert (
        diagnostics["last_gatekeeper_repair_status"]
        == "fallback_after_llm_failure"
    )
    assert diagnostics["reachability_last_status"] == "fallback_invalid_contract"
    assert diagnostics["reachability_last_error"] == "fallback_invalid_contract"


def test_aborted_smoke_result_keeps_route_usage(monkeypatch) -> None:
    route_call = _client_call("gm_tool_agent.iteration_1")
    route_client = SimpleNamespace(recent_calls=[route_call], total_calls=1)
    monkeypatch.setattr(
        benchmark,
        "role_snapshot",
        lambda _service, _runtime: {"core": "deepseek-v4-flash"},
    )
    monkeypatch.setattr(
        benchmark,
        "_state_probe",
        lambda _service, _runtime: {"gate_status": "adventure"},
    )

    result = benchmark._smoke_run_result(
        config=SimpleNamespace(action_model="deepseek-v4-flash"),
        service=SimpleNamespace(),
        runtime=SimpleNamespace(),
        route_client=route_client,
        preparation={
            "provider_calls": [_client_call("session_prep")],
            "provider": benchmark._usage_summary(
                [_client_call("session_prep")]
            ),
        },
        health_summary={"http_status": 200, "ok": True},
        isolated_port=49152,
        opening_background={"status": "settled"},
        opening_background_clean=True,
        post_close_background={"status": "not_applicable"},
        expressor_calls={},
        abort_error="required scenario failed: ordinary_fact",
    )

    assert result["aborted"] is True
    assert result["abort_error"] == "required scenario failed: ordinary_fact"
    assert result["route_provider_total"]["call_count"] == 1
    assert result["route_provider_total"]["prompt_tokens"] == 100
    assert result["checks"]["required_scenarios_completed"] is False


def test_opening_failure_wait_collects_background_usage(monkeypatch) -> None:
    client = SimpleNamespace(recent_calls=[])

    def fake_wait(_runtime, *, timeout_seconds):
        assert timeout_seconds == 12.0
        client.recent_calls.append(_client_call("npc_blueprint_design"))
        return {"settled": True, "failed_jobs": 0, "error": ""}

    monkeypatch.setattr(benchmark, "_wait_background", fake_wait)

    summary, clean = benchmark._measure_opening_background(
        SimpleNamespace(),
        client,
        timeout_seconds=12.0,
    )

    assert clean is True
    assert summary["provider"]["call_count"] == 1
    assert summary["provider"]["prompt_tokens"] == 100
    assert summary["provider_calls"][0]["operation"] == "npc_blueprint_design"
