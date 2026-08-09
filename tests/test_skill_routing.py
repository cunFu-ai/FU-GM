import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Character, EnemyRank, StatusEffect


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class SkillRoutingTests(unittest.TestCase):
    def test_shadow_strike_spends_hp_and_routes_to_dark_attack(self) -> None:
        characters = CharacterManager()
        attacker = self.hero("瓦莉亚", hp=40, weapon_damage=4, skills={"暗影击": 2})
        target = self.enemy("帝国机甲", hp=50, physical_defense=8)
        characters.add(attacker)
        characters.add(target)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 4, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "瓦莉亚",
                    "target": "帝国机甲",
                    "skill_name": "暗影击",
                    "attributes": ["DEX", "MIG"],
                },
            )
        )

        self.assertEqual(characters.get("瓦莉亚").hp, 37)
        self.assertEqual(resolution.payload["roll"].damage_type, "dark")
        self.assertEqual(resolution.payload["roll"].damage, 14)
        self.assertEqual(characters.get("帝国机甲").hp, 36)

    def test_multi_target_attack_uses_one_roll_against_each_defense(self) -> None:
        characters = CharacterManager()
        attacker = self.hero("弓手", weapon_damage=4)
        first = self.enemy("帝国士兵A", hp=40, physical_defense=9)
        second = self.enemy("帝国士兵B", hp=40, physical_defense=12)
        characters.add(attacker)
        characters.add(first)
        characters.add(second)
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 4])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "弓手",
                    "targets": ["帝国士兵A", "帝国士兵B"],
                    "attributes": ["DEX", "MIG"],
                },
            )
        )

        self.assertEqual(len(resolution.payload["rolls"]), 2)
        self.assertTrue(resolution.payload["rolls"][0].success)
        self.assertFalse(resolution.payload["rolls"][1].success)
        self.assertEqual(characters.get("帝国士兵A").hp, 30)
        self.assertEqual(characters.get("帝国士兵B").hp, 40)

    def test_barrage_spends_mp_and_routes_ranged_multi_attack(self) -> None:
        characters = CharacterManager()
        attacker = self.hero("神射手", mp=40, weapon_damage=4, skills={"弹幕射击": 1})
        attacker.weapon_range = "ranged"
        first = self.enemy("帝国士兵A", hp=40, physical_defense=10)
        second = self.enemy("帝国士兵B", hp=40, physical_defense=10)
        characters.add(attacker)
        characters.add(first)
        characters.add(second)
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "神射手",
                    "targets": ["帝国士兵A", "帝国士兵B"],
                    "skill_name": "弹幕射击",
                    "attributes": ["DEX", "MIG"],
                },
            )
        )

        self.assertEqual(characters.get("神射手").mp, 30)
        self.assertTrue(all(roll.success for roll in resolution.payload["rolls"]))
        self.assertEqual(characters.get("帝国士兵A").hp, 30)
        self.assertEqual(characters.get("帝国士兵B").hp, 30)

    def test_crossfire_reaction_cancels_ranged_attack(self) -> None:
        characters = CharacterManager()
        attacker = self.enemy("帝国狙击手", hp=40, physical_defense=10)
        attacker.weapon_damage = 8
        attacker.weapon_range = "ranged"
        target = self.hero("瓦莉亚", hp=40)
        reactor = self.hero("神射手", mp=50, skills={"干涉火力": 1})
        reactor.weapon_range = "ranged"
        characters.add(attacker)
        characters.add(target)
        characters.add(reactor)
        rules = RulesEngine()
        rules._rng = FakeRandom([5, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "帝国狙击手",
                    "target": "瓦莉亚",
                    "attributes": ["DEX", "MIG"],
                    "is_melee": False,
                    "reactions": [{"actor": "神射手", "skill_name": "干涉火力"}],
                },
            )
        )

        self.assertEqual(characters.get("瓦莉亚").hp, 40)
        self.assertEqual(characters.get("神射手").mp, 35)
        self.assertFalse(resolution.payload["roll"].success)
        self.assertTrue(resolution.payload["reaction_events"][0]["cancelled"])

    def test_counter_attack_reaction_triggers_after_even_melee_roll(self) -> None:
        characters = CharacterManager()
        attacker = self.enemy("帝国剑士", hp=40, physical_defense=10)
        attacker.weapon_damage = 8
        defender = self.hero("武器大师", hp=40, weapon_damage=4, skills={"反击": 1})
        characters.add(attacker)
        characters.add(defender)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 3, 5, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "帝国剑士",
                    "target": "武器大师",
                    "attributes": ["DEX", "MIG"],
                    "is_melee": True,
                    "reactions": [{"actor": "武器大师", "skill_name": "反击"}],
                },
            )
        )

        self.assertEqual(characters.get("武器大师").hp, 40)
        self.assertEqual(characters.get("帝国剑士").hp, 36)
        counter_events = [event for event in resolution.payload["reaction_events"] if event.get("skill_name") == "反击"]
        self.assertTrue(counter_events[0]["triggered"])
        self.assertEqual(counter_events[0]["roll"].high_roll, 0)

    def test_counter_attack_waits_for_its_own_trait_reroll_before_damage(self) -> None:
        characters = CharacterManager()
        attacker = self.enemy("帝国剑士", hp=40, physical_defense=10)
        attacker.weapon_damage = 8
        defender = self.hero(
            "武器大师",
            hp=40,
            weapon_damage=4,
            skills={"反击": 1},
        )
        defender.identity = "百战不退的剑士"
        defender.fabula_points = 1
        characters.add(attacker)
        characters.add(defender)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 3, 2, 3, 5, 5])
        interceptor = self.interceptor(rules, characters)

        first = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "帝国剑士",
                    "target": "武器大师",
                    "attributes": ["DEX", "MIG"],
                    "is_melee": True,
                    "reactions": [{"actor": "武器大师", "skill_name": "反击"}],
                },
            )
        )

        self.assertEqual(characters.get("帝国剑士").hp, 40)
        counter = next(
            event
            for event in first.payload["reaction_events"]
            if event.get("skill_name") == "反击"
        )
        self.assertTrue(counter["check_result_provisional"])
        trait_window = next(
            window
            for window in counter["decision_windows"]
            if window["kind"] == "trait_invocation"
        )

        revised = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "武器大师",
                    "window_id": trait_window["window_id"],
                    "trait_name": "百战不退的剑士",
                    "invocation_rationale": "百战不退的经验让他在敌刃掠过后立刻抓住反击角度。",
                },
            )
        )

        self.assertTrue(revised.payload["roll"].success)
        self.assertEqual(revised.payload["roll"].high_roll, 0)
        self.assertEqual(characters.get("帝国剑士").hp, 36)
        self.assertEqual(characters.get("武器大师").fabula_points, 0)

    def test_condemn_spends_mp_damages_target_mp_and_applies_status(self) -> None:
        characters = CharacterManager()
        speaker = self.hero("米菈", skills={"谴责": 4})
        target = self.enemy("帝国军官", mp=50, magic_defense=10)
        characters.add(speaker)
        characters.add(target)
        rules = RulesEngine()
        rules._rng = FakeRandom([5, 5, 1, 1])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "米菈",
                    "target": "帝国军官",
                    "skill_name": "谴责",
                    "status_effect": "shaken",
                },
            )
        )

        self.assertEqual(characters.get("米菈").mp, 35)
        self.assertEqual(characters.get("帝国军官").mp, 10)
        self.assertIn(StatusEffect.SHAKEN, characters.get("帝国军官").statuses)
        self.assertEqual(resolution.payload["target_resource_change"].amount, -40)

    def test_class_name_misrouted_as_skill_can_be_coerced_to_hinder(self) -> None:
        characters = CharacterManager()
        speaker = self.hero("艾薇娅", skills={"谴责": 1})
        target = self.enemy("监察官艾蕾娜", magic_defense=10)
        characters.add(speaker)
        characters.add(target)
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "艾薇娅",
                    "target": "监察官艾蕾娜",
                    "skill_name": "游说家",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "status_effect": "shaken",
                    "reasoning": "艾薇娅用游说家的方式妨碍监察官，指出她是在剥夺选择。",
                },
            )
        )

        self.assertEqual(resolution.action.action_type, ActionType.HINDER)
        self.assertTrue(resolution.payload["roll"].success)
        self.assertIn(StatusEffect.SHAKEN, characters.get("监察官艾蕾娜").statuses)

    def test_encourage_heals_and_registers_attribute_buff(self) -> None:
        characters = CharacterManager()
        speaker = self.hero("诺亚", skills={"鼓舞": 6})
        target = self.hero("露琪亚", hp=10)
        characters.add(speaker)
        characters.add(target)
        conflict = ConflictManager(characters)
        interceptor = self.interceptor(RulesEngine(), characters, conflict)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "诺亚",
                    "target": "露琪亚",
                    "skill_name": "鼓舞",
                    "chosen_attribute": "DEX",
                },
            )
        )

        self.assertEqual(characters.get("诺亚").mp, 35)
        self.assertEqual(characters.get("露琪亚").hp, 40)
        self.assertEqual(characters.get("露琪亚").attribute_bonuses["DEX"], 1)
        self.assertEqual(resolution.payload["healing_change"].amount, 30)

    def test_disarming_rhetoric_removes_shaken_soldier_from_conflict(self) -> None:
        characters = CharacterManager()
        speaker = self.hero("吟游诗人", mp=60, hero_skills=["卸甲真言"])
        target = self.enemy("帝国士兵", level=6, hp=12, statuses=[StatusEffect.SHAKEN])
        characters.add(speaker)
        characters.add(target)
        conflict = ConflictManager(characters)
        conflict.start_scene("城门谈判", ["吟游诗人", "帝国士兵"])
        conflict.register_enemy("帝国士兵", EnemyRank.SOLDIER)
        interceptor = self.interceptor(RulesEngine(), characters, conflict)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "吟游诗人",
                    "target": "帝国士兵",
                    "skill_name": "卸甲真言",
                },
            )
        )

        self.assertEqual(characters.get("吟游诗人").mp, 37)
        self.assertIn("帝国士兵", conflict.state.escaped_combatants)
        self.assertNotIn("帝国士兵", conflict.state.turn_order)
        self.assertIn("和平离开冲突", resolution.rules_text)

    def test_arcanist_summons_frost_arcanum_with_emergency_and_regeneration(self) -> None:
        characters = CharacterManager()
        arcanist = self.hero(
            "奥灵使",
            hp=10,
            mp=60,
            skills={"契约与召唤": 1, "险境召唤": 2, "奥灵疗愈": 2},
            bound_arcana=["霜"],
        )
        characters.add(arcanist)
        conflict = ConflictManager(characters)
        interceptor = self.interceptor(RulesEngine(), characters, conflict)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "奥灵使",
                    "skill_name": "契约与召唤",
                    "mode": "summon",
                    "arcanum": "霜",
                },
            )
        )

        active = characters.get("奥灵使")
        self.assertEqual(active.mp, 30)
        self.assertEqual(active.hp, 20)
        self.assertEqual(active.active_arcanum, "霜")
        self.assertEqual(active.bound_arcana, ["霜"])
        self.assertEqual(active.temporary_affinities["ice"], Affinity.RESIST)
        self.assertIn(StatusEffect.ENRAGED, active.temporary_status_immunities)
        self.assertIn("冰系抗性", resolution.rules_text)

        conflict.end_scene()

        cleared = characters.get("奥灵使")
        self.assertEqual(cleared.active_arcanum, "")
        self.assertNotIn("ice", cleared.temporary_affinities)
        self.assertNotIn(StatusEffect.ENRAGED, cleared.temporary_status_immunities)

    def test_arcanist_dismisses_sky_arcanum_as_thunderstorm(self) -> None:
        characters = CharacterManager()
        arcanist = self.hero("奥灵使", mp=60, skills={"契约与召唤": 1}, bound_arcana=["天空"])
        enemy = self.enemy("帝国机甲", hp=100)
        characters.add(arcanist)
        characters.add(enemy)
        conflict = ConflictManager(characters)
        interceptor = self.interceptor(RulesEngine(), characters, conflict)
        interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "奥灵使",
                    "skill_name": "契约与召唤",
                    "mode": "summon",
                    "arcanum": "天空",
                },
            )
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "奥灵使",
                    "skill_name": "契约与召唤",
                    "mode": "dismiss",
                    "arcanum": "天空",
                    "targets": ["帝国机甲"],
                },
            )
        )

        self.assertEqual(characters.get("帝国机甲").hp, 70)
        self.assertEqual(characters.get("奥灵使").active_arcanum, "")
        self.assertNotIn("lightning", characters.get("奥灵使").temporary_affinities)
        self.assertIn("雷暴", resolution.rules_text)

    def test_sword_arcanum_turns_attacks_into_untyped_bonus_damage(self) -> None:
        characters = CharacterManager()
        attacker = self.hero("奥术剑士", mp=60, weapon_damage=4, skills={"契约与召唤": 1}, bound_arcana=["剑"])
        target = self.enemy("帝国机甲", hp=60, physical_defense=8)
        target.affinities["fire"] = Affinity.IMMUNE
        characters.add(attacker)
        characters.add(target)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        interceptor = self.interceptor(rules, characters)
        interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "奥术剑士",
                    "skill_name": "契约与召唤",
                    "mode": "summon",
                    "arcanum": "剑",
                },
            )
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "奥术剑士",
                    "target": "帝国机甲",
                    "attributes": ["DEX", "MIG"],
                    "damage_type": "fire",
                },
            )
        )

        self.assertEqual(resolution.payload["roll"].damage_type, "none")
        self.assertEqual(resolution.payload["roll"].damage, 13)
        self.assertEqual(characters.get("帝国机甲").hp, 47)

    def test_arcanist_cannot_summon_without_recorded_contract(self) -> None:
        characters = CharacterManager()
        arcanist = self.hero("奥灵使", mp=60, skills={"契约与召唤": 1})
        characters.add(arcanist)
        interceptor = self.interceptor(RulesEngine(), characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "奥灵使",
                    "skill_name": "契约与召唤",
                    "mode": "summon",
                    "arcanum": "霜",
                },
            )
        )

        self.assertTrue(resolution.payload["skill_failed"])
        self.assertEqual(characters.get("奥灵使").active_arcanum, "")

    def test_unimplemented_but_known_skill_cannot_fake_a_successful_action(self) -> None:
        characters = CharacterManager()
        hero = self.hero("奥灵使", skills={"奥灵回响": 1})
        characters.add(hero)
        interceptor = self.interceptor(RulesEngine(), characters)

        with self.assertRaisesRegex(ValueError, "没有可直接提交的技能行动执行器"):
            interceptor.resolve(
                Action(
                    ActionType.SKILL,
                    {
                        "actor": "奥灵使",
                        "target": "帝国士兵",
                        "skill_name": "奥灵回响",
                    },
                )
            )

    def interceptor(self, rules, characters, conflict=None):
        return ActionInterceptor(
            rules,
            characters,
            ClockManager(),
            conflict or ConflictManager(characters),
            WorldState(),
        )

    def hero(
        self,
        name,
        *,
        level=5,
        hp=40,
        mp=40,
        weapon_damage=4,
        skills=None,
        hero_skills=None,
        bound_arcana=None,
    ) -> Character:
        return Character(
            name=name,
            level=level,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=hp,
            max_mp=60,
            mp=mp,
            crisis_threshold=20,
            weapon_damage=weapon_damage,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
            fabula_points=3,
            inventory_points=6,
            max_inventory_points=6,
            skills=skills or {},
            hero_skills=hero_skills or [],
            bound_arcana=bound_arcana or [],
        )

    def enemy(
        self,
        name,
        *,
        level=5,
        hp=40,
        mp=20,
        physical_defense=10,
        magic_defense=10,
        statuses=None,
    ) -> Character:
        return Character(
            name=name,
            level=level,
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=max(40, hp),
            hp=hp,
            max_mp=max(20, mp),
            mp=mp,
            crisis_threshold=max(40, hp) // 2,
            defenses={"physical": physical_defense, "magic": magic_defense},
            traits=["enemy"],
            statuses=statuses or [],
        )


if __name__ == "__main__":
    unittest.main()
