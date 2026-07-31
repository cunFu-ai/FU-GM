from __future__ import annotations

from fu_gm.components.combat_trait_manager import CombatTraitEvent
from fu_gm.models import EffectTiming, TimedEffect
from fu_gm.online_smoke_test import _json_safe


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
