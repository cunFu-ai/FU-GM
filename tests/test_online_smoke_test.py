from __future__ import annotations

from fu_gm.components.combat_trait_manager import CombatTraitEvent
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import EffectTiming, TimedEffect
from fu_gm.online_smoke_test import _json_safe, _seed_online_smoke_fixture


def test_online_smoke_fixture_is_isolated_and_minimal(tmp_path) -> None:
    service = FUGMHttpService(data_root=tmp_path, use_llm=False, rules_seed=1)
    runtime = service._runtime("online-smoke-test", auto_load=False)
    app = runtime.app

    _seed_online_smoke_fixture(app)

    assert [item.name for item in app.character_manager.all()] == [
        "冒烟测试角色",
        "训练靶",
    ]
    assert app.scene_manager.current_scene is not None
    assert app.scene_manager.current_scene.name == "在线 Agent 冒烟测试"
    assert app.conflict_manager.state.active is True
    assert service._player_character_control_map(runtime) == {
        "玩家": ["冒烟测试角色"]
    }


def test_json_safe_serializes_nested_typed_rule_events() -> None:
    event = CombatTraitEvent(
        actor="帝国机甲",
        event_type="flight_suppressed",
        summary="落地",
        effect=TimedEffect(
            owner="帝国机甲",
            effect_type="trait_suppression",
            expires_on=EffectTiming.ROUND_END,
        ),
    )

    payload = _json_safe({"events": [event]})

    assert payload["events"][0]["actor"] == "帝国机甲"
    assert payload["events"][0]["effect"]["expires_on"] == EffectTiming.ROUND_END.value
