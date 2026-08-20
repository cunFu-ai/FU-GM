from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import benchmark_deepseek_opening_ab as benchmark


def _call(operation: str, *, ok: bool = True) -> dict[str, object]:
    return {
        "operation": operation,
        "ok": ok,
        "response_chars": 24 if ok else 0,
    }


def test_ordinary_fact_proof_accepts_one_successful_core_call() -> None:
    proof = benchmark._ordinary_fact_provider_proof(
        {
            "provider_calls": [
                _call("gm_tool_agent.iteration_1"),
            ]
        }
    )

    assert proof["proved"] is True
    assert proof["only_one_core_call"] is True
    assert proof["no_model_grounding"] is True
    assert proof["source"] == "live_run.provider_attempt_finished"


def test_ordinary_fact_proof_rejects_model_grounding_call() -> None:
    proof = benchmark._ordinary_fact_provider_proof(
        {
            "provider_calls": [
                _call("gm_tool_agent.iteration_1"),
                _call("gm_reply_grounding_verification"),
            ]
        }
    )

    assert proof["proved"] is False
    assert proof["only_one_core_call"] is False
    assert proof["no_model_grounding"] is False
    assert proof["grounding_operations"] == [
        "gm_reply_grounding_verification"
    ]


def test_ordinary_fact_proof_rejects_missing_or_failed_core_call() -> None:
    assert benchmark._ordinary_fact_provider_proof({})["proved"] is False
    proof = benchmark._ordinary_fact_provider_proof(
        {
            "provider_calls": [
                _call("gm_tool_agent.iteration_1", ok=False),
            ]
        }
    )
    assert proof["proved"] is False
    assert proof["provider_call_succeeded"] is False


def test_opening_provider_partition_keeps_npc_calls_out_of_critical_path() -> None:
    calls = [
        _call("unrelated_before_window"),
        _call("gm_tool_agent.iteration_1"),
        _call("npc_blueprint_design"),
        _call("scene_opening"),
        _call("npc_blueprint_design"),
        _call("ordinary_fact_after_window"),
    ]

    partition = benchmark._partition_opening_provider_calls(
        calls,
        route_start_index=1,
        route_end_index=4,
        background_end_index=5,
    )

    assert [item["operation"] for item in partition["critical"]] == [
        "gm_tool_agent.iteration_1",
        "scene_opening",
    ]
    assert [item["operation"] for item in partition["background"]] == [
        "npc_blueprint_design",
        "npc_blueprint_design",
    ]
    assert partition["route_partition_complete"] is True
    assert partition["settle_only_background"] is True


def test_opening_provider_partition_flags_non_npc_settle_call() -> None:
    calls = [
        _call("gm_tool_agent.iteration_1"),
        _call("gm_reply_grounding_verification"),
    ]

    partition = benchmark._partition_opening_provider_calls(
        calls,
        route_start_index=0,
        route_end_index=1,
        background_end_index=2,
    )

    assert partition["settle_only_background"] is False
    assert [
        item["operation"] for item in partition["unclassified_settle"]
    ] == ["gm_reply_grounding_verification"]


def test_reloaded_prefetch_fingerprint_evidence_uses_only_short_prefixes() -> None:
    fingerprint = "a" * 64
    runtime = SimpleNamespace(
        transaction_lock=threading.RLock(),
        app=SimpleNamespace(
            session_zero_manager=SimpleNamespace(
                state=SimpleNamespace(
                    prepared_chapter_one_session=SimpleNamespace(
                        fingerprint=fingerprint
                    )
                )
            )
        ),
    )
    service = SimpleNamespace(
        adventure_opening_prefetcher=SimpleNamespace(
            _current_fingerprint_locked=lambda _runtime: fingerprint
        )
    )

    evidence = benchmark._reloaded_prefetch_fingerprint_evidence(
        service,
        runtime,
    )

    assert evidence == {
        "persisted_prefix": "a" * 12,
        "current_prefix": "a" * 12,
        "persisted_present": True,
        "current_present": True,
        "matches": True,
    }


def test_reloaded_prefetch_fingerprint_evidence_detects_mismatch() -> None:
    runtime = SimpleNamespace(
        transaction_lock=threading.RLock(),
        app=SimpleNamespace(
            session_zero_manager=SimpleNamespace(
                state=SimpleNamespace(
                    prepared_chapter_one_session=SimpleNamespace(
                        fingerprint="a" * 64
                    )
                )
            )
        ),
    )
    service = SimpleNamespace(
        adventure_opening_prefetcher=SimpleNamespace(
            _current_fingerprint_locked=lambda _runtime: "b" * 64
        )
    )

    evidence = benchmark._reloaded_prefetch_fingerprint_evidence(
        service,
        runtime,
    )

    assert evidence["matches"] is False
    assert evidence["persisted_prefix"] == "a" * 12
    assert evidence["current_prefix"] == "b" * 12
