from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.components.skill_trigger_manager import SkillTriggerManager
from fu_gm.components.solo_play_manager import SoloPlayManager
from fu_gm.components.world_state import WorldState
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Character, EncounterDifficulty, RollOutcome, StatusEffect


def _hero(*, skills=None):
    return Character(
        name="诺艾尔",
        attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 8},
        max_hp=45,
        hp=45,
        max_mp=45,
        mp=45,
        traits=["pc"],
        skills=skills or {},
    )


def test_single_pc_activates_solo_guidance_and_encounter_safety_notes() -> None:
    characters = CharacterManager()
    characters.add(_hero())
    world = WorldState()
    solo = SoloPlayManager(characters, world)
    encounter = EncounterManager(characters, ConflictManager(characters))

    design = encounter.design_encounter(["诺艾尔"], difficulty=EncounterDifficulty.NORMAL)

    assert solo.is_active()
    assert "两条可行路径" in solo.prompt_guidance()
    assert any("单人档位" in note for note in design.transparency_notes)


def test_skill_trigger_manifest_exposes_conflict_start_decisions() -> None:
    actor = _hero(skills={"疾速身法": 1, "快速评估": 2})
    manager = SkillTriggerManager()

    manifest = manager.hook_manifest(actor)
    windows = manager.judgement_windows_for_event(actor, "conflict_start")

    assert set(manifest["conflict_start"]) == {"疾速身法", "快速评估"}
    assert {window["skill"] for window in windows} == {"疾速身法", "快速评估"}


def test_skill_lifecycle_bus_has_no_unhandled_phase() -> None:
    manifest = SkillTriggerManager().lifecycle_manifest()

    assert manifest["unhandled_events"] == []


def test_lucky_seven_replaces_one_die_and_replays_committed_check() -> None:
    hero = _hero(skills={"幸运七": 1})
    characters = CharacterManager()
    characters.add(hero)
    rules = RulesEngine()
    rules._rng = _FixedDice([2, 3])
    interceptor = ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )

    first = interceptor.resolve(
        Action(
            ActionType.REQUEST_ROLL,
            {
                "actor": "诺艾尔",
                "target": "锁住的钟门",
                "attributes": ["DEX", "INS"],
                "target_number": 10,
                "non_damage": True,
            },
        )
    )
    lucky_window = next(
        window
        for window in first.payload["post_check_windows"]
        if window.get("label") == "幸运七"
    )
    revised = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "skill_name": "幸运七",
                "die_index": 1,
                "window_id": lucky_window["window_id"],
            },
        )
    )

    assert not first.payload["roll"].success
    assert any(window.get("label") == "幸运七" for window in first.payload["post_check_windows"])
    assert revised.payload["roll"].success
    assert revised.payload["roll"].dice == [(8, 7), (10, 3)]
    assert characters.get("诺艾尔").lucky_number == 2
    assert "scene:skill:幸运七" in characters.get("诺艾尔").trigger_cooldowns

    SkillTriggerManager().emit("scene_end", characters.get("诺艾尔"))
    assert "scene:skill:幸运七" not in characters.get("诺艾尔").trigger_cooldowns


def test_dark_blood_and_dodge_are_part_of_effective_defense_state() -> None:
    characters = CharacterManager()
    hero = _hero(skills={"身负黑血": 1, "闪避": 2})
    hero.hp = hero.max_hp // 2
    hero.defenses["physical"] = 8
    hero.equipped_armor = "旅行装束"
    characters.add(hero)

    assert characters.effective_affinity("诺艾尔", "dark") == Affinity.RESIST
    assert characters.effective_affinity("诺艾尔", "poison") == Affinity.RESIST
    assert characters.effective_defense("诺艾尔", "physical") == 10


def test_rage_can_turn_a_low_non_fumble_pair_into_critical_success() -> None:
    rules = RulesEngine(seed=0)
    actor = _hero(skills={"狂暴": 1})
    rules.force_next_check_outcome(
        RollOutcome(
            actor=actor.name,
            attributes=["DEX", "MIG"],
            dice=[(8, 4), (8, 4)],
            modifier=0,
            total=8,
            high_roll=4,
            target_number=20,
            success=False,
            critical_success=False,
            fumble=False,
            margin=-12,
        )
    )

    outcome = rules.roll_check(
        actor,
        ["DEX", "MIG"],
        20,
        critical_on_any_pair=True,
    )

    assert outcome.critical_success
    assert outcome.success
    assert outcome.opportunity_count == 1


