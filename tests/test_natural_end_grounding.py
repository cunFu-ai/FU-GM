from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import probe_deepseek_boss_battle as probe

from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingVerifier,
)
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService


class _NeverSemanticClient:
    config = type("Config", (), {"response_format_enabled": True})()

    def create_chat_completion(self, **_kwargs: object) -> str:
        raise AssertionError("authoritative natural conflict end must stay local")


def test_boss_fixture_natural_end_uses_real_state_builder(tmp_path: Path) -> None:
    message = (
        "权威冲突状态显示一方已经没有可行动成员。"
        "请只调用end_conflict提交自然结局。"
    )
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=119)
    runtime, _fixture = probe.build_boss_fixture(service)
    decisions = runtime.app.interceptor.decision_window_manager
    decisions.cancel_matching(
        kind="skill_parameter",
        reason="test_prepare_natural_conflict_end",
    )
    runtime.app.conflict_manager.surrender_combatant(probe.BOSS_NAME)
    runtime.app.character_manager.get(probe.MINION_NAME).hp = 0
    runtime.app.conflict_manager.resolve_zero_hp(
        probe.MINION_NAME,
        source_actor=probe.SUPPORT_HERO,
    )
    fate_window = decisions.find_pending(
        kind="npc_fate",
        owner=probe.SUPPORT_HERO,
    )
    assert fate_window is not None
    runtime.app.conflict_manager.resolve_pending_npc_fate(
        window_id=fate_window.window_id,
        responder=probe.SUPPORT_HERO,
        choice="capture",
    )
    decisions.create(
        kind="trait_invocation",
        owner=probe.SUPPORT_HERO,
        prompt="可选：援用一项特质。",
        scope_kind="conflict",
        scope_id=runtime.app.conflict_manager.state.scene_name,
        blocking=False,
        action_type="InvokeTrait",
    )
    context = GMToolExecutionContext(
        campaign_id=probe.CAMPAIGN_ID,
        session_id=probe.SESSION_ID,
        channel_id=probe.CHANNEL_ID,
        speaker="__gm__",
        gate_status="adventure",
        directly_addressed=False,
        metadata={
            "current_message": message,
            "system_gm_beat_request": True,
            "heartbeat_action": "conflict_resolution",
        },
    )

    state = service.gm_agent_message_coordinator.state_builder.build(context)
    status = state["runtime"]["conflict"]["resolution_status"]
    assert status["ready_for_natural_end"] is True
    assert status["natural_outcome"] == "hostile_side_removed"
    assert status["active_hostiles"] == []
    assert status["pending_exit_transitions"] == []
    assert status["pending_zero_hp_characters"] == []
    pending = state["processes"]["decisions"]["pending"]
    assert [item["kind"] for item in pending] == ["trait_invocation"]
    assert pending[0]["blocking"] is False

    review = GMReplyGroundingVerifier(
        _NeverSemanticClient(),
        model="unused",
    ).verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state,
        tool_name="end_conflict",
        arguments={
            "outcome": "hostile_side_removed",
            "continue_scene": True,
        },
        deadline=999999999.0,
    )

    assert review.valid is True
    assert review.category == "local_authoritative_natural_end_conflict"


def test_boss_fixture_natural_end_does_not_bypass_pending_npc_fate(
    tmp_path: Path,
) -> None:
    message = "请只调用end_conflict提交自然结局。"
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=120)
    runtime, _fixture = probe.build_boss_fixture(service)
    decisions = runtime.app.interceptor.decision_window_manager
    decisions.cancel_matching(
        kind="skill_parameter",
        reason="test_prepare_pending_fate",
    )
    runtime.app.conflict_manager.surrender_combatant(probe.BOSS_NAME)
    runtime.app.character_manager.get(probe.MINION_NAME).hp = 0
    runtime.app.conflict_manager.resolve_zero_hp(
        probe.MINION_NAME,
        source_actor=probe.SUPPORT_HERO,
    )
    context = GMToolExecutionContext(
        campaign_id=probe.CAMPAIGN_ID,
        session_id=probe.SESSION_ID,
        channel_id=probe.CHANNEL_ID,
        speaker="__gm__",
        gate_status="adventure",
        directly_addressed=False,
        metadata={
            "current_message": message,
            "system_gm_beat_request": True,
            "heartbeat_action": "conflict_resolution",
        },
    )

    state = service.gm_agent_message_coordinator.state_builder.build(context)
    pending = state["processes"]["decisions"]["pending"]
    assert any(item["kind"] == "npc_fate" for item in pending)

    class _RejectingSemanticClient:
        config = type("Config", (), {"response_format_enabled": True})()

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create_chat_completion(self, **kwargs: object) -> str:
            self.calls.append(dict(kwargs))
            return (
                '{"valid":false,"category":"gm_must_repair",'
                '"unsupported_claims":["npc_fate pending"],'
                '"correction_hint":"resolve the pending fate first"}'
            )

    client = _RejectingSemanticClient()
    review = GMReplyGroundingVerifier(client, model="semantic-model").verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state,
        tool_name="end_conflict",
        arguments={
            "outcome": "hostile_side_removed",
            "continue_scene": True,
        },
        deadline=999999999.0,
    )

    assert review.valid is False
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"
