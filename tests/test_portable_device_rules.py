from __future__ import annotations

from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.portable_device_rules import (
    portable_device_tiers,
    validate_portable_device_choices,
)
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character, HeroDraft


def test_portable_device_choices_preserve_unlock_and_upgrade_order() -> None:
    choices = validate_portable_device_choices(
        3,
        ["魔导", "炼金装置", "魔导装置"],
        require_complete=True,
    )

    assert choices == ["魔导装置", "炼金装置", "魔导装置"]
    assert portable_device_tiers(choices) == {"魔导装置": 2, "炼金装置": 1}


def test_hero_draft_with_portable_benefits_needs_one_choice_per_rank() -> None:
    world = WorldState()
    world.world_profile.hero_drafts["白河"] = HeroDraft(
        player_name="白河",
        hero_name="洛岚",
        identity="魔导工匠",
        theme="赎罪",
        origin="第七采掘城",
        classes={"造物使": 3, "武器大师": 2},
        attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
            skills={
                "便携装置": 2,
                "秘密配方": 1,
                "碎骨": 1,
                "破防打击": 1,
            },
            skill_options={"便携装置": ["魔导装置"]},
            equipment=["铁锤", "旅行装束"],
        )
    manager = CharacterCreationManager(CharacterManager(), world)

    validation = manager.validate_hero_draft("白河")

    assert not validation.ready
    assert validation.missing_fields == ["便携装置（还需 1 次装置选择）"]


def test_gadget_use_without_selected_device_returns_player_facing_clarification() -> None:
    characters = CharacterManager()
    hero = Character(
        name="洛岚",
        attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
        max_hp=45,
        hp=45,
        max_mp=35,
        mp=35,
        inventory_points=8,
        skills={"便携装置": 1},
        traits=["pc"],
    )
    characters.add(hero)
    conflict = ConflictManager(characters)
    interceptor = ActionInterceptor(
        RulesEngine(seed=1),
        characters,
        ClockManager(),
        conflict,
        WorldState(),
    )

    resolution = interceptor.resolve(
        Action(
            ActionType.TINKERER_GADGET,
            {"actor": "洛岚", "gadget_type": "alchemy", "tier": "basic"},
        )
    )

    assert resolution.payload["gadget_failed"] is True
    assert "还没选定装置类型" in resolution.rules_text
    assert "gadget_type" not in resolution.rules_text
    assert "mode" not in resolution.rules_text


def test_gadget_tier_blocks_unlearned_advanced_feature_without_spending_ip() -> None:
    characters = CharacterManager()
    hero = Character(
        name="洛岚",
        attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
        max_hp=45,
        hp=45,
        max_mp=35,
        mp=35,
        inventory_points=8,
        skills={"便携装置": 1},
        skill_options={"便携装置": ["魔导装置"]},
        traits=["pc"],
    )
    characters.add(hero)
    interceptor = ActionInterceptor(
        RulesEngine(seed=1),
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )

    resolution = interceptor.resolve(
        Action(
            ActionType.TINKERER_GADGET,
            {
                "actor": "洛岚",
                "gadget_type": "magitech",
                "mode": "魔法加农炮",
            },
        )
    )

    assert resolution.payload["gadget_failed"] is True
    assert "需要进阶增益" in resolution.rules_text
    assert characters.get("洛岚").inventory_points == 8
