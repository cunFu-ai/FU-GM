from __future__ import annotations

from dataclasses import asdict

import pytest

from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
    attack_rules_for_bestiary,
)
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_ability_runtime import npc_context_check_bonus
from fu_gm.components.npc_condition_manager import NPCConditionManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character, EnemyRank, StatusEffect


class FixedDice:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high
        return value


def creature(
    name: str,
    *,
    traits: list[str],
    statuses: set[StatusEffect] | None = None,
    hp: int = 80,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=hp,
        hp=hp,
        max_mp=40,
        mp=40,
        fabula_points=0,
        defenses={"physical": 5, "magic": 5},
        traits=list(traits),
        statuses=list(statuses or set()),
    )


def runtime(
    *characters: Character,
    dice: list[int],
    order: list[str] | None = None,
) -> tuple[ActionInterceptor, CharacterManager, ClockManager, ConflictManager]:
    manager = CharacterManager()
    for character in characters:
        manager.add(character)
    conflict = ConflictManager(manager)
    names = list(order or [character.name for character in characters])
    player_side = [name for name in names if "pc" in manager.get(name).traits]
    enemy_side = [name for name in names if name not in player_side]
    conflict.start_scene(
        "图鉴规则测试",
        names,
        player_side=player_side,
        enemy_side=enemy_side,
    )
    rules = RulesEngine()
    rules._rng = FixedDice(dice)
    clocks = ClockManager()
    interceptor = ActionInterceptor(
        rules,
        manager,
        clocks,
        conflict,
        WorldState(),
    )
    return interceptor, manager, clocks, conflict


def npc_attack(
    actor: str,
    target: str,
    *,
    attack_name: str,
    damage_type: str,
    weapon_damage: int,
    status: str = "",
    effects: list[dict[str, object]] | None = None,
) -> Action:
    return Action(
        ActionType.NPCACT,
        {
            "actor": actor,
            "npc_action_type": "Attack",
            "target": target,
            "attributes": ["DEX", "INS"],
            "attack_name": attack_name,
            "damage_type": damage_type,
            "weapon_damage": weapon_damage,
            "targets_magic_defense": damage_type == "none",
            "status_effect_on_hit": status or None,
            "npc_attack_effects": list(effects or []),
        },
    )


def effect_dicts(template: str, attack_name: str) -> list[dict[str, object]]:
    return [
        asdict(effect)
        for effect in attack_rules_for_bestiary(template, attack_name).get(
            "effects",
            [],
        )
    ]


def test_cockatrice_first_hit_slows_without_damage_or_petrification_check() -> None:
    cockatrice = creature("鸡蛇怪", traits=["enemy", "怪物"])
    hero = creature("探险者", traits=["pc"])
    interceptor, manager, _, _ = runtime(
        cockatrice,
        hero,
        dice=[8, 7],
        order=["鸡蛇怪", "探险者"],
    )

    result = interceptor.resolve(
        npc_attack(
            "鸡蛇怪",
            "探险者",
            attack_name="石化啄击",
            damage_type="none",
            weapon_damage=0,
            status="slow",
            effects=effect_dicts("鸡蛇怪", "石化啄击"),
        )
    )

    assert manager.get("探险者").hp == 80
    assert StatusEffect.SLOW in manager.get("探险者").statuses
    assert interceptor.decision_window_manager.find_pending(kind="reactive_check") is None
    assert result.payload["roll"].success


def test_cockatrice_second_hit_opens_check_and_failed_check_petrifies() -> None:
    cockatrice = creature("鸡蛇怪", traits=["enemy", "怪物"])
    hero = creature(
        "探险者",
        traits=["pc"],
        statuses={StatusEffect.SLOW},
    )
    interceptor, manager, _, conflict = runtime(
        cockatrice,
        hero,
        dice=[8, 7, 3, 4],
        order=["鸡蛇怪", "探险者"],
    )

    interceptor.resolve(
        npc_attack(
            "鸡蛇怪",
            "探险者",
            attack_name="石化啄击",
            damage_type="none",
            weapon_damage=0,
            status="slow",
            effects=effect_dicts("鸡蛇怪", "石化啄击"),
        )
    )
    window = interceptor.decision_window_manager.find_pending(
        kind="reactive_check",
        owner="探险者",
    )
    assert window is not None

    resolved = interceptor.resolve(
        Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": "探险者",
                "target": "抵抗石化",
                "attributes": ["MIG", "WLP"],
                "target_number": 10,
                "non_damage": True,
                "_reactive_check_window_id": window.window_id,
                "_reaction_followup": True,
                "_enforce_turn_order": False,
            },
        )
    )

    assert not resolved.payload["roll"].success
    assert "石化" in manager.get("探险者").special_conditions["petrified"]
    assert "探险者" not in conflict.state.turn_order
    assert interceptor.decision_window_manager.find_pending(
        window_id=window.window_id
    ) is None


