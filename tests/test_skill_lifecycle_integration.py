from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Bond, Character, RollOutcome, SceneType, StatusEffect


def _character(
    name: str,
    *,
    pc: bool,
    skills: dict[str, int] | None = None,
    skill_options: dict[str, list[str]] | None = None,
) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
        max_hp=60,
        hp=60,
        max_mp=40,
        mp=40,
        fabula_points=2 if pc else 0,
        defenses={"physical": 8, "magic": 8},
        weapon_damage=5,
        traits=["pc" if pc else "enemy"],
        skills=skills or {},
        skill_options=skill_options or {},
    )


def _interceptor(*characters: Character) -> tuple[ActionInterceptor, ConflictManager, WorldState]:
    manager = CharacterManager()
    for character in characters:
        manager.add(character)
    conflict = ConflictManager(manager)
    world = WorldState()
    interceptor = ActionInterceptor(RulesEngine(seed=1), manager, ClockManager(), conflict, world)
    return interceptor, conflict, world


def _hit(actor: str, target: str, *, high_roll: int = 5) -> RollOutcome:
    return RollOutcome(
        actor=actor,
        attributes=["DEX", "MIG"],
        dice=[(8, high_roll), (8, high_roll - 1)],
        modifier=0,
        total=high_roll * 2 - 1,
        high_roll=high_roll,
        target=target,
        target_number=8,
        success=True,
        critical_success=False,
        fumble=False,
        margin=high_roll * 2 - 9,
    )


def test_damage_lifecycle_applies_dirty_tricks_and_pain_once_per_turn() -> None:
    hero = _character("绯", pc=True, skills={"阴狠手段": 2, "痛楚": 2})
    hero.hp = 30
    hero.mp = 10
    hero.fabula_points = 0
    hero.bonds = [Bond(target="宿敌", emotions=["憎恨"])]
    enemy = _character("宿敌", pc=False)
    enemy.statuses = [StatusEffect.SHAKEN]
    interceptor, conflict, _world = _interceptor(hero, enemy)
    conflict.start_scene("钟楼决战", ["绯", "宿敌"])

    interceptor.rules_engine.force_next_check_outcome(_hit("绯", "宿敌"))
    first = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "绯", "target": "宿敌"}))
    interceptor.rules_engine.force_next_check_outcome(_hit("绯", "宿敌"))
    second = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "绯", "target": "宿敌"}))

    assert first.payload["roll"].damage == 13  # high 5 + weapon 5 + rank 2 + one status
    assert interceptor.character_manager.get("绯").hp == 34
    assert interceptor.character_manager.get("绯").mp == 14
    assert sum(
        1
        for resolution in (first, second)
        for event in resolution.payload.get("skill_trigger_events", [])
        if event.get("source") == "痛楚"
    ) == 2  # one HP and one MP record, both from the first trigger only


def test_guard_lifecycle_applies_bodyguard_and_persists_guard_choices() -> None:
    guardian = _character("盾卫", pc=True, skills={"保镖": 1, "死战不退": 2, "鹰眼": 1})
    guardian.hp = 30
    guardian.bonds = [Bond(target="同伴", emotions=["信赖", "喜爱"])]
    ally = _character("同伴", pc=True)
    enemy = _character("敌兵", pc=False)
    interceptor, conflict, _world = _interceptor(guardian, ally, enemy)
    conflict.start_scene("桥头", ["盾卫", "敌兵", "同伴"])

    covered = interceptor.resolve(
        Action(ActionType.GUARD, {"actor": "盾卫", "guarded_target": "同伴"})
    )
    assert interceptor.character_manager.effective_affinity("同伴", "fire") == Affinity.RESIST
    assert any(event.get("source") == "保镖" for event in covered.payload["skill_trigger_events"])

    unguarded = interceptor.resolve(Action(ActionType.GUARD, {"actor": "盾卫"}))
    skills = {window["skill"] for window in unguarded.payload["skill_decision_windows"]}
    assert skills == {"死战不退", "鹰眼"}
    assert interceptor.character_manager.get("盾卫").hp == 34


def test_spending_fabula_opens_one_persisted_unyielding_will_choice() -> None:
    hero = _character("炽心", pc=True, skills={"不屈意志": 2})
    hero.hp = 20
    interceptor, _conflict, world = _interceptor(hero)

    spent = interceptor.resolve(
        Action(ActionType.MODIFY_RESOURCE, {"target": "炽心", "resource": "fabula_points", "amount": -1})
    )
    window = next(window for window in spent.payload["skill_decision_windows"] if window["skill"] == "不屈意志")
    assert len([item for item in world.decision_windows.values() if item.status.value == "pending"]) == 1

    resolved = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "炽心",
                "window_id": window["window_id"],
                "choice": "recover_hp",
                "selected_option": {"choice": "recover_hp", "amount": 10},
            },
        )
    )

    assert resolved.payload["decision_window_id"] == window["window_id"]
    assert interceptor.character_manager.get("炽心").hp == 30
    assert not interceptor.decision_window_manager.pending(kind="skill_parameter", owner="炽心")


