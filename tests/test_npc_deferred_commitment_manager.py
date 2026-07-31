from __future__ import annotations

from fu_gm.components.npc_deferred_commitment_manager import (
    NPCDeferredCommitmentManager,
)
from fu_gm.components.scene_frame_manager import SceneFrame, SceneFrameManager


def test_structured_deferred_npc_commitment_is_persisted_and_resolved() -> None:
    frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")

    recorded = NPCDeferredCommitmentManager.update_from_public_answer(
        frame,
        npc="白花巡守",
        public_statement="我去通报会长，有答复就回来告诉你们。",
        speech_plan={
            "deferred_action": "通报白花守望会会长",
            "deferred_result": "把会长的答复告诉英雄",
            "deferred_trigger": "通报完成后",
        },
    )

    assert recorded is not None
    assert recorded["trigger_status"] == "waiting"
    assert NPCDeferredCommitmentManager.pending(frame) == [recorded]

    resolved = NPCDeferredCommitmentManager.resolve_from_public_answer(
        frame,
        npc="白花巡守",
        public_statement="会长的答复是：旧路可以借，但由巡守带路。",
        speech_plan={
            "commitment_id": recorded["commitment_id"],
            "commitment_outcome": "fulfilled",
        },
    )

    assert resolved == [recorded]
    assert NPCDeferredCommitmentManager.pending(frame) == []
    assert recorded["trigger_status"] == "fulfilled"


def test_exact_deferred_commitment_trigger_is_recorded_without_resolving_it() -> None:
    frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")
    recorded = NPCDeferredCommitmentManager.record_from_public_answer(
        frame,
        npc="白花守望会会长",
        public_statement="巡守会在旧路入口等你们，并在那里带路。",
        speech_plan={
            "deferred_action": "白花巡守前往旧路入口并在那里带路",
            "deferred_result": "在旧路入口为队伍带路",
            "deferred_trigger": "队伍抵达旧路入口",
        },
    )

    triggered = NPCDeferredCommitmentManager.mark_trigger_reached(
        frame,
        commitment_id=recorded["commitment_id"],
        actor="苍祈",
        evidence="苍祈跟上白花巡守，向旧路入口前进",
        location="旧路入口",
        responder="白花巡守",
    )

    assert triggered is recorded
    assert recorded["status"] == "pending"
    assert recorded["trigger_status"] == "reached"
    assert recorded["triggered_by"] == "苍祈"
    assert recorded["trigger_responder"] == "白花巡守"
    assert NPCDeferredCommitmentManager.find_pending(
        frame,
        recorded["commitment_id"],
    ) is recorded
    assert (
        NPCDeferredCommitmentManager.mark_trigger_reached(
            frame,
            commitment_id=recorded["commitment_id"],
            actor="苍祈",
            evidence="重复移动",
            location="旧路入口",
            responder="白花巡守",
        )
        is None
    )


def test_legacy_explicit_report_promise_is_tracked_but_not_self_resolved() -> None:
    frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")
    statement = (
        "我现在就把你们的请求通报会长；"
        "会长有答复，我会当面告诉你们。"
    )

    recorded = NPCDeferredCommitmentManager.update_from_public_answer(
        frame,
        npc="白花巡守",
        public_statement=statement,
    )

    assert recorded is not None
    assert "通报会长" in recorded["action"]
    assert NPCDeferredCommitmentManager.pending(frame) == [recorded]
    assert NPCDeferredCommitmentManager.resolve_from_public_answer(
        frame,
        npc="白花巡守",
        public_statement=statement,
    ) == []


def test_scene_packet_exposes_only_pending_npc_commitments() -> None:
    manager = SceneFrameManager()
    manager.current_frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")
    pending = NPCDeferredCommitmentManager.record_from_public_answer(
        manager.current_frame,
        npc="白花巡守",
        public_statement="我去请示，会带回答复。",
        speech_plan={
            "deferred_action": "请示会长",
            "deferred_result": "带回会长答复",
            "deferred_trigger": "请示完成后",
        },
    )
    resolved = NPCDeferredCommitmentManager.record_from_public_answer(
        manager.current_frame,
        npc="钟匠",
        public_statement="我去取钥匙。",
        speech_plan={
            "deferred_action": "取来旧钥匙",
            "deferred_result": "把旧钥匙交给英雄",
            "deferred_trigger": "找到钥匙后",
        },
    )
    assert pending is not None and resolved is not None
    resolved["status"] = "resolved"

    packet = manager.expression_packet(include_private=True)

    assert packet["pending_npc_commitments"] == [pending]
    assert "NPC已经公开答应、尚待履行" in manager.format_for_prompt(include_private=True)


def test_luna_semantic_update_resolves_non_formulaic_public_fulfilment() -> None:
    frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")
    recorded = NPCDeferredCommitmentManager.record_from_public_answer(
        frame,
        npc="失忆旅人",
        public_statement="下一次金属短响传来时，我留意身体的反应再告诉你。",
        speech_plan={
            "deferred_action": "留意下一次金属短响带来的本能反应",
            "deferred_result": "直接告诉艾薇娅最先出现的本能反应",
            "deferred_trigger": "下一次金属短响传来",
        },
    )
    assert recorded is not None
    beat = "失忆旅人说：‘第一下是转头，然后才想退开。’"

    resolved = NPCDeferredCommitmentManager.apply_semantic_updates(
        frame,
        public_beat=beat,
        updates=[
            {
                "commitment_id": recorded["commitment_id"],
                "npc": "失忆旅人",
                "outcome": "fulfilled",
                "evidence": "第一下是转头，然后才想退开。",
            }
        ],
    )

    assert resolved == [recorded]
    assert recorded["status"] == "resolved"
    assert recorded["resolution_source"] == "luna_scene_semantics"
    assert NPCDeferredCommitmentManager.pending(frame) == []


def test_luna_semantic_update_cannot_clear_another_npcs_commitment() -> None:
    frame = SceneFrame(scene_key="风铃廊", scene_name="风铃廊")
    recorded = NPCDeferredCommitmentManager.record_from_public_answer(
        frame,
        npc="失忆旅人",
        public_statement="下一次金属短响传来时，我留意身体的反应再告诉你。",
        speech_plan={
            "deferred_action": "留意下一次金属短响带来的本能反应",
            "deferred_result": "直接告诉艾薇娅最先出现的本能反应",
            "deferred_trigger": "下一次金属短响传来",
        },
    )
    assert recorded is not None

    resolved = NPCDeferredCommitmentManager.apply_semantic_updates(
        frame,
        public_beat="白花巡守说：‘第一下是转头。’",
        updates=[
            {
                "commitment_id": recorded["commitment_id"],
                "npc": "白花巡守",
                "outcome": "fulfilled",
                "evidence": "第一下是转头。",
            }
        ],
    )

    assert resolved == []
    assert NPCDeferredCommitmentManager.pending(frame) == [recorded]

