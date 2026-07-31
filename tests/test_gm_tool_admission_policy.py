from __future__ import annotations

import tempfile

from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Clock


def context(message: str = "") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="admission-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={"current_message": message},
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
            "change_clock",
            {
                "name": "闸门开启",
                "delta": 1,
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
