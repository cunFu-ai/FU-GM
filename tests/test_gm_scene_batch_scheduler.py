from __future__ import annotations

from fu_gm.components.gm_scene_batch_scheduler import GMSceneBatchScheduler


def _scene_state(*participants: str) -> dict[str, object]:
    return {
        "scene": {
            "name": "白花碑驿站",
            "location": "风铃廊",
            "participants": list(participants),
        }
    }


def test_batch_scheduler_preserves_explicit_tool_order() -> None:
    calls = [
        {
            "tool_name": "move_scene_group",
            "arguments": {"actor": "伊莉雅", "destination": "旧路闸门内侧"},
        },
        {
            "tool_name": "decide_npc_response",
            "arguments": {"name": "白花守望会会长"},
        },
    ]
    schedule = GMSceneBatchScheduler.schedule(
        calls,
        observed_state=_scene_state("伊莉雅", "白花守望会会长"),
    )

    assert schedule.reordered is False
    assert schedule.execution_order == (
        "move_scene_group",
        "decide_npc_response",
    )
    assert calls[0]["tool_name"] == "move_scene_group"


def test_batch_scheduler_returns_a_detached_call_copy() -> None:
    calls = [
        {
            "tool_name": "move_scene_group",
            "arguments": {"actor": "伊莉雅", "destination": "钟楼"},
        },
        {
            "tool_name": "decide_npc_response",
            "arguments": {"name": "钟楼守卫"},
        },
    ]
    schedule = GMSceneBatchScheduler.schedule(
        calls,
        observed_state=_scene_state("伊莉雅", "钟楼守卫"),
    )

    assert schedule.reordered is False
    assert schedule.execution_order == (
        "move_scene_group",
        "decide_npc_response",
    )
    assert schedule.calls[0] is not calls[0]
