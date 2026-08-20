from fu_gm.components.check_success_effect_policy import CheckSuccessEffectPolicy


def review(**overrides: object):
    arguments: dict[str, object] = {
        "action_type": "RequestRoll",
        "actor": "洛岚",
        "success_observation": "铜质锁舌缩回门体，书房侧门已经可以推开。",
        "has_success_transition": False,
    }
    arguments.update(overrides)
    return CheckSuccessEffectPolicy.validate(**arguments)


def test_local_mansion_lock_operation_remains_a_plain_success() -> None:
    result = review()

    assert result.valid, result


def test_remote_mansion_arrival_requires_a_structured_transition() -> None:
    result = review(
        success_observation="洛岚避开巡查，抵达灰棘宅邸的三楼主卧。",
    )

    assert not result.valid
    assert result.error_code == "CHECK_SUCCESS_TRANSITION_UNCOMMITTED"


def test_implicit_bridge_arrival_requires_a_structured_transition() -> None:
    result = review(
        success_observation="穿过吊桥，实际抵达对岸石台。",
    )

    assert not result.valid
    assert result.error_code == "CHECK_SUCCESS_TRANSITION_UNCOMMITTED"


def test_bridge_arrival_is_valid_when_transition_is_structured() -> None:
    result = review(
        success_observation="洛岚穿过吊桥，实际抵达对岸石台。",
        has_success_transition=True,
    )

    assert result.valid, result


def test_route_investigation_can_reveal_an_entrance_without_moving() -> None:
    result = review(
        action_type="Investigate",
        success_observation="洛岚确认东侧楼梯可以进入三楼回廊。",
    )

    assert result.valid, result


def test_route_answer_can_begin_with_an_entry_phrase_without_moving() -> None:
    result = review(
        action_type="Investigate",
        success_observation="进入三楼回廊的楼梯藏在东侧书架后。",
    )

    assert result.valid, result


def test_investigation_can_reveal_a_preexisting_bridge_state() -> None:
    result = review(
        action_type="Investigate",
        success_observation="裂纹表明整座石桥早已坍塌，河道中的断面都已钙化。",
    )

    assert result.valid, result


def test_bridge_collapse_cannot_exist_only_in_success_observation() -> None:
    result = review(
        success_observation="绳索断裂，整座吊桥轰然坍塌，两岸通路就此断绝。",
    )

    assert not result.valid
    assert result.error_code == "CHECK_SUCCESS_WORLD_CHANGE_UNCOMMITTED"


def test_fire_spread_cannot_exist_only_in_success_observation() -> None:
    result = review(
        success_observation="火势骤然蔓延到整层建筑，所有出口都被封死。",
    )

    assert not result.valid
    assert result.error_code == "CHECK_SUCCESS_WORLD_CHANGE_UNCOMMITTED"


def test_investigation_cannot_disguise_a_new_fire_event_as_a_reveal() -> None:
    result = review(
        action_type="Investigate",
        success_observation="火势骤然蔓延到整层建筑，楼梯当场被封死。",
    )

    assert not result.valid
    assert result.error_code == "CHECK_SUCCESS_WORLD_CHANGE_UNCOMMITTED"


def test_static_mechanism_reveal_remains_valid() -> None:
    result = review(
        action_type="Investigate",
        success_observation="两道回路正在交替切断锁芯供能，窗口每次只持续三息。",
    )

    assert result.valid, result
