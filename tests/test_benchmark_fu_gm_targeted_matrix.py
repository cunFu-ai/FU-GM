from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_fu_gm_targeted_matrix.py"
SPEC = importlib.util.spec_from_file_location("benchmark_fu_gm_targeted_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeClient:
    recent_calls: list[dict[str, object]] = []


def test_world_registry_commits_all_supported_categories() -> None:
    state: list[str] = []
    registry = MODULE.world_registry(state)
    context = MODULE.GMToolExecutionContext(
        campaign_id="c",
        session_id="s0",
        channel_id="g",
        speaker="p",
        gate_status="session_zero",
        directly_addressed=True,
    )

    for category in sorted(MODULE.EXPECTED_WORLD_CATEGORIES):
        receipt = registry.execute(
            "create_world_setting",
            {"category": category},
            context,
        )
        assert receipt.ok is True

    assert set(state) == MODULE.EXPECTED_WORLD_CATEGORIES


def test_world_registry_rejects_unknown_category_without_mutation() -> None:
    state: list[str] = []
    registry = MODULE.world_registry(state)
    context = MODULE.GMToolExecutionContext(
        campaign_id="c",
        session_id="s0",
        channel_id="g",
        speaker="p",
        gate_status="session_zero",
        directly_addressed=True,
    )

    receipt = registry.execute(
        "create_world_setting",
        {"category": "unsupported"},
        context,
    )

    assert receipt.ok is False
    assert state == []


def test_snapshot_transaction_restores_partial_world_writes() -> None:
    state = ["kingdoms"]
    transaction = MODULE.SnapshotTransaction(state)
    state.append("mysteries")
    transaction.rollback()
    assert state == ["kingdoms"]


def _npc_response(
    *,
    reply: str,
    used_fallback: bool = False,
) -> dict[str, object]:
    return {
        "ok": True,
        "route": "gm_agent_tool",
        "reply": reply,
        "agent_error": "",
        "tool_receipts": [
            {
                "tool_name": "decide_npc_response",
                "ok": True,
                "state_changed": True,
                "error_code": "",
                "result": {
                    "npc_voice": {
                        "used_model": True,
                        "used_fallback": used_fallback,
                        "model": "mimo-v2.5",
                        "audit_performed": False,
                        "audit_passed": True,
                        "latency_ms": 81,
                        "fallback_reason": (
                            "bad voice output" if used_fallback else ""
                        ),
                    },
                    "private_plan": "must-not-be-copied",
                },
            }
        ],
    }


def _npc_calls() -> list[dict[str, object]]:
    return [
        {
            "model": "mimo-v2.5",
            "operation": "gm_tool_agent.iteration_1",
            "attempt": 1,
            "ok": True,
            "elapsed_ms": 100,
            "response_chars": 80,
            "thinking_enabled": False,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        },
        {
            "model": "mimo-v2.5",
            "operation": "npc_voice_render",
            "attempt": 1,
            "ok": True,
            "elapsed_ms": 80,
            "response_chars": 50,
            "thinking_enabled": False,
            "usage": {"prompt_tokens": 500, "completion_tokens": 50},
        },
    ]


def test_authoritative_npc_dialogue_accepts_model_voice_and_safe_reply() -> None:
    result = MODULE._npc_dialogue_result(
        model="mimo-v2.5",
        status=200,
        response=_npc_response(
            reply="国王抬眼看向伊莉雅。‘把清单留下，我会先命人核验车号。’"
        ),
        calls=_npc_calls(),
        elapsed_ms=250,
        isolated_port=49152,
        state_hash_before="before",
        state_hash_after="after",
    )

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["receipts"][0]["npc_voice"]["used_fallback"] is False
    assert result["provider"]["call_count"] == 2
    assert "must-not-be-copied" not in repr(result)


def test_authoritative_npc_dialogue_rejects_fallback_leak_and_player_automation() -> None:
    private_fact = MODULE.NPC_PRIVATE_MARKERS[0]
    result = MODULE._npc_dialogue_result(
        model="mimo-v2.5",
        status=200,
        response=_npc_response(
            reply=f"{private_fact}。你点头并把清单交给了国王。",
            used_fallback=True,
        ),
        calls=_npc_calls()[:1],
        elapsed_ms=250,
        isolated_port=49152,
        state_hash_before="before",
        state_hash_after="after",
    )

    assert result["passed"] is False
    assert result["checks"]["npc_voice_render_called"] is False
    assert result["checks"]["npc_voice_no_fallback"] is False
    assert result["checks"]["no_confirmed_private_fact_leak"] is False
    assert result["checks"]["no_suspected_private_surface"] is True
    assert result["confirmed_private_fact_leak_count"] == 1
    assert result["suspected_private_surface_count"] == 0
    assert result["checks"]["no_player_agency_violation"] is False
    assert private_fact not in result["reply"]


def test_npc_dialogue_marks_ambiguous_prior_knowledge_as_suspected_surface() -> None:
    surface_reply = (
        "国王压低声音。‘北境的粮车……本王知道。"
        "总督的账册，本王也看过。’"
    )

    confirmed, suspected = MODULE._npc_private_exposure_categories(surface_reply)
    result = MODULE._npc_dialogue_result(
        model="mimo-v2.5",
        status=200,
        response=_npc_response(reply=surface_reply),
        calls=_npc_calls(),
        elapsed_ms=250,
        isolated_port=49152,
        state_hash_before="before",
        state_hash_after="after",
    )

    assert confirmed == []
    assert "king_knowledge_hint_about_grain_convoy" in suspected
    assert "king_access_hint_about_governor_ledger" in suspected
    assert result["checks"]["no_confirmed_private_fact_leak"] is True
    assert result["checks"]["no_suspected_private_surface"] is False
    assert result["confirmed_private_fact_leak_count"] == 0
    assert result["suspected_private_surface_count"] == 2
    assert result["private_review_required"] is True
    assert result["passed"] is False
    assert surface_reply not in result["reply"]
    assert "总督的账册" not in result["reply"]


def test_npc_dialogue_confirms_explicit_prior_knowledge_of_withholding() -> None:
    confirmed_reply = "总督截留粮车一事，寡人此前就知晓。"

    confirmed, suspected = MODULE._npc_private_exposure_categories(
        confirmed_reply
    )

    assert confirmed == ["explicit_prior_knowledge_of_grain_withholding"]
    assert suspected == []


def test_npc_dialogue_does_not_flag_ordinary_evidence_question() -> None:
    safe_reply = "国王看着伊莉雅。‘你有证据吗？把你亲眼见到的部分说清楚。’"

    assert MODULE._npc_private_exposure_categories(safe_reply) == ([], [])
    result = MODULE._npc_dialogue_result(
        model="mimo-v2.5",
        status=200,
        response=_npc_response(reply=safe_reply),
        calls=_npc_calls(),
        elapsed_ms=250,
        isolated_port=49152,
        state_hash_before="before",
        state_hash_after="after",
    )

    assert result["checks"]["no_confirmed_private_fact_leak"] is True
    assert result["checks"]["no_suspected_private_surface"] is True
    assert result["private_review_required"] is False
    assert result["reply"] == safe_reply
    assert result["passed"] is True


@pytest.mark.parametrize(
    "safe_reply",
    (
        "总督的账册，本王没看过。",
        "总督的账册，本王未曾看过。",
        "总督的账册，本王从未看过。",
        "北境粮车之事，本王知道你刚才提过。",
        "你说本王看过总督账册？没有。",
        "总督账册，本王刚刚才看过。",
        "听你这么一说，本王知道总督扣粮了。",
        "北境粮车的公开调度，本王知道。",
    ),
)
def test_npc_dialogue_exposure_detector_ignores_non_prior_context(
    safe_reply: str,
) -> None:
    assert MODULE._npc_private_exposure_categories(safe_reply) == ([], [])


def test_failed_receipt_keeps_only_bounded_sanitized_diagnostics() -> None:
    response = _npc_response(reply="国王问：‘你有证据吗？’")
    response["tool_receipts"].append(
        {
            "tool_name": "unsafe_followup",
            "ok": False,
            "state_changed": False,
            "error_code": "FOLLOWUP_REJECTED",
            "message": (
                "无法执行，详情位于 /private/runtime/private.json；"
                "private_plan={\"secret\":\"must-not-leak\"}"
            ),
            "correction_hint": (
                "移除 private_situation={\"answer\":\"must-not-leak\"} 后重试。"
            ),
            "result": {"private_plan": "must-not-be-copied"},
        }
    )

    result = MODULE._npc_dialogue_result(
        model="mimo-v2.5",
        status=200,
        response=response,
        calls=_npc_calls(),
        elapsed_ms=250,
        isolated_port=49152,
        state_hash_before="before",
        state_hash_after="after",
    )
    failed = next(
        receipt for receipt in result["receipts"] if not receipt["ok"]
    )

    assert failed["error_code"] == "FOLLOWUP_REJECTED"
    assert failed["error_message"]
    assert failed["correction_hint"]
    assert len(failed["error_message"]) <= 300
    assert len(failed["correction_hint"]) <= 300
    assert "/Users/" not in repr(failed)
    assert "must-not-leak" not in repr(failed)
    assert "must-not-be-copied" not in repr(result)
    assert "private_plan" not in repr(failed)
    assert "private_situation" not in repr(failed)
    assert result["checks"]["no_failed_receipt"] is False


def test_cli_can_select_only_authoritative_npc_dialogue(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_fu_gm_targeted_matrix.py",
            "--models",
            "mimo-v2.5",
            "--scenarios",
            "authoritative_npc_dialogue",
        ],
    )

    args = MODULE.parse_args()

    assert args.models == ["mimo-v2.5"]
    assert args.scenarios == ["authoritative_npc_dialogue"]


