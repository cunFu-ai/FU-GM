from __future__ import annotations

from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.gadget_manager import TinkererGadgetManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Affinity,
    Character,
    HeroCreationProfile,
    TravelEventType,
    TravelThreatLevel,
)


class FakeRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high
        return value


def character(
    name: str,
    *,
    hp: int = 40,
    max_hp: int = 40,
    mp: int = 40,
    max_mp: int = 40,
    skills: dict[str, int] | None = None,
    skill_options: dict[str, list[str]] | None = None,
    traits: list[str] | None = None,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=max_hp,
        hp=hp,
        max_mp=max_mp,
        mp=mp,
        crisis_threshold=max_hp // 2,
        inventory_points=10,
        max_inventory_points=10,
        defenses={"physical": 10, "magic": 10},
        skills=skills or {},
        skill_options=skill_options or {},
        traits=traits or ["pc"],
    )


def test_starting_character_applies_permanent_hp_and_mp_skills() -> None:
    characters = CharacterManager()
    rules = RulesEngine()
    rules._rng = FakeRandom([3, 4])
    manager = CharacterCreationManager(characters, WorldState(), rules)

    result = manager.create_player_character(
        HeroCreationProfile(
            player_name="玩家",
            hero_name="守书人",
            identity="守卫禁书库的学者",
            theme="使命",
            origin="白塔",
            classes={"守护者": 3, "博学家": 2},
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            skills={
                "铁壁": 1,
                "保镖": 1,
                "防御精通": 1,
                "集中心智": 1,
                "知识就是力量": 1,
            },
            equipment=["旅行装束"],
        )
    )

    assert result.character.max_hp == 53
    assert result.character.max_mp == 53
    assert result.character.hp == 53
    assert result.character.mp == 53
    assert result.character.permanent_skill_ranks_applied == {
        "铁壁": 1,
        "集中心智": 1,
    }


def test_legacy_permanent_skill_bonus_reconciliation_is_idempotent() -> None:
    characters = CharacterManager()
    hero = character(
        "旧存档英雄",
        max_hp=45,
        hp=20,
        max_mp=45,
        mp=10,
        skills={"铁壁": 2, "集中心智": 1},
    )
    characters.add(hero)

    first = characters.reconcile_permanent_skill_bonuses()
    second = characters.reconcile_permanent_skill_bonuses()

    repaired = characters.get("旧存档英雄")
    assert repaired.max_hp == 51
    assert repaired.max_mp == 48
    assert repaired.hp == 20
    assert repaired.mp == 10
    assert len(first) == 2
    assert second == []


def test_staged_journey_freezes_wayfarer_travel_roll_modifiers() -> None:
    rules = RulesEngine()
    rules._rng = FakeRandom([3])
    travel = TravelManager(rules)
    progress = travel.begin_journey(
        journey_id="journey-1",
        origin="白塔",
        destination="旧港",
        threat_levels=[TravelThreatLevel.MEDIUM],
        regions=["盐沼"],
        threat_die_step_reduction=1,
        discovery_threshold=3,
    )

    advance = travel.advance_active_journey()

    assert progress.threat_die_step_reduction == 1
    assert progress.discovery_threshold == 3
    assert advance.pending_event is not None
    assert advance.pending_event.die_size == 8
    assert advance.pending_event.roll == 3
    assert advance.pending_event.event_type == TravelEventType.DISCOVERY


def test_secret_formula_modifies_crafted_alchemy_damage_and_healing() -> None:
    characters = CharacterManager()
    inventor = character(
        "造物使",
        hp=0,
        max_hp=200,
        skills={"便携装置": 1, "秘密配方": 2},
        skill_options={"便携装置": ["炼金装置"]},
    )
    enemy = character("机兵", hp=40, max_hp=40, traits=["enemy"])
    characters.add(inventor)
    characters.add(enemy)
    rules = RulesEngine()
    rules._rng = FakeRandom([8, 9, 8, 9])
    manager = TinkererGadgetManager(
        rules,
        characters,
        ConflictManager(characters),
    )

    damage = manager.use_alchemy(
        "造物使",
        target_roll=7,
        effect_roll=4,
        targets=["机兵"],
    )
    healing = manager.use_alchemy(
        "造物使",
        target_roll=1,
        effect_roll=18,
        targets=["造物使"],
    )

    assert damage.damage_results[0]["damage"] == 22
    assert characters.get("机兵").hp == 18
    assert healing.resource_changes[0].amount == 110
    assert characters.get("造物使").hp == 110


def test_dark_blood_changes_actual_damage_resolution_in_crisis() -> None:
    target = character(
        "暗刃",
        hp=20,
        max_hp=50,
        skills={"身负黑血": 1},
    )

    damage, affinity = RulesEngine().compute_damage(
        high_roll=10,
        weapon_damage=10,
        damage_type="dark",
        target=target,
    )

    assert affinity == Affinity.RESIST
    assert damage == 10
