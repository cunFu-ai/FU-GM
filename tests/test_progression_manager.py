import unittest

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character, RestType, StatusEffect
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class ProgressionManagerTests(unittest.TestCase):
    def test_award_session_experience_uses_fabula_average_and_marks_level_up_ready(self) -> None:
        characters = CharacterManager()
        characters.add(self.pc("瓦莉亚", experience_points=4))
        characters.add(self.pc("米菈", experience_points=0))
        world_state = WorldState()
        manager = ProgressionManager(characters, world_state)

        report = manager.award_session_experience(
            participating_pcs=["瓦莉亚", "米菈"],
            ultima_spent=3,
            fabula_spent=5,
        )

        self.assertEqual(report.total_xp, 10)
        self.assertEqual(report.fabula_xp, 2)
        self.assertEqual(characters.get("瓦莉亚").experience_points, 14)
        self.assertTrue(report.gains[0].can_level_up)
        self.assertIn("阶段经验", world_state.memories[-1])

    def test_level_up_adds_class_level_skill_and_keeps_current_hp_mp_unchanged(self) -> None:
        characters = CharacterManager()
        hero = self.pc("瓦莉亚", experience_points=10, hp=12, mp=3)
        hero.classes = {"武器大师": 5, "守护者": 3}
        hero.skills = {"近战武器掌握": 4, "保镖": 1, "防御精通": 2, "挺身守护": 1}
        characters.add(hero)
        manager = ProgressionManager(characters)

        result = manager.level_up("瓦莉亚", class_name="守护者", skill_name="防御精通")
        updated = characters.get("瓦莉亚")

        self.assertEqual(updated.level, 6)
        self.assertEqual(updated.experience_points, 0)
        self.assertEqual(updated.classes["守护者"], 4)
        self.assertEqual(updated.skills["防御精通"], 3)
        self.assertEqual(updated.max_hp, 46)
        self.assertEqual(updated.max_mp, 41)
        self.assertEqual(updated.hp, 12)
        self.assertEqual(updated.mp, 3)
        self.assertEqual(result.class_level_after, 4)

    def test_level_up_accepts_extracted_skill_and_hero_skill_aliases(self) -> None:
        characters = CharacterManager()
        hero = self.pc("诺亚", level=9, experience_points=10)
        hero.classes = {"造物使": 9, "御魂使": 5}
        hero.skills = {"便携装置": 5, "秘密配方": 4, "灵魂魔法": 5}
        characters.add(hero)
        manager = ProgressionManager(characters)

        result = manager.level_up("诺亚", class_name="造物使", skill_name="先见之明", hero_skill="技术升级")
        updated = characters.get("诺亚")

        self.assertEqual(result.class_name, "造物使")
        self.assertEqual(result.skill_name, "先见之明")
        self.assertIn("升级", updated.hero_skills)

    def test_character_can_only_level_up_once_per_session_even_with_extra_xp(self) -> None:
        characters = CharacterManager()
        hero = self.pc("瓦莉亚", experience_points=25)
        characters.add(hero)
        manager = ProgressionManager(characters)

        manager.level_up("瓦莉亚", class_name="守护者", skill_name="防御精通")

        self.assertEqual(characters.get("瓦莉亚").experience_points, 15)
        self.assertFalse(manager.can_level_up("瓦莉亚"))

    def test_level_twenty_requires_and_applies_attribute_increase(self) -> None:
        characters = CharacterManager()
        hero = self.pc("米菈", level=19, experience_points=10)
        hero.attributes["WLP"] = 8
        hero.max_mp = 64
        hero.classes = {"元素使": 9, "御魂使": 5, "博学家": 5}
        hero.skills = {"元素魔法": 9, "灵魂魔法": 5, "集中": 5}
        characters.add(hero)
        manager = ProgressionManager(characters)

        result = manager.level_up(
            "米菈",
            class_name="元素使",
            skill_name="元素魔法",
            attribute_increase="WLP",
            hero_skill="强力咒语",
        )
        updated = characters.get("米菈")

        self.assertEqual(updated.level, 20)
        self.assertEqual(updated.attributes["WLP"], 10)
        self.assertEqual(updated.max_mp, 75)
        self.assertIn("强力咒语", updated.hero_skills)
        self.assertEqual(result.mastered_class, "元素使")

    def test_status_immunity_hero_skill_blocks_future_statuses(self) -> None:
        characters = CharacterManager()
        hero = self.pc("旅人", level=9, experience_points=10)
        hero.classes = {"旅人": 9, "浪客": 5, "御魂使": 5}
        hero.skills = {"忠实的伙伴": 5, "足智多谋": 4, "窃取灵魂": 5, "灵魂魔法": 5}
        hero.statuses = [StatusEffect.SLOW]
        characters.add(hero)
        manager = ProgressionManager(characters)

        manager.level_up(
            "旅人",
            class_name="旅人",
            skill_name="酒馆闲聊",
            hero_skill="状态免疫",
            status_immunity=StatusEffect.SLOW,
        )

        self.assertIn(StatusEffect.SLOW, characters.get("旅人").permanent_status_immunities)
        self.assertNotIn(StatusEffect.SLOW, characters.get("旅人").statuses)
        self.assertFalse(characters.add_status("旅人", StatusEffect.SLOW))

    def test_powerful_strike_hero_skill_adds_damage_and_weapon_mastery_adds_accuracy(self) -> None:
        characters = CharacterManager()
        attacker = self.pc("瓦莉亚")
        attacker.weapon_damage = 6
        attacker.skills = {"近战武器掌握": 2}
        attacker.hero_skills = ["强力攻击"]
        target = Character(
            name="训练魔像",
            attributes={"DEX": 6, "MIG": 6, "INS": 6, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=10,
            mp=10,
            defenses={"physical": 10, "magic": 10},
            traits=["enemy"],
        )
        characters.add(attacker)
        characters.add(target)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        interceptor = ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "训练魔像",
                    "attributes": ["DEX", "MIG"],
                    "is_melee": True,
                },
            )
        )

        roll = resolution.payload["roll"]
        self.assertEqual(roll.total, 10)
        self.assertTrue(roll.success)
        self.assertEqual(roll.damage, 15)
        self.assertEqual(characters.get("训练魔像").hp, 35)

    def test_big_pockets_reduces_inventory_point_cost_for_tent(self) -> None:
        characters = CharacterManager()
        hero = self.pc("诺亚")
        hero.inventory_points = 4
        hero.hero_skills = ["大口袋"]
        characters.add(hero)
        rest = RestManager(characters, ClockManager())

        result = rest.rest(RestType.WILDERNESS, safe_source="魔法帐篷", payer="诺亚")

        self.assertEqual(result.ip_spent, 3)
        self.assertEqual(characters.get("诺亚").inventory_points, 1)

    def test_orchestrator_exposes_session_experience_and_level_up(self) -> None:
        characters = CharacterManager()
        hero = self.pc("诺亚", experience_points=5)
        hero.classes = {"造物使": 5, "御魂使": 3}
        hero.skills = {"便携装置": 5, "灵魂魔法": 3}
        characters.add(hero)
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(RulesEngine(), characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
        )

        report = app.award_session_experience(participating_pcs=["诺亚"], ultima_spent=0, fabula_spent=0)
        result = app.level_up_character("诺亚", class_name="御魂使", skill_name="灵魂魔法")

        self.assertEqual(report.total_xp, 5)
        self.assertEqual(result.level_after, 6)
        self.assertTrue(world_state.memories)

    def pc(self, name, *, level=5, experience_points=0, hp=40, mp=40) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=hp,
            max_mp=40,
            mp=mp,
            level=level,
            crisis_threshold=22,
            inventory_points=6,
            max_inventory_points=6,
            fabula_points=3,
            experience_points=experience_points,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
            classes={"武器大师": 5, "守护者": 3},
            skills={"近战武器掌握": 4, "保镖": 1, "防御精通": 2, "挺身守护": 1},
        )


if __name__ == "__main__":
    unittest.main()
