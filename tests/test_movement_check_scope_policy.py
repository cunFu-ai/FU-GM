from fu_gm.components.movement_check_scope_policy import MovementCheckScopePolicy


def review(**overrides: object):
    arguments: dict[str, object] = {
        "source_message": "我贴着墙穿过断桥，走到祭坛前的平台。",
        "evidence": "贴着墙穿过断桥，走到祭坛前的平台",
        "actor": "洛岚",
        "destination": "沉星遗迹·祭坛前平台",
        "obstacle": "断桥上不断坠落的碎石",
        "purpose": "穿过断桥抵达祭坛前平台",
        "success_observation": "洛岚穿过断桥，抵达沉星遗迹·祭坛前平台。",
        "failure_consequence": "碎石砸在断桥边缘，洛岚只能停在原地。",
        "resolution_mode": "single_obstacle",
        "known_player_characters": ("洛岚", "艾菲"),
    }
    arguments.update(overrides)
    return MovementCheckScopePolicy.validate(**arguments)


def test_explicit_adjacent_landing_across_one_ruin_obstacle_is_valid() -> None:
    result = review()

    assert result.valid, result


def test_route_search_cannot_be_expanded_to_remote_mansion_room() -> None:
    result = review(
        source_message="我沿东侧回廊往前走，找找通往楼上的路。",
        evidence="沿东侧回廊往前走，找找通往楼上的路",
        destination="灰棘宅邸·三楼主卧",
        obstacle="尚未查明的楼梯与沿途巡查",
        purpose="穿过回廊找到楼梯并抵达三楼主卧",
        success_observation="洛岚抵达灰棘宅邸·三楼主卧。",
        failure_consequence="洛岚没能绕开巡查，只得停下。",
    )

    assert not result.valid
    assert result.error_code == "EXPLORATION_EXPANDED_TO_ARRIVAL"


def test_directional_ruin_exploration_cannot_commit_the_inner_sanctum() -> None:
    result = review(
        source_message="我朝石梯下方摸索，看看哪边通往遗迹深处。",
        evidence="朝石梯下方摸索，看看哪边通往遗迹深处",
        destination="沉星遗迹·内圣所",
        obstacle="尚未查明的通路与中间区域",
        purpose="穿过地下区域抵达内圣所",
        success_observation="洛岚抵达沉星遗迹·内圣所。",
        failure_consequence="洛岚在石梯下方迷失方向，只能停在原地。",
    )

    assert not result.valid
    assert result.error_code == "EXPLORATION_EXPANDED_TO_ARRIVAL"


def test_abstract_journey_requires_literal_player_authorization() -> None:
    result = review(
        source_message="我出发去银风驿站。",
        evidence="出发去银风驿站",
        destination="银风驿站",
        obstacle="整段山路的暴雨和泥泞",
        purpose="穿过暴雨山路抵达银风驿站",
        success_observation="洛岚抵达银风驿站。",
        failure_consequence="暴雨拖慢了行程，洛岚没能抵达银风驿站。",
        resolution_mode="abstract_journey",
    )

    assert not result.valid
    assert result.error_code == "ABSTRACT_JOURNEY_NOT_AUTHORIZED"


def test_literal_one_pass_request_authorizes_abstract_journey() -> None:
    result = review(
        source_message="这段山路一口气结算到银风驿站吧。",
        evidence="这段山路一口气结算到银风驿站",
        destination="银风驿站",
        obstacle="整段山路的暴雨和泥泞",
        purpose="穿过暴雨山路抵达银风驿站",
        success_observation="洛岚抵达银风驿站。",
        failure_consequence="暴雨拖慢了行程，洛岚没能抵达银风驿站。",
        resolution_mode="abstract_journey",
    )

    assert result.valid, result


def test_leaving_one_room_does_not_authorize_arrival_outside_the_city() -> None:
    result = review(
        source_message="我离开客房，往外走。",
        evidence="离开客房，往外走",
        destination="鹰尾城外的安全地带",
        obstacle="从客房到城门的所有巡查与关卡",
        purpose="穿过建筑和城区抵达城外",
        success_observation="洛岚抵达鹰尾城外的安全地带。",
        failure_consequence="洛岚未能穿过眼前关卡，只能停下。",
    )

    assert not result.valid
    assert result.error_code == "MOVEMENT_DESTINATION_OUTRUNS_INTENT"


def test_generic_step_outside_can_use_an_adjacent_unqualified_landing() -> None:
    result = review(
        source_message="我离开客房，往外走。",
        evidence="离开客房，往外走",
        destination="门外",
        obstacle="门口垂落的破损风钟",
        purpose="绕开风钟走到门外",
        success_observation="洛岚绕开风钟，抵达门外。",
        failure_consequence="洛岚被风钟挡住，只能停在原地。",
    )

    assert result.valid, result


def test_failure_cannot_expand_one_mansion_door_into_a_party_wide_lockout() -> None:
    result = review(
        source_message="我趁巡查转身，穿过书房侧门进入东回廊。",
        evidence="穿过书房侧门进入东回廊",
        destination="灰棘宅邸·东回廊",
        obstacle="书房侧门前的巡查",
        purpose="绕开巡查进入东回廊",
        success_observation="洛岚穿过侧门，抵达灰棘宅邸·东回廊。",
        failure_consequence="整座宅邸的全部出口封死，所有人都被困住。",
    )

    assert not result.valid
    assert result.error_code == "MOVEMENT_FAILURE_EXCEEDS_OBSTACLE"


def test_failure_cannot_revoke_a_previously_committed_ruin_route() -> None:
    result = review(
        failure_consequence="此前已查明的侧廊路线彻底失效。",
    )

    assert not result.valid
    assert result.error_code == "MOVEMENT_FAILURE_REVOKES_COMMITTED_RESULT"


def test_failure_cannot_move_or_trap_an_uninvolved_player_character() -> None:
    result = review(
        failure_consequence="碎石挡住断桥，艾菲也被困在对岸。",
    )

    assert not result.valid
    assert result.error_code == "MOVEMENT_FAILURE_AFFECTS_UNINVOLVED_PC"