def test_multi_target_non_damage_attack_never_enters_damage_resolution() -> None:
    cockatrice = creature("鸡蛇怪", traits=["enemy", "怪物"])
    first = creature("甲", traits=["pc"])
    second = creature("乙", traits=["pc"])
    interceptor, manager, _, _ = runtime(
        cockatrice,
        first,
        second,
        dice=[8, 8],
        order=["鸡蛇怪", "甲", "乙"],
    )

    result = interceptor.resolve(
        Action(
            ActionType.NPCACT,
            {
                "actor": "鸡蛇怪",
                "npc_action_type": "Attack",
                "targets": ["甲", "乙"],
                "attributes": ["DEX", "INS"],
                "attack_name": "石化啄击",
                "damage_type": "none",
                "weapon_damage": 0,
                "targets_magic_defense": True,
                "status_effect_on_hit": StatusEffect.SLOW,
            },
        )
    )

    assert manager.get("甲").hp == 80
    assert manager.get("乙").hp == 80
    assert StatusEffect.SLOW in manager.get("甲").statuses
    assert StatusEffect.SLOW in manager.get("乙").statuses
    assert all(outcome.damage == 0 for outcome in result.payload["rolls"])


def test_cockatrice_critical_resistance_settles_parent_before_optional_opportunity() -> None:
    cockatrice = creature("鸡蛇怪", traits=["enemy", "怪物"])
    hero = creature(
        "探险者",
        traits=["pc"],
        statuses={StatusEffect.SLOW},
    )
    interceptor, manager, _, _ = runtime(
        cockatrice,
        hero,
        dice=[8, 7, 8, 8],
        order=["鸡蛇怪", "探险者"],
    )
    interceptor.resolve(
        npc_attack(
            "鸡蛇怪",
            "探险者",
            attack_name="石化啄击",
            damage_type="none",
            weapon_damage=0,
            status="slow",
            effects=effect_dicts("鸡蛇怪", "石化啄击"),
        )
    )
    parent = interceptor.decision_window_manager.find_pending(
        kind="reactive_check",
        owner="探险者",
    )
    assert parent is not None

    check = interceptor.resolve(
        Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": "探险者",
                "target": "抵抗石化",
                "attributes": ["MIG", "WLP"],
                "target_number": 10,
                "non_damage": True,
                "_reactive_check_window_id": parent.window_id,
                "_reaction_followup": True,
                "_enforce_turn_order": False,
            },
        )
    )
    opportunity = interceptor.decision_window_manager.find_pending(
        kind="critical_opportunity",
        owner="探险者",
    )
    assert check.payload["roll"].critical_success
    assert opportunity is not None

    interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "探险者",
                "window_id": opportunity.window_id,
                "effect": "优势",
                "target": "探险者",
            },
        )
    )

    assert "petrified" not in manager.get("探险者").special_conditions
    assert interceptor.decision_window_manager.find_pending(
        window_id=parent.window_id
    ) is None


def test_swallow_restricts_actions_hurts_on_turn_and_releases_at_four_segments() -> None:
    flower = creature("陷龙花", traits=["enemy", "植物"], hp=120)
    hero = creature(
        "探险者",
        traits=["pc"],
        statuses={StatusEffect.WEAKENED},
        hp=100,
    )
    interceptor, manager, clocks, conflict = runtime(
        flower,
        hero,
        dice=[8, 7, 5, 5],
        order=["陷龙花", "探险者"],
    )

    interceptor.resolve(
        npc_attack(
            "陷龙花",
            "探险者",
            attack_name="吞龙巨口",
            damage_type="physical",
            weapon_damage=10,
            effects=effect_dicts("陷龙花", "吞龙巨口"),
        )
    )
    swallowed = conflict.state.swallowed_targets["探险者"]
    assert clocks.get(swallowed.escape_clock).current == 0
    with pytest.raises(ValueError, match="只能推进脱困命刻"):
        interceptor.resolve(Action(ActionType.GUARD, {"actor": "探险者"}))

    before_turn_damage = manager.get("探险者").hp
    assert conflict.next_turn() == "探险者"
    conflict.begin_current_turn()
    assert manager.get("探险者").hp == before_turn_damage - 20

    manager.modify_resource("陷龙花", "hp", -1)
    assert clocks.get(swallowed.escape_clock).current == 1
    interceptor.resolve(
        Action(
            ActionType.OBJECTIVE,
            {
                "actor": "探险者",
                "target": swallowed.escape_clock,
                "clock_name": swallowed.escape_clock,
                "attributes": ["MIG", "WLP"],
                "target_number": 10,
            },
        )
    )
    assert clocks.get(swallowed.escape_clock).current == 2

    manager.modify_resource("陷龙花", "hp", -1)
    manager.modify_resource("陷龙花", "hp", -1)
    assert "探险者" not in conflict.state.swallowed_targets