def test_entropy_feedback_recovers_mp_through_resource_lifecycle() -> None:
    characters = CharacterManager()
    hero = _hero(skills={"灵智回流": 3})
    hero.mp = 10
    characters.add(hero)
    conflict = ConflictManager(characters)
    interceptor = ActionInterceptor(
        RulesEngine(seed=0),
        characters,
        ClockManager(),
        conflict,
        WorldState(),
    )

    resolution = interceptor.resolve(
        Action(
            ActionType.MODIFY_RESOURCE,
            {"target": "诺艾尔", "resource": "hp", "amount": -10},
        )
    )

    assert characters.get("诺艾尔").mp == 16
    assert any(event["source"] == "灵智回流" for event in resolution.payload["skill_trigger_events"])


def test_travel_event_bus_exposes_supply_and_discovery_threshold() -> None:
    actor = _hero(skills={"充足补给": 3, "宝物猎人": 2})
    result = SkillTriggerManager().emit("travel_roll", actor, roll=3)

    assert result.effects[0].resource == "inventory_points"
    assert result.effects[0].amount == 3
    assert result.facts[0]["discovery_threshold"] == 3


class _FixedDice:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        assert low <= value <= high
        return value


def _spell_interceptor(hero: Character, enemy: Character, dice: list[int]) -> ActionInterceptor:
    characters = CharacterManager()
    characters.add(hero)
    characters.add(enemy)
    rules = RulesEngine()
    rules._rng = _FixedDice(dice)
    return ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())


def _spell_enemy() -> Character:
    return Character(
        name="铜壳卫兵",
        attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
        max_hp=100,
        hp=100,
        max_mp=30,
        mp=30,
        defenses={"physical": 10, "magic": 10},
        traits=["enemy"],
    )


def test_elementalist_spell_preparation_applies_barrage_and_cataclysm() -> None:
    hero = _hero(skills={"魔法炮击": 2, "天灾骤降": 2})
    hero.max_mp = 100
    hero.mp = 100
    hero.equipment = ["法杖"]
    hero.equipped_main_hand = "法杖"
    interceptor = _spell_interceptor(hero, _spell_enemy(), [3, 3])

    resolution = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "spell_name": "焰流",
                "cataclysm_extra_mp": 20,
            },
        )
    )

    assert resolution.payload["roll"].total == 10
    assert resolution.payload["roll"].damage == 38
    assert interceptor.character_manager.get("诺艾尔").mp == 60
    assert resolution.payload["spell_skill_preparation"]["sources"] == ["魔法炮击", "天灾骤降"]


def test_spellblade_uses_non_magic_weapon_formula_and_rejects_mp_over_limit() -> None:
    hero = _hero(skills={"以械引咒": 1, "天灾骤降": 1})
    hero.max_mp = 100
    hero.mp = 100
    hero.equipment = ["钢匕首", "法杖"]
    hero.equipped_main_hand = "钢匕首"
    hero.equipped_off_hand = "法杖"
    interceptor = _spell_interceptor(hero, _spell_enemy(), [3, 4])

    resolution = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "spell_name": "焰流",
                "use_weapon_formula": True,
                "spell_weapon": "钢匕首",
            },
        )
    )

    assert resolution.payload["roll"].attributes == ["DEX", "INS"]
    assert resolution.payload["roll"].modifier == 2
    assert resolution.payload["roll"].total == 9
    assert not resolution.payload["roll"].success

    interceptor.character_manager.get("诺艾尔").mp = 100
    blocked = interceptor.resolve(
        Action(
            ActionType.SPELL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "spell_name": "焰流",
                "cataclysm_extra_mp": 10,
                "use_weapon_formula": True,
                "spell_weapon": "钢匕首",
            },
        )
    )
    assert blocked.payload["skill_validation_failed"]
    assert interceptor.character_manager.get("诺艾尔").mp == 100


