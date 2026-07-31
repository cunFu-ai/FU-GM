from fu_gm.components.scene_access_boundary import SceneAccessBoundary
from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.models import ActionType, SceneRecord, SceneType


def _scene() -> SceneRecord:
    return SceneRecord(
        name="白花碑驿站的迟响",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        objective="说服白花守望会开放旧路，护送失忆旅人离开",
    )


def test_scene_access_boundary_blocks_crossing_a_route_still_required_by_objective() -> None:
    boundary = SceneAccessBoundary()
    frame = SceneFrame(
        scene_key="白花碑驿站",
        scene_name="白花碑驿站的迟响",
        dramatic_question="英雄能否说服守望会开放旧路？",
    )

    action = boundary.guard_action(
        "南星: 赛璃沿旧路走到转折处，停下来查看前方。",
        frame=frame,
        scene=_scene(),
    )

    assert action is not None
    assert action.action_type == ActionType.NARRATE
    assert action.parameters["scene_access_blocked"] is True
    assert action.parameters["blocked_route"] == "旧路"
    assert "不要" not in action.parameters["summary"]
    assert "只描述" not in action.parameters["summary"]
    assert "后台" not in action.parameters["summary"]


def test_resolved_route_condition_allows_crossing() -> None:
    boundary = SceneAccessBoundary()
    scene = _scene()
    frame = SceneFrame(
        scene_key="白花碑驿站",
        scene_name="白花碑驿站的迟响",
        open_conditions=[
            {
                "condition_id": "gate-1",
                "condition": "留下担保后开放旧路",
                "promised_result": "准许使用旧路",
                "promise_subject": "旧路",
                "status": "resolved",
            }
        ],
        public_facts=["守望会已经兑现承诺：准许使用旧路。"],
    )

    review = boundary.review(
        "南星: 赛璃沿旧路走到转折处，停下来查看前方。",
        frame=frame,
        scene=scene,
    )

    assert review.blocked is False


def test_unrelated_movement_is_not_blocked_by_a_different_gate() -> None:
    boundary = SceneAccessBoundary()
    frame = SceneFrame(
        scene_key="白花碑驿站",
        scene_name="白花碑驿站的迟响",
        open_conditions=[
            {
                "condition_id": "gate-1",
                "condition": "留下担保后开放旧路",
                "promised_result": "准许使用旧路",
                "promise_subject": "旧路",
                "status": "open",
            }
        ],
    )

    review = boundary.review(
        "伊莉雅沿风铃墙走到柜台前。",
        frame=frame,
        scene=_scene(),
    )

    assert review.blocked is False


def test_unmentioned_named_route_cannot_be_invented_as_an_arrived_location() -> None:
    boundary = SceneAccessBoundary()
    scene = SceneRecord(
        name="风铃墙失声的早市",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站正门外",
        objective="弄清空白收据与失声风铃的关系",
    )
    frame = SceneFrame(
        scene_key="风铃墙早市",
        scene_name=scene.name,
        location=scene.location,
        visible_elements=["白瓷风铃", "收购柜台", "候车厅门口"],
    )

    review = boundary.review(
        "阿凛: 伊莉雅沿旧路走到转折处，停下来查看前方。",
        frame=frame,
        scene=scene,
    )

    assert review.blocked is True
    assert review.route == "旧路"


def test_publicly_established_ungated_route_can_be_crossed() -> None:
    boundary = SceneAccessBoundary()
    scene = SceneRecord(
        name="驿站后门",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站",
        summary="岑烛刚指出后门通往卸货小巷。",
    )
    frame = SceneFrame(
        scene_key="驿站后门",
        scene_name=scene.name,
        location=scene.location,
        public_facts=["后门敞开着，外面就是卸货小巷。"],
    )

    review = boundary.review(
        "伊莉雅穿过后门走进卸货小巷。",
        frame=frame,
        scene=scene,
    )

    assert review.blocked is False


def test_route_detector_does_not_join_a_future_route_phrase_to_a_later_move() -> None:
    text = (
        "赛璃对会长说：‘我负责带他沿你们巡守引领的旧路前行。’"
        "说完，她走到失名旅人身侧，示意他跟紧自己。"
    )

    assert SceneAccessBoundary._crossed_route(text) == ""