def test_champion_swallow_capacity_is_two_only_with_three_actions() -> None:
    flower = creature("陷龙花", traits=["enemy", "植物"])
    first = creature("甲", traits=["pc"])
    second = creature("乙", traits=["pc"])
    manager = CharacterManager()
    for character in (flower, first, second):
        manager.add(character)
    conflict = ConflictManager(manager)
    conflict.register_enemy("陷龙花", EnemyRank.CHAMPION, action_count=3)
    conflict.start_scene(
        "花腹",
        ["陷龙花", "甲", "乙"],
        player_side=["甲", "乙"],
        enemy_side=["陷龙花"],
    )
    conditions = NPCConditionManager(manager, ClockManager(), conflict)

    assert conditions.capacity_for("陷龙花") == 2
    conditions.swallow("陷龙花", "甲")
    conditions.swallow("陷龙花", "乙")
    assert len(conditions.swallowed_by("陷龙花")) == 2


def test_npc_interposer_uses_own_policy_and_only_once_until_its_turn() -> None:
    hero = creature("英雄", traits=["pc"])
    ward = creature("书记官", traits=["enemy", "人型"])
    guard = creature("守卫", traits=["enemy", "人型"])
    guard.npc_ability_profiles = ability_profiles_for_bestiary("守卫")
    guard.npc_tactics = {"protect_policy": "always"}
    interceptor, manager, _, conflict = runtime(
        hero,
        guard,
        ward,
        dice=[8, 7],
        order=["英雄", "守卫", "书记官"],
    )
    manager.get("英雄").weapon_damage = 5

    interceptor.resolve(
        Action(
            ActionType.ATTACK,
            {
                "actor": "英雄",
                "target": "书记官",
                "attributes": ["DEX", "INS"],
                "weapon_damage": 5,
                "damage_type": "physical",
            },
        )
    )

    assert manager.get("书记官").hp == 80
    assert manager.get("守卫").hp < 80
    assert conflict.npc_interposer_for("书记官", source_actor="英雄") is None


def test_npc_interposer_can_take_a_hostile_spell_for_an_ally() -> None:
    caster = creature("元素使", traits=["pc"])
    caster.level = 20
    ward = creature("书记官", traits=["enemy", "人型"])
    guard = creature("灰嚎怪", traits=["enemy", "野兽"])
    guard.npc_ability_profiles = ability_profiles_for_bestiary("灰嚎怪")
    guard.npc_tactics = {"protect_policy": "always"}
    interceptor, manager, _, _ = runtime(
        caster,
        guard,
        ward,
        dice=[8, 7],
        order=["元素使", "灰嚎怪", "书记官"],
    )

    result = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "元素使",
                "target": "书记官",
                "spell_name": "焰流",
            },
        )
    )

    assert manager.get("书记官").hp == 80
    assert manager.get("灰嚎怪").hp < 80
    assert "灰嚎怪挺身代替书记官" in result.rules_text


def test_lamia_negotiation_bonus_only_applies_to_matching_check_context() -> None:
    lamia = creature("蛇足女妖", traits=["enemy", "怪物"])
    lamia.npc_ability_profiles = ability_profiles_for_bestiary("蛇足女妖")
    hero = creature("使者", traits=["pc"])
    interceptor, _, _, _ = runtime(
        lamia,
        hero,
        dice=[3, 4, 3, 4],
        order=["蛇足女妖", "使者"],
    )

    assert npc_context_check_bonus(lamia, "交涉对抗检定") == 3
    assert npc_context_check_bonus(lamia, "调查墙上的刻痕") == 0
    negotiation = interceptor.resolve(
        Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": "蛇足女妖",
                "target": "使者",
                "attributes": ["INS", "WLP"],
                "target_number": 10,
                "non_damage": True,
                "check_context": "交涉对抗检定",
                "_enforce_turn_order": False,
            },
        )
    )
    ordinary = interceptor.resolve(
        Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": "蛇足女妖",
                "target": "石墙",
                "attributes": ["INS", "WLP"],
                "target_number": 10,
                "non_damage": True,
                "check_context": "调查检定",
                "_enforce_turn_order": False,
            },
        )
    )

    assert negotiation.payload["roll"].total == 10
    assert negotiation.payload["roll"].success
    assert ordinary.payload["roll"].total == 7
    assert not ordinary.payload["roll"].success