def test_desperate_survival_uses_status_count_for_checks_and_damage() -> None:
    actor = _hero()
    actor.hero_skills = ["绝处逢生"]
    actor.statuses = [StatusEffect.SLOW, StatusEffect.SHAKEN]
    manager = SkillTriggerManager()

    check = manager.emit("before_check", actor, attributes=["DEX", "INS"])
    damage = manager.emit("before_damage", actor, is_spell=False, is_melee=True)

    assert sum(effect.amount for effect in check.effects) == 2
    assert sum(effect.amount for effect in damage.effects) == 4


def test_perfect_aim_applies_two_warning_shot_effects() -> None:
    hero = _hero(skills={"威慑射击": 1})
    hero.hero_skills = ["完美瞄准"]
    enemy = _spell_enemy()
    interceptor = _spell_interceptor(hero, enemy, [8, 8])

    resolution = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "skill_name": "威慑射击",
                "options": [StatusEffect.SHAKEN.value, StatusEffect.SLOW.value],
            },
        )
    )

    target = interceptor.character_manager.get("铜壳卫兵")
    assert resolution.payload["roll"].success
    assert target.statuses == [StatusEffect.SHAKEN, StatusEffect.SLOW]
    assert len(resolution.payload["selected_effects"]) == 2


def test_reprise_repeats_orator_skill_and_pays_twice() -> None:
    hero = _hero(skills={"谴责": 1})
    hero.hero_skills = ["复诵"]
    second = _spell_enemy()
    second.name = "第二卫兵"
    characters = CharacterManager()
    characters.add(hero)
    characters.add(_spell_enemy())
    characters.add(second)
    rules = RulesEngine()
    rules._rng = _FixedDice([8, 8, 1, 1, 8, 8, 1, 1])
    interceptor = ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

    resolution = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "repeat_target": "第二卫兵",
                "skill_name": "谴责",
                "repeat": True,
            },
        )
    )

    assert interceptor.character_manager.get("诺艾尔").mp == 35
    assert StatusEffect.SHAKEN in interceptor.character_manager.get("铜壳卫兵").statuses
    assert StatusEffect.SHAKEN in interceptor.character_manager.get("第二卫兵").statuses
    assert "reprise" in resolution.payload


def test_loot_everything_uses_one_roll_for_multiple_souls_and_only_once_each() -> None:
    hero = _hero(skills={"窃取灵魂": 2})
    hero.hero_skills = ["洗劫一空"]
    hero.inventory_points = 0
    hero.max_inventory_points = 6
    second = _spell_enemy()
    second.name = "第二卫兵"
    characters = CharacterManager()
    characters.add(hero)
    characters.add(_spell_enemy())
    characters.add(second)
    rules = RulesEngine()
    rules._rng = _FixedDice([8, 8])
    conflict = ConflictManager(characters)
    interceptor = ActionInterceptor(rules, characters, ClockManager(), conflict, WorldState())
    action = Action(
        ActionType.SKILL,
        {
            "actor": "诺艾尔",
            "targets": ["铜壳卫兵", "第二卫兵"],
            "skill_name": "窃取灵魂",
        },
    )

    resolution = interceptor.resolve(action)
    interceptor.resolve(
        Action(
            ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "诺艾尔",
                    "effect": "自定义",
                    "description": "两枚灵魂宝藏发出不同回声。",
                    "window_id": interceptor.decision_window_manager.pending(
                        kind="critical_opportunity",
                        owner="诺艾尔",
                    )[0].window_id,
                },
            )
        )
    for target_name in ("铜壳卫兵", "第二卫兵"):
        target = interceptor.character_manager.get(target_name)
        target.trigger_cooldowns.clear()
    repeated = interceptor.resolve(action)

    assert resolution.payload["hit_targets"] == ["铜壳卫兵", "第二卫兵"]
    assert interceptor.character_manager.get("诺艾尔").inventory_points == 4
    assert "skill:soul_stolen" in interceptor.character_manager.get("铜壳卫兵").permanent_trigger_keys
    assert repeated.payload["already_stolen"] == ["铜壳卫兵", "第二卫兵"]


