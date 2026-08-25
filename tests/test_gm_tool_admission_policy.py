from __future__ import annotations

import tempfile

from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Clock


def context(
    message: str = "",
    *,
    source_event_id: str = "",
    speaker: str = "阿凛",
) -> GMToolExecutionContext:
    metadata = {"current_message": message}
    if source_event_id:
        metadata["source_event_id"] = source_event_id
    return GMToolExecutionContext(
        campaign_id="admission-test",
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
        metadata=metadata,
    )


def test_blocking_decision_rejects_unrelated_clock_write() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        runtime.app.clock_manager.add(
            Clock(name="闸门开启", max_segments=6, current=1)
        )
        window = runtime.app.interceptor.decision_window_manager.create(
            kind="zero_hp",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
        )

        receipt = service.gm_tool_registry.execute(
            "fill_clock",
            {
                "name": "闸门开启",
                "amount": 1,
                "cause": "direct_action_success",
                "reason": "推进闸门",
            },
            context(),
        )

        assert not receipt.ok
        assert receipt.error_code == "BLOCKING_DECISION_PENDING"
        assert receipt.result["pending_windows"][0]["window_id"] == window.window_id
        assert runtime.app.clock_manager.get("闸门开启").current == 1


def test_campaign_save_and_safety_boundary_remain_available_during_choice() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        runtime.app.interceptor.decision_window_manager.create(
            kind="zero_hp",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
        )

        saved = service.gm_tool_registry.execute(
            "save_campaign",
            {"slot": "待决现场"},
            context(),
        )
        message = "界限：不要在游戏里出现蜘蛛。"
        safety = service.gm_tool_registry.execute(
            "record_safety_boundary",
            {
                "kind": "line",
                "content": "蜘蛛",
            },
            context(message),
        )

        assert saved.ok
        assert safety.ok
        assert "蜘蛛" in runtime.app.world_state.world_profile.safety_lines


def test_window_resolution_reaches_domain_validator_instead_of_global_block() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        runtime.app.interceptor.decision_window_manager.create(
            kind="zero_hp",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
        )

        receipt = service.gm_tool_registry.execute(
            "resolve_rule_window",
            {
                "action_type": "ResolveZeroHP",
                "actor": "伊莉雅",
                "window_id": "不存在",
                "choice": "放弃抵抗",
                "details": {},
            },
            context("伊莉雅选择放弃抵抗。"),
        )

        assert not receipt.ok
        assert receipt.error_code != "BLOCKING_DECISION_PENDING"


def test_same_event_passive_npc_reply_is_not_blocked_by_new_check_window() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        source_event_id = "message:admission-test:group-1:m-1"
        runtime.app.interceptor.decision_window_manager.create(
            kind="check_roll_confirmation",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
            payload={"source_event_id": source_event_id},
        )

        receipt = service.gm_tool_registry.execute(
            "decide_npc_response",
            {
                "name": "尚未建档的人",
                "actor": "伊莉雅",
                "public_segments": [
                    {"text": "他点了点头。", "tags": ["nonverbal"]},
                ],
                "speech_act": "answer",
                "condition_outcome": "none",
                "proposal_outcome": "none",
            },
            context("伊莉雅提醒他先别出声。", source_event_id=source_event_id),
        )

        assert not receipt.ok
        assert receipt.error_code == "NPC_PROFILE_REQUIRED"


def test_same_event_npc_reply_cannot_open_new_gate_during_check_window() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        source_event_id = "message:admission-test:group-1:m-2"
        runtime.app.interceptor.decision_window_manager.create(
            kind="check_roll_confirmation",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
            payload={"source_event_id": source_event_id},
        )

        receipt = service.gm_tool_registry.execute(
            "decide_npc_response",
            {
                "name": "尚未建档的人",
                "actor": "伊莉雅",
                "public_segments": [
                    {"text": "先替我办一件事。", "tags": ["gate_requirement"]},
                ],
                "speech_act": "new_gate",
            },
            context("伊莉雅向他问路。", source_event_id=source_event_id),
        )

        assert not receipt.ok
        assert receipt.error_code == "BLOCKING_DECISION_PENDING"


def test_foreign_player_npc_reply_is_not_globally_blocked_by_roll_confirmation() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        service._player_character_control_map = lambda _runtime: {
            "阿凛": ["伊莉雅"],
            "loading": ["伊大石"],
        }
        runtime.app.interceptor.decision_window_manager.create(
            kind="check_roll_confirmation",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
            payload={"source_event_id": "message:group-1:m-earlier"},
        )

        receipt = service.gm_tool_registry.execute(
            "decide_collective_response",
            {
                "collective_name": "双方巡逻队",
                "addressed_actor": "伊大石",
                "public_segments": [
                    {"text": "两边都没有立刻放下武器。", "tags": ["fact"]},
                ],
                "speech_act": "answer",
                "condition_outcome": "none",
                "proposal_outcome": "none",
            },
            context(
                "伊大石请双方暂且停火。",
                source_event_id="message:group-1:m-later",
                speaker="loading",
            ),
        )

        assert receipt.error_code != "BLOCKING_DECISION_PENDING"


def test_roll_owner_cannot_bypass_own_confirmation_with_npc_reply() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        service._player_character_control_map = lambda _runtime: {
            "阿凛": ["伊莉雅"],
        }
        runtime.app.interceptor.decision_window_manager.create(
            kind="check_roll_confirmation",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
            payload={"source_event_id": "message:group-1:m-earlier"},
        )

        receipt = service.gm_tool_registry.execute(
            "decide_npc_response",
            {
                "name": "尚未建档的人",
                "actor": "伊莉雅",
                "public_segments": [
                    {"text": "他没有回答。", "tags": ["nonverbal"]},
                ],
                "speech_act": "answer",
                "condition_outcome": "none",
                "proposal_outcome": "none",
            },
            context(
                "伊莉雅继续追问。",
                source_event_id="message:group-1:m-later",
                speaker="阿凛",
            ),
        )

        assert receipt.error_code == "BLOCKING_DECISION_PENDING"


def test_gm_cannot_resolve_window_created_by_same_player_message() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("admission-test")
        source_event_id = "message:admission-test:group-1:m-3"
        window = runtime.app.interceptor.decision_window_manager.create(
            kind="check_roll_confirmation",
            owner="伊莉雅",
            blocking=True,
            allowed_responders=["伊莉雅"],
            payload={"source_event_id": source_event_id},
        )

        receipt = service.gm_tool_registry.execute(
            "resolve_rule_window",
            {
                "action_type": "ResolveDecision",
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "roll",
                "details": {},
            },
            context("伊莉雅检查门闩。", source_event_id=source_event_id),
        )

        assert not receipt.ok
        assert receipt.error_code == "PLAYER_CONFIRMATION_REQUIRES_NEW_MESSAGE"
        assert runtime.app.interceptor.decision_window_manager.get(
            window.window_id
        ).status.value == "pending"