def test_trait_and_bond_post_check_replies_bypass_general_action_planning() -> None:
    hero = _character("赛璃", pc=True)
    hero.identity = "钟鸣公国的御魂医师"
    hero.bonds = [Bond(target="伊莉雅", emotions=["信赖"])]
    ally = _character("伊莉雅", pc=True)
    interceptor, _conflict, world = _interceptor(hero, ally)
    interceptor.decision_window_manager.create(
        kind="trait_invocation",
        owner="赛璃",
        options=[{"trait": "钟鸣公国的御魂医师"}],
        blocking=True,
    )
    interceptor.decision_window_manager.create(
        kind="bond_invocation",
        owner="赛璃",
        options=[{"target": "伊莉雅", "strength": 1}],
        blocking=True,
    )
    trait_window = interceptor.decision_window_manager.pending(
        kind="trait_invocation",
        owner="赛璃",
    )[0]
    bond_window = interceptor.decision_window_manager.pending(
        kind="bond_invocation",
        owner="赛璃",
    )[0]
    trait = Action(
        ActionType.INVOKE_TRAIT,
        {
            "actor": "赛璃",
            "window_id": trait_window.window_id,
            "trait_name": "钟鸣公国的御魂医师",
            "reroll_indices": [0, 1],
            "reroll_index_base": 0,
        },
    )
    bond = Action(
        ActionType.INVOKE_BOND,
        {
            "actor": "赛璃",
            "window_id": bond_window.window_id,
            "bond_target": "伊莉雅",
        },
    )

    assert trait.action_type == ActionType.INVOKE_TRAIT
    assert trait.parameters["reroll_indices"] == [0, 1]
    assert trait.parameters["reroll_index_base"] == 0
    assert bond.action_type == ActionType.INVOKE_BOND
    assert bond.parameters["bond_target"] == "伊莉雅"


def test_advantage_opportunity_can_target_its_owner() -> None:
    hero = _character("赛璃", pc=True)
    interceptor, _conflict, world = _interceptor(hero)
    interceptor.decision_window_manager.create(
        kind="critical_opportunity",
        owner="赛璃",
        options=[{"effect": "优势"}],
        blocking=True,
    )
    window = interceptor.decision_window_manager.pending(
        kind="critical_opportunity",
        owner="赛璃",
    )[0]
    action = Action(
        ActionType.TRIGGER_OPPORTUNITY,
        {
            "actor": "赛璃",
            "window_id": window.window_id,
            "effect": "优势",
            "target": "赛璃",
        },
    )

    assert action.action_type == ActionType.TRIGGER_OPPORTUNITY
    assert action.parameters["effect"] == "优势"
    assert action.parameters["target"] == "赛璃"


def test_active_arcanum_dismissal_clears_link_and_opens_echo_window() -> None:
    hero = _character("召灵者", pc=True, skills={"契约与召唤": 1, "奥灵回响": 2})
    hero.active_arcanum = "魔典"
    hero.equipment = ["法杖"]
    hero.equipped_main_hand = "法杖"
    interceptor, conflict, _world = _interceptor(hero)
    conflict.start_scene("神谕厅", ["召灵者"])

    resolution = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "召灵者",
                "skill_name": "契约与召唤",
                "mode": "dismiss",
                "question": "钟声从何而来？",
            },
        )
    )

    assert interceptor.character_manager.get("召灵者").active_arcanum == ""
    assert any(window["skill"] == "奥灵回响" for window in resolution.payload["skill_decision_windows"])


def test_same_species_poison_is_applied_by_skill_event_lifecycle() -> None:
    hero = _character("拟兽使", pc=True, skills={"同源之毒": 1})
    wolf = _character("霜狼", pc=False)
    automaton = _character("机兵", pc=False)
    interceptor, conflict, _world = _interceptor(hero, wolf, automaton)
    conflict.start_scene("冰原", ["拟兽使", "霜狼", "机兵"])

    outcome = interceptor.skill_lifecycle.trigger(
        "after_chimerist_spell_damage",
        hero,
        origin_species="野兽",
        damaged_targets=[
            {"target": "霜狼", "species": "野兽"},
            {"target": "机兵", "species": "构装体"},
        ],
    )

    assert StatusEffect.POISONED in wolf.statuses
    assert StatusEffect.POISONED not in automaton.statuses
    assert any(record.get("source") == "同源之毒" for record in outcome.records)


