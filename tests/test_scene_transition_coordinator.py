from fu_gm.components.scene_transition_coordinator import SceneTransitionCoordinator
from fu_gm.models import Action, ActionResolution, ActionType, SceneRecord, SceneType


def _route() -> dict[str, object]:
    return {
        "performed_action": True,
        "table_proposal_only": False,
        "movement_scope": "cross_scene",
        "movement_destination": "白花碑驿站·东侧月台",
        "movement_companions": ["失名旅人"],
        "action_summary": "苍祈带着失名旅人穿过后门",
    }


def _resolution(**parameters: object) -> ActionResolution:
    return ActionResolution(
        Action(ActionType.NARRATE, {"material_change": True, **parameters}),
        "",
        {},
    )


def test_confirmed_cross_scene_move_becomes_authoritative_anchor() -> None:
    scene = SceneRecord(
        name="候车厅",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站·候车厅",
    )

    route = _route()
    route["movement_companions"] = []
    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=route,
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="白花碑驿站·东侧月台",
        ),
        public_reply="门外没有拦截，苍祈与旅人已经抵达白花碑驿站·东侧月台。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is not None
    assert anchor.location == "白花碑驿站·东侧月台"
    assert anchor.participants == ("苍祈",)
    assert scene.location == "白花碑驿站·东侧月台"
    assert SceneTransitionCoordinator.anchor_for_scene(scene) == anchor


def test_confirmed_move_uses_canonical_resolved_participants() -> None:
    scene = SceneRecord(
        name="候车厅",
        scene_type=SceneType.STANDARD,
        location="白花碑驿站·候车厅",
    )

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="白花碑驿站·东侧月台",
            resolved_movement_participants=["苍祈", "失忆旅人"],
        ),
        public_reply="苍祈与失忆旅人已经抵达白花碑驿站·东侧月台。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is not None
    assert anchor.participants == ("苍祈", "失忆旅人")
    assert "失名旅人" not in scene.pending_transition_participants


def test_nonempty_npc_reply_cannot_commit_an_unresolved_cross_scene_move() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(),
        public_reply="失名旅人说：我得先看看门外。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"
    assert scene.pending_transition_location == ""


def test_publicly_confirmed_arrival_with_structured_result_can_commit() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    route = _route()
    route["movement_companions"] = []
    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=route,
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="白花碑驿站·东侧月台",
        ),
        public_reply="队伍沿旧栈桥顺利抵达白花碑驿站·东侧月台，旅人也跟在苍祈身后。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is not None
    assert anchor.location == "白花碑驿站·东侧月台"
    assert scene.location == "白花碑驿站·东侧月台"


def test_structured_destination_does_not_require_database_label_in_public_prose() -> None:
    scene = SceneRecord("城门", SceneType.STANDARD, location="第七采掘城")
    route = _route()
    route["movement_destination"] = "静默图书馆"
    route["movement_companions"] = []

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=route,
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="静默图书馆",
        ),
        public_reply="洛岚推开半掩的大门，馆内的旧索引柜映入眼帘。",
        scene=scene,
        actor="洛岚",
    )

    assert anchor is not None
    assert anchor.location == "静默图书馆"


def test_structured_arrival_without_resolved_companions_cannot_move_an_npc() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="白花碑驿站·东侧月台",
        ),
        public_reply="苍祈与旅人已经抵达白花碑驿站·东侧月台。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"


def test_permission_to_go_somewhere_is_not_a_public_arrival() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(),
        public_reply="会长点头：你们可以前往白花碑驿站·东侧月台，但先别离开候车厅。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"


def test_npc_dialogue_cannot_use_its_own_arrival_wording_to_move_heroes() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(npc_answer_generated=True),
        public_reply="失名旅人说：我曾经抵达白花碑驿站·东侧月台，但不是现在。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"


def test_wrong_landing_point_cannot_commit_an_unresolved_cross_scene_move() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(
            movement_resolved=True,
            resolved_movement_destination="白花碑驿站·西侧仓房",
        ),
        public_reply="苍祈与旅人已经抵达西侧仓房。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"


def test_empty_world_reply_cannot_commit_a_transition() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(),
        public_reply="",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.location == "白花碑驿站·候车厅"
    assert scene.pending_transition_location == ""


def test_blocked_route_cannot_commit_a_transition() -> None:
    scene = SceneRecord("候车厅", SceneType.STANDARD, location="白花碑驿站·候车厅")

    anchor = SceneTransitionCoordinator.observe_turn(
        route_decision=_route(),
        resolution=_resolution(scene_access_blocked=True),
        public_reply="后门仍被锁住。",
        scene=scene,
        actor="苍祈",
    )

    assert anchor is None
    assert scene.pending_transition_location == ""
