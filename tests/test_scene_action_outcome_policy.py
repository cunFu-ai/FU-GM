from __future__ import annotations

from fu_gm.components.scene_action_outcome_policy import SceneActionOutcomePolicy
from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.models import Action, ActionType


def _route() -> dict[str, object]:
    return {
        "commitment": "action",
        "world_response_required": True,
        "interaction_kind": "manipulate",
    }


def test_committed_pressure_action_that_only_echoes_player_requires_replan() -> None:
    policy = SceneActionOutcomePolicy()
    review = policy.review(
        Action(ActionType.NARRATE, {"summary": "洛岚走到门口，把能透进来的视线挡住。"}),
        route_decision=_route(),
        player_message="白河：洛岚走到门口，把能透进来的视线先挡住。",
        frame=SceneFrame(scene_key="驿站", scene_name="驿站", current_pressure="巡逻队正在撞门。"),
        has_active_pressure=True,
    )

    assert review.replan_required
    assert "难度等级" in review.instruction
    assert "程序默认难度" in review.instruction


def test_concrete_committed_narration_is_already_an_outcome() -> None:
    policy = SceneActionOutcomePolicy()
    review = policy.review(
        Action(
            ActionType.NARRATE,
            {
                "summary": "木柜被推入门槽，门外第一下撞击只震落了两片白漆。",
                "establish_fact": True,
                "consume_turn": True,
            },
        ),
        route_decision=_route(),
        player_message="白河：洛岚把木柜推到门前。",
        frame=SceneFrame(scene_key="驿站", scene_name="驿站"),
        has_active_pressure=True,
    )

    assert not review.replan_required


def test_policy_never_replans_player_table_proposal() -> None:
    policy = SceneActionOutcomePolicy()
    review = policy.review(
        Action(ActionType.NARRATE, {"summary": "大家还在商量。"}),
        route_decision={
            "commitment": "proposal",
            "world_response_required": False,
            "interaction_kind": "table",
        },
        player_message="白河：要不我去守门？",
        frame=SceneFrame(scene_key="驿站", scene_name="驿站"),
        has_active_pressure=True,
    )

    assert not review.replan_required


def test_generic_scene_opening_flag_cannot_authorize_committed_action_placeholder() -> None:
    policy = SceneActionOutcomePolicy()
    review = policy.review(
        Action(
            ActionType.NARRATE,
            {
                "summary": "镜头打开：地点、人物和压力同时浮出，等英雄把第一步行动落进去。",
                "scene_open_request": True,
            },
        ),
        route_decision=_route(),
        player_message="白河：洛岚把灰粉装进玻璃管并封好。",
        frame=SceneFrame(scene_key="驿站", scene_name="驿站"),
        has_active_pressure=False,
    )

    assert review.replan_required
    assert review.reason == "committed_action_was_replaced_by_scene_opening_placeholder"
