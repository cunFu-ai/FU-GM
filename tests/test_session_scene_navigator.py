from fu_gm.components.session_scene_navigator import SessionSceneNavigator
from fu_gm.models import SessionDramaticContract, SessionSceneOpportunity


def _contract() -> SessionDramaticContract:
    return SessionDramaticContract(
        title="白花碑驿站的迟响",
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="失声的风铃墙",
                location="白花碑驿站·正门外",
            ),
            SessionSceneOpportunity(
                scene_key="social",
                scene_role="social_or_investigation",
                title="看守的旧登记册",
                location="白花碑驿站·旧储物间",
                npc_names=["白栀"],
                entry_points=["向白栀提出交换"],
            ),
            SessionSceneOpportunity(
                scene_key="alternate",
                scene_role="alternate_approach",
                title="柜台后的运单",
                location="白花碑驿站·货运台",
                entry_points=["调查运单", "启动仪式"],
            ),
            SessionSceneOpportunity(
                scene_key="climax",
                scene_role="climax_candidate",
                title="狭道对峙",
                location="白花碑驿站·风铃墙背面",
            ),
            SessionSceneOpportunity(
                scene_key="aftermath",
                scene_role="aftermath",
                title="临时庇护所",
                location="白花碑驿站·候车厅",
            ),
        ],
    )


def test_selects_functional_scene_for_each_act() -> None:
    navigator = SessionSceneNavigator()
    contract = _contract()

    assert navigator.select(contract, act_number=1).scene_key == "start"
    assert navigator.select(contract, act_number=3, used_keys={"start", "social"}).scene_key == "climax"
    assert navigator.select(contract, act_number=4, used_keys={"start", "social", "climax"}).scene_key == "aftermath"


def test_middle_scene_follows_recent_player_approach() -> None:
    navigator = SessionSceneNavigator()
    contract = _contract()

    social = navigator.select(
        contract,
        act_number=2,
        used_keys={"start"},
        recent_context="我们向白栀提出承诺，想得到明确答复。",
    )
    alternate = navigator.select(
        contract,
        act_number=2,
        used_keys={"start"},
        recent_context="洛岚调查运单，苍祈准备启动仪式。",
    )

    assert social.scene_key == "social"
    assert alternate.scene_key == "alternate"


def test_player_destination_rejects_prepared_scene_at_another_room() -> None:
    navigator = SessionSceneNavigator()
    contract = _contract()

    selected = navigator.select(
        contract,
        act_number=2,
        used_keys={"start"},
        recent_context="苍祈已经带着失名旅人从后门抵达东侧月台。",
        location_anchor="白花碑驿站·东侧月台",
    )

    assert selected is None


def test_locationless_prepared_scene_can_adopt_player_destination() -> None:
    navigator = SessionSceneNavigator()
    contract = SessionDramaticContract(
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="mobile",
                scene_role="alternate_approach",
                title="追来的风铃回声",
                location="",
            )
        ]
    )

    selected = navigator.select(
        contract,
        act_number=2,
        location_anchor="白花碑驿站·东侧月台",
    )

    assert selected is not None
    assert selected.scene_key == "mobile"


def test_anchor_matches_the_same_destination_with_a_more_specific_exit_label() -> None:
    assert SessionSceneNavigator.location_matches_anchor(
        "白花碑驿站·东侧月台",
        "白花碑驿站·东侧月台后门外",
    )
    assert not SessionSceneNavigator.location_matches_anchor(
        "白花碑驿站·候车厅登记台",
        "白花碑驿站·东侧月台后门外",
    )


def test_broad_parent_location_can_select_a_prepared_child_scene() -> None:
    navigator = SessionSceneNavigator()
    contract = _contract()

    selected = navigator.select(
        contract,
        act_number=1,
        location_anchor="白花碑驿站",
    )

    assert selected is not None
    assert selected.scene_key == "start"
    assert SessionSceneNavigator.location_matches_anchor(
        "白花碑驿站·正门外",
        "白花碑驿站",
    )


def test_infers_act_from_scene_name() -> None:
    navigator = SessionSceneNavigator()
    assert navigator.infer_act("第03场·场景3：反转与高潮") == 3
    assert navigator.infer_act("余波与收束") == 4


def test_missing_climax_or_aftermath_never_reuses_an_earlier_scene() -> None:
    navigator = SessionSceneNavigator()
    contract = SessionDramaticContract(
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="旧登记处",
            ),
            SessionSceneOpportunity(
                scene_key="middle",
                scene_role="alternate_approach",
                title="钟仓调查",
            ),
        ]
    )

    assert navigator.select(contract, act_number=3, used_keys={"start", "middle"}) is None
    assert navigator.select(contract, act_number=4, used_keys={"start", "middle"}) is None


def test_missing_middle_never_consumes_climax_or_aftermath() -> None:
    navigator = SessionSceneNavigator()
    contract = SessionDramaticContract(
        potential_scenes=[
            SessionSceneOpportunity(
                scene_key="start",
                scene_role="strong_start",
                title="风铃廊问路",
            ),
            SessionSceneOpportunity(
                scene_key="alternate",
                scene_role="alternate_approach",
                title="风铃回声仪式",
            ),
            SessionSceneOpportunity(
                scene_key="climax",
                scene_role="climax_candidate",
                title="旧路闸门与巡逻队",
            ),
            SessionSceneOpportunity(
                scene_key="aftermath",
                scene_role="aftermath",
                title="风铃下的决定",
            ),
        ]
    )

    assert navigator.select(
        contract,
        act_number=2,
        used_keys={"start", "alternate"},
    ) is None