def test_soul_steal_reroll_replays_skill_effect_instead_of_only_changing_roll() -> None:
    hero = _hero(skills={"窃取灵魂": 2})
    hero.theme = "疑虑"
    hero.fabula_points = 2
    hero.inventory_points = 0
    hero.max_inventory_points = 6
    enemy = _spell_enemy()
    characters = CharacterManager()
    characters.add(hero)
    characters.add(enemy)
    rules = RulesEngine()
    rules._rng = _FixedDice([1, 2, 8])
    interceptor = ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )

    first = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "skill_name": "窃取灵魂",
            },
        )
    )
    assert first.payload["check_result_provisional"] is True
    assert characters.get("诺艾尔").inventory_points == 0
    assert "skill:soul_stolen" not in characters.get("铜壳卫兵").permanent_trigger_keys
    trait_window = next(
        window
        for window in first.payload["post_check_windows"]
        if window["kind"] == "trait_invocation"
    )

    replayed = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "诺艾尔",
                "trait_name": "疑虑",
                "reroll_indices": [0],
                "window_id": trait_window["window_id"],
            },
        )
    )
    assert replayed.payload["roll"].success is True
    assert replayed.payload["check_result_provisional"] is True
    assert characters.get("诺艾尔").inventory_points == 0

    accept_window = interceptor.decision_window_manager.pending(
        kind="trait_invocation",
        owner="诺艾尔",
        blocking_only=True,
    )[0]
    interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "诺艾尔",
                "post_check_acceptance": True,
                "window_id": accept_window.window_id,
            },
        )
    )
    assert characters.get("诺艾尔").inventory_points == 2
    assert "skill:soul_stolen" in characters.get("铜壳卫兵").permanent_trigger_keys


def test_provoke_reroll_keeps_opponent_roll_and_delays_mp_and_status_commit() -> None:
    hero = _hero(skills={"挑衅": 1})
    hero.theme = "愤怒"
    hero.fabula_points = 2
    hero.mp = 40
    enemy = _spell_enemy()
    characters = CharacterManager()
    characters.add(hero)
    characters.add(enemy)
    rules = RulesEngine()
    rules._rng = _FixedDice([1, 2, 4, 4, 8])
    interceptor = ActionInterceptor(
        rules,
        characters,
        ClockManager(),
        ConflictManager(characters),
        WorldState(),
    )

    first = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "target": "铜壳卫兵",
                "skill_name": "挑衅",
            },
        )
    )
    assert first.payload["roll"].success is False
    assert first.payload["check_result_provisional"] is True
    assert characters.get("诺艾尔").mp == 40
    assert StatusEffect.ENRAGED not in characters.get("铜壳卫兵").statuses
    trait_window = next(
        window
        for window in first.payload["post_check_windows"]
        if window["kind"] == "trait_invocation"
    )

    replayed = interceptor.resolve(
        Action(
            ActionType.INVOKE_TRAIT,
            {
                "actor": "诺艾尔",
                "trait_name": "愤怒",
                "reroll_indices": [0],
                "window_id": trait_window["window_id"],
            },
        )
    )
    assert replayed.payload["roll"].success is True
    assert replayed.payload["opposed_check"].right_roll.total == 8
    assert replayed.payload["check_result_provisional"] is True
    assert characters.get("诺艾尔").mp == 40
    assert StatusEffect.ENRAGED not in characters.get("铜壳卫兵").statuses

    accept_window = interceptor.decision_window_manager.pending(
        kind="trait_invocation",
        owner="诺艾尔",
        blocking_only=True,
    )[0]
    interceptor.resolve(
        Action(
            ActionType.NARRATE,
            {
                "actor": "诺艾尔",
                "post_check_acceptance": True,
                "window_id": accept_window.window_id,
            },
        )
    )
    assert characters.get("诺艾尔").mp == 35
    assert StatusEffect.ENRAGED in characters.get("铜壳卫兵").statuses


def test_gale_combo_concentrates_bladestorm_on_one_target() -> None:
    hero = _hero(skills={"利刃风暴": 1})
    hero.hero_skills = ["疾风连打"]
    hero.weapon_accuracy_attributes = ["DEX", "MIG"]
    hero.weapon_damage = 5
    enemy = _spell_enemy()
    interceptor = _spell_interceptor(hero, enemy, [4, 4])

    resolution = interceptor.resolve(
        Action(
            ActionType.SKILL,
            {
                "actor": "诺艾尔",
                "targets": ["铜壳卫兵"],
                "skill_name": "利刃风暴",
                "multi_attack": 2,
            },
        )
    )

    assert resolution.payload["roll"].total == 10
    assert resolution.payload["roll"].damage == 19
