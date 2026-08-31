from __future__ import annotations

import pytest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character


class FakeRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high
        return value


def _character(name: str, traits: list[str], *, protect: bool = False) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=50,
        hp=50,
        max_mp=30,
        mp=30,
        defenses={"physical": 5, "magic": 5},
        weapon_damage=5,
        traits=traits,
        skills={"挺身守护": 1} if protect else {},
    )


def _runtime(dice: list[int]):
    characters = CharacterManager()
    for character in (
        _character("赤炉大将", ["enemy", "villain"]),
        _character("诺艾尔", ["pc"], protect=True),
        _character("星澜", ["pc"]),
    ):
        characters.add(character)
    conflict = ConflictManager(characters)
    conflict.start_scene(
        "炉心王座",
        ["赤炉大将", "诺艾尔", "星澜"],
        player_side=["诺艾尔", "星澜"],
        enemy_side=["赤炉大将"],
    )
    rules = RulesEngine()
    rules._rng = FakeRandom(dice)
    return (
        ActionInterceptor(
            rules,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        ),
        characters,
        conflict,
    )


def test_guarded_creature_is_not_a_legal_melee_target_instead_of_redirecting_damage() -> None:
    interceptor, characters, conflict = _runtime([8, 8])
    conflict.apply_guard("诺艾尔", guarded_target="星澜")

    with pytest.raises(ValueError, match="不能成为近战攻击的目标"):
        interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "赤炉大将",
                    "target": "星澜",
                    "is_melee": True,
                },
            )
        )

    assert characters.get("诺艾尔").hp == 50
    assert characters.get("星澜").hp == 50
    legal = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": "赤炉大将",
                "target": "诺艾尔",
                "is_melee": True,
            },
        )
    )
    assert legal.payload["roll"].target == "诺艾尔"
    assert characters.get("诺艾尔").hp < 50


def test_guard_can_only_cover_another_present_ally() -> None:
    interceptor, _characters, _conflict = _runtime([])

    with pytest.raises(ValueError, match="另一名盟友"):
        interceptor.resolve(
            Action(
                ActionType.GUARD,
                {"actor": "诺艾尔", "guarded_target": "诺艾尔"},
            )
        )


def test_guard_is_once_per_owner_turn_and_cannot_build_a_cover_chain() -> None:
    interceptor, _characters, conflict = _runtime([])

    first = interceptor.resolve(
        Action(
            ActionType.GUARD,
            {"actor": "诺艾尔", "guarded_target": "星澜"},
        )
    )
    assert first.payload["guarding"] is True
    with pytest.raises(ValueError, match="本回合已经执行过一次防御行动"):
        interceptor.resolve(Action(ActionType.GUARD, {"actor": "诺艾尔"}))

    # A creature already protecting somebody cannot itself be placed behind
    # another Guard, preventing a recursive cover chain.
    conflict.clear_effects("诺艾尔")
    conflict.apply_guard("星澜", guarded_target="诺艾尔")
    with pytest.raises(ValueError, match="不能再成为另一名防御者的掩护目标"):
        conflict.apply_guard("诺艾尔", guarded_target="星澜")

    with pytest.raises(ValueError, match="同一阵营"):
        interceptor.resolve(
            Action(
                ActionType.GUARD,
                {"actor": "诺艾尔", "guarded_target": "赤炉大将"},
            )
        )


def test_protect_is_an_out_of_turn_reaction_and_redirects_exactly_one_attack() -> None:
    interceptor, characters, conflict = _runtime([7, 6, 7, 6])

    armed = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "挺身守护",
                "target": "星澜",
                "_enforce_turn_order": True,
            },
        )
    )
    assert armed.payload["protect_reaction_armed"] is True
    assert armed.payload["turn_consumed"] is False
    assert conflict.state.current_actor() == "赤炉大将"

    first = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": "赤炉大将",
                "target": "星澜",
                "is_melee": False,
            },
        )
    )
    assert first.payload["roll"].target == "诺艾尔"
    assert "挺身守护" in first.rules_text
    protected_hp = characters.get("诺艾尔").hp
    assert protected_hp < 50
    assert characters.get("星澜").hp == 50

    second = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": "赤炉大将",
                "target": "星澜",
                "is_melee": False,
            },
        )
    )
    assert second.payload["roll"].target == "星澜"
    assert characters.get("诺艾尔").hp == protected_hp
    assert characters.get("星澜").hp < 50


def test_protect_redirects_an_immediate_danger_outside_conflict() -> None:
    characters = CharacterManager()
    characters.add(_character("诺艾尔", ["pc"], protect=True))
    scenes = SceneManager()
    scene = scenes.start_scene(
        "庆典上的黑影",
        participants=["诺艾尔", "禾音"],
    )
    interceptor = ActionInterceptor(
        RulesEngine(),
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
        scene_manager=scenes,
    )

    result = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "挺身守护",
                "target": "禾音",
            },
        )
    )

    assert result.payload["protect_reaction_triggered"] is True
    assert result.payload["immediate_scene_protection"] is True
    assert result.payload["protected_target"] == "禾音"
    assert result.payload["turn_consumed"] is False
    assert "只能在冲突" not in result.rules_text
    assert scene.narrative_effects[-1]["owner"] == "诺艾尔"
    assert scene.narrative_effects[-1]["target"] == "禾音"
    assert scene.narrative_effects[-1]["data"]["immediate"] is True


def test_protect_redirects_a_dangerous_spell_without_charging_twice() -> None:
    interceptor, characters, _conflict = _runtime([7, 6])
    interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "挺身守护",
                "target": "星澜",
                "_enforce_turn_order": True,
            },
        )
    )

    result = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "赤炉大将",
                "target": "星澜",
                "attributes": ["INS", "WLP"],
                "mp_cost": 5,
                "fixed_damage": 5,
                "damage_type": "fire",
            },
        )
    )

    assert result.payload["roll"].target == "诺艾尔"
    assert "挺身守护" in result.rules_text
    assert characters.get("诺艾尔").hp < 50
    assert characters.get("星澜").hp == 50
    assert characters.get("赤炉大将").mp == 25


def test_failed_spell_does_not_consume_an_armed_protect_reaction() -> None:
    interceptor, characters, _conflict = _runtime([7, 6])
    characters.get("赤炉大将").spells = ["炎弹"]
    characters.get("赤炉大将").mp = 0
    interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "挺身守护",
                "target": "星澜",
                "_enforce_turn_order": True,
            },
        )
    )

    failed = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "赤炉大将",
                "spell_name": "炎弹",
                "target": "星澜",
            },
        )
    )
    assert failed.payload["spell_failed"] is True
    reaction = next(
        effect
        for effect in interceptor.conflict_manager.state.active_effects
        if effect.effect_type == "protect_reaction"
    )
    assert reaction.data["used"] is False

    attack = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {"actor": "赤炉大将", "target": "星澜", "is_melee": False},
        )
    )
    assert attack.payload["roll"].target == "诺艾尔"


def test_protect_resolves_separately_when_protector_was_already_a_target() -> None:
    interceptor, characters, _conflict = _runtime([7, 6])
    interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "挺身守护",
                "target": "星澜",
                "_enforce_turn_order": True,
            },
        )
    )

    result = interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": "赤炉大将",
                "target": "星澜",
                "targets": ["星澜", "诺艾尔"],
                "is_melee": False,
            },
        )
    )

    assert [roll.target for roll in result.payload["rolls"]] == ["诺艾尔", "诺艾尔"]
    assert characters.get("星澜").hp == 50
    assert characters.get("诺艾尔").hp < 30