def test_npc_fixture_disables_only_scene_writer_during_setup(monkeypatch) -> None:
    writer_client = object()
    voice_client = object()
    writer = SimpleNamespace(client=writer_client, model="mimo-v2.5")
    voice = SimpleNamespace(client=voice_client, model="mimo-v2.5")
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            scene_creative_writer=writer,
            npc_voice_renderer=voice,
        )
    )
    service = SimpleNamespace(_runtime=lambda _campaign_id: runtime)

    def fake_setup(
        actual_service,
        campaign_id,
        session_id,
        channel_id,
    ):
        assert actual_service is service
        assert (campaign_id, session_id, channel_id) == ("c", "s", "g")
        assert writer.client is None
        assert writer.model == ""
        assert voice.client is voice_client
        assert voice.model == "mimo-v2.5"
        return "完整fallback opening"

    monkeypatch.setattr(MODULE, "setup_scenario", fake_setup)

    opening = MODULE._setup_npc_fixture_without_scene_writer(
        service,
        "c",
        "s",
        "g",
    )

    assert opening == "完整fallback opening"
    assert writer.client is writer_client
    assert writer.model == "mimo-v2.5"
    assert voice.client is voice_client
    assert voice.model == "mimo-v2.5"


def test_npc_fixture_restores_scene_writer_when_setup_raises(monkeypatch) -> None:
    writer_client = object()
    writer = SimpleNamespace(client=writer_client, model="deepseek-v4-flash")
    runtime = SimpleNamespace(
        app=SimpleNamespace(scene_creative_writer=writer)
    )
    service = SimpleNamespace(_runtime=lambda _campaign_id: runtime)

    def failing_setup(*_args):
        assert writer.client is None
        assert writer.model == ""
        raise RuntimeError("fixture setup failed")

    monkeypatch.setattr(MODULE, "setup_scenario", failing_setup)

    with pytest.raises(RuntimeError, match="fixture setup failed"):
        MODULE._setup_npc_fixture_without_scene_writer(
            service,
            "c",
            "s",
            "g",
        )

    assert writer.client is writer_client
    assert writer.model == "deepseek-v4-flash"