def test_potion_rain_is_emitted_by_alchemy_and_commits_half_healing() -> None:
    inventor = _character(
        "炼金师",
        pc=True,
        skills={"便携装置": 1, "药剂雨": 1},
        skill_options={"便携装置": ["炼金装置"]},
    )
    inventor.max_hp = 200
    inventor.hp = 0
    inventor.inventory_points = 6
    ally = _character("同伴", pc=True)
    ally.max_hp = 200
    ally.hp = 0
    interceptor, _conflict, world = _interceptor(inventor, ally)

    crafted = interceptor.resolve(
        Action(
            ActionType.TINKERER_GADGET,
            {
                "actor": "炼金师",
                "gadget_type": "alchemy",
                "tier": "basic",
                "target_roll": 1,
                "effect_roll": 18,
                "targets": ["炼金师"],
            },
        )
    )
    window = next(item for item in crafted.payload["skill_decision_windows"] if item["skill"] == "药剂雨")
    assert interceptor.character_manager.get("炼金师").hp == 100

    resolved = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "炼金师",
                "window_id": window["window_id"],
                "choice": "select_targets",
                "selected_option": {
                    "choice": "select_targets",
                    "targets": ["同伴"],
                    "max_extra_targets": 1,
                },
            },
        )
    )

    assert resolved.payload["decision_window_id"] == window["window_id"]
    assert interceptor.character_manager.get("炼金师").hp == 50
    assert interceptor.character_manager.get("同伴").hp == 50


def test_elemental_shroud_waits_for_legal_targets_and_element_before_spending_mp() -> None:
    caster = _character("伊莉雅", pc=True)
    ally = _character("失忆旅人", pc=True)
    interceptor, _conflict, world = _interceptor(caster, ally)

    pending = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "伊莉雅",
                "target": "驿站廊口与旅人的身影",
                "spell_name": "元素幕障",
                "clock_name": "财团巡逻队逼近",
                "clock_direction": -1,
            },
        )
    )

    assert pending.payload["spell_parameter_required"] is True
    assert set(pending.payload["required_fields"]) == {"targets", "chosen_damage_type"}
    assert interceptor.character_manager.get("伊莉雅").mp == 40
    assert "roll" not in pending.payload
    window = interceptor.decision_window_manager.pending(kind="spell_parameter", owner="伊莉雅")[0]

    resolved = interceptor.resolve(
        Action(
            ActionType.RESOLVE_DECISION,
            {
                "actor": "伊莉雅",
                "window_id": window.window_id,
                "choice": "cast_spell",
                "selected_option": {
                    "choice": "cast_spell",
                    "targets": ["伊莉雅", "失忆旅人"],
                    "chosen_damage_type": "fire",
                },
            },
        )
    )

    assert interceptor.character_manager.get("伊莉雅").mp == 30
    assert interceptor.character_manager.effective_affinity("伊莉雅", "fire") == Affinity.RESIST
    assert interceptor.character_manager.effective_affinity("失忆旅人", "fire") == Affinity.RESIST
    assert "roll" not in resolved.payload
    assert resolved.payload["committed_source_action"].action_type == ActionType.SPELL
    assert "clock_name" not in resolved.payload["committed_source_action"].parameters
    assert not interceptor.decision_window_manager.pending(kind="spell_parameter", owner="伊莉雅")


def test_elemental_shroud_can_target_present_narrative_npc_without_fabricated_stats() -> None:
    caster = _character("伊莉雅", pc=True)
    caster.spells = ["元素幕障"]
    manager = CharacterManager()
    manager.add(caster)
    conflict = ConflictManager(manager)
    world = WorldState()
    scenes = SceneManager()
    scenes.start_scene(
        "风铃廊问路",
        SceneType.STANDARD,
        participants=["伊莉雅", "失忆旅人"],
    )
    interceptor = ActionInterceptor(
        RulesEngine(seed=1),
        manager,
        ClockManager(),
        conflict,
        world,
        scene_manager=scenes,
    )

    resolved = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "伊莉雅",
                "target": "失忆旅人",
                "spell_name": "元素幕障",
                "chosen_damage_type": "wind",
            },
        )
    )

    assert manager.get("伊莉雅").mp == 35
    assert resolved.payload["spell_target"] == "失忆旅人"
    assert scenes.current_scene is not None
    assert scenes.current_scene.narrative_effects[0]["target"] == "失忆旅人"
    assert scenes.current_scene.narrative_effects[0]["effect_type"] == "affinity_buff"
    assert not manager.exists("失忆旅人")
