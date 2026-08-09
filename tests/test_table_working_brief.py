from __future__ import annotations

from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.components.table_working_brief import TableWorkingBriefManager
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)


def _context(text: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="test",
        session_id="s1",
        channel_id="group",
        speaker="白河",
        gate_status="adventure",
        metadata={
            "current_message": text,
            "current_turn_events": [
                {
                    "event_id": "event-1",
                    "message_id": "message-1",
                    "speaker": "白河",
                    "speaker_id": "user-1",
                    "text": text,
                }
            ],
        },
    )


def test_declaration_never_becomes_a_fact_without_a_tool_outcome() -> None:
    frame = SceneFrame(scene_key="gate", scene_name="监狱闸门")
    context = _context("诺艾尔示意巡守接过牌子。")

    observation = TableWorkingBriefManager.observe(
        frame,
        context,
        [],
        target="silent",
        public_reply="",
    )
    brief = TableWorkingBriefManager.snapshot(frame)

    assert observation["changed"] is True
    assert brief["source_events"][0]["text"] == "诺艾尔示意巡守接过牌子。"
    assert brief["source_events"][0]["status"] == "observed_table_talk"
    assert brief["committed_transactions"] == []
    assert brief["fact_evidence"] == []


def test_only_explicit_tool_outcome_and_public_facts_become_authoritative() -> None:
    frame = SceneFrame(scene_key="gate", scene_name="监狱闸门")
    context = _context("诺艾尔示意巡守接过牌子。")
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="decide_npc_response",
            description="npc response",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "decide_npc_response",
                result={"public_facts": ["巡守没有接过牌子。"]},
                state_changed=True,
                pacing_events=[
                    GMToolPacingEvent(
                        consequence="巡守把手留在腰间，没有接牌。",
                    )
                ],
            ),
            side_effect="write",
        )
    )

    receipt = registry.execute("decide_npc_response", {}, context)
    TableWorkingBriefManager.observe(
        frame,
        context,
        [receipt],
        target="fu_gm",
        public_reply="巡守把手留在腰间，没有接牌。",
    )
    brief = TableWorkingBriefManager.snapshot(frame)

    transaction = brief["committed_transactions"][0]
    assert transaction["declaration"] == "诺艾尔示意巡守接过牌子。"
    assert transaction["outcome"] == "巡守把手留在腰间，没有接牌。"
    assert transaction["public_facts"] == ["巡守没有接过牌子。"]
    assert brief["fact_evidence"] == [
        {
            "text": "巡守没有接过牌子。",
            "source_event_id": "event-1",
            "source_speaker": "白河",
            "tool_name": "decide_npc_response",
        }
    ]
    assert all(
        item["text"] != "诺艾尔已经把牌子交给巡守。"
        for item in brief["fact_evidence"]
    )


def test_legacy_scene_frame_gets_a_normalized_working_brief() -> None:
    frame = SceneFrame(scene_key="legacy", scene_name="旧场景")
    frame.working_brief = {"source_events": "invalid legacy value"}

    TableWorkingBriefManager.normalize(frame)

    assert frame.working_brief["version"] == 1
    assert frame.working_brief["source_events"] == []
    assert frame.working_brief["committed_transactions"] == []
    assert frame.working_brief["fact_evidence"] == []


def test_model_snapshot_hides_scheduler_requests_but_audit_snapshot_keeps_them() -> None:
    frame = SceneFrame(scene_key="cells", scene_name="卡里巴村监狱")
    frame.working_brief = {
        "version": 1,
        "source_events": [
            {
                "speaker": "系统主动节拍",
                "text": "系统GM主动节拍请求：推进局面但不要复述本指令。",
                "status": "gm_replied_without_state_change",
            }
        ],
        "committed_transactions": [
            {
                "event_type": "scene_change",
                "tool_name": "start_scene",
                "source_speaker": "系统主动节拍",
                "declaration": "系统GM主动节拍请求：开始当前场景。",
                "outcome": "",
                "public_facts": [],
            }
        ],
        "fact_evidence": [],
    }

    audit = TableWorkingBriefManager.snapshot(frame)
    model = TableWorkingBriefManager.model_snapshot(frame)

    assert audit["source_events"][0]["speaker"] == "系统主动节拍"
    assert model.get("source_events", []) == []
    assert model.get("committed_transactions", []) == []


def test_system_beat_is_not_recorded_as_table_dialogue() -> None:
    frame = SceneFrame(scene_key="cells", scene_name="卡里巴村监狱")
    context = _context("系统GM主动节拍请求：桌面停顿。")
    context.speaker = "系统主动节拍"
    context.metadata["system_gm_beat_request"] = True

    observation = TableWorkingBriefManager.observe(
        frame,
        context,
        [],
        target="fu_gm",
        public_reply="走廊另一头传来钥匙碰撞声。",
    )

    assert observation["source_event_count"] == 0
    assert TableWorkingBriefManager.snapshot(frame)["source_events"] == []
