import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.trigger_manager import TriggerManager
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Character, RestType, StatusEffect


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class TriggerManagerTests(unittest.TestCase):
    def test_story_ring_can_convert_critical_opportunity_to_fabula_point(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=2000))
        characters.add(self.enemy("史莱姆"))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "物语之戒", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "史莱姆"}))

        self.assertEqual(characters.get("阿凛").fabula_points, 4)
        self.assertIn("物语之戒", resolution.rules_text)
        self.assertEqual(resolution.payload["trigger_results"][0].source, "物语之戒")

    def test_beginner_boots_grant_xp_on_fumble(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=1000))
        characters.add(self.enemy("史莱姆"))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "新手靴", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 1])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "史莱姆"}))

        self.assertEqual(characters.get("阿凛").fabula_points, 4)
        self.assertEqual(characters.get("阿凛").experience_points, 1)
        self.assertIn("新手靴", resolution.rules_text)

    def test_rebirth_ring_prevents_zero_hp_once_until_rest(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", hp=5, zenit=4000, physical_defense=6))
        characters.add(self.enemy("暗骑士", weapon_damage=20))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "重生之戒", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "暗骑士", "target": "阿凛"}))

        hero = characters.get("阿凛")
        self.assertEqual(hero.hp, 1)
        self.assertTrue(hero.trigger_cooldowns)
        self.assertNotIn("conflict_event", resolution.payload)
        self.assertTrue(resolution.payload["trigger_results"][0].prevented_zero_hp)

        RestManager(characters, ClockManager()).rest(RestType.SETTLEMENT, safe_source="旅馆")

        self.assertEqual(hero.trigger_cooldowns, set())

    def test_weapon_hit_can_restore_mp(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", mp=10, zenit=2000))
        characters.add(self.enemy("魔兽", physical_defense=8))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "莫瑞甘", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 4])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "魔兽"}))

        self.assertEqual(characters.get("阿凛").mp, 20)
        self.assertIn("莫瑞甘", resolution.rules_text)

    def test_weapon_kill_can_restore_inventory_points(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", inventory_points=0, zenit=2000))
        characters.add(self.enemy("飞贼", hp=10, physical_defense=6))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "劫掠弓", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 3])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "飞贼"}))

        self.assertEqual(characters.get("阿凛").inventory_points, 2)
        self.assertIn("劫掠弓", resolution.rules_text)

    def test_spell_hit_trigger_from_equipped_item_applies_status(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", mp=40, zenit=2000))
        characters.add(self.enemy("魔偶", magic_defense=10))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "死灵之书", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([5, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(ActionType.SPELL, {"actor": "阿凛", "target": "魔偶", "spell_name": "光照射线"})
        )

        self.assertIn(StatusEffect.SHAKEN, characters.get("魔偶").statuses)
        self.assertIn("死灵之书", resolution.rules_text)

    def test_travel_discovery_trigger_can_grant_fabula_point(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=1000))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "游荡者之靴", equip=True)
        trigger_manager = TriggerManager(characters)

        results = trigger_manager.on_travel_discovery(["阿凛"])

        self.assertEqual(characters.get("阿凛").fabula_points, 4)
        self.assertEqual(results[0].source, "游荡者之靴")

    def interceptor(self, rules, characters):
        return ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

    def hero(
        self,
        name,
        *,
        hp=40,
        mp=60,
        inventory_points=6,
        zenit=0,
        physical_defense=10,
    ) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=hp,
            max_mp=60,
            mp=mp,
            crisis_threshold=20,
            defenses={"physical": physical_defense, "magic": 10},
            traits=["pc"],
            fabula_points=3,
            inventory_points=inventory_points,
            max_inventory_points=6,
            zenit=zenit,
        )

    def enemy(
        self,
        name,
        *,
        hp=40,
        physical_defense=10,
        magic_defense=10,
        weapon_damage=6,
    ) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=max(40, hp),
            hp=hp,
            max_mp=20,
            mp=20,
            crisis_threshold=max(40, hp) // 2,
            defenses={"physical": physical_defense, "magic": magic_defense},
            traits=["enemy"],
            weapon_damage=weapon_damage,
        )


if __name__ == "__main__":
    unittest.main()
