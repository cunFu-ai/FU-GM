import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Character, StatusEffect


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class EquipmentEffectsTests(unittest.TestCase):
    def test_rare_weapon_profile_and_affinity_are_applied_when_equipped(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=2000))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))

        economy.buy_item("阿凛", "雷霆之弓", equip=True)

        hero = characters.get("阿凛")
        self.assertEqual(hero.equipped_main_hand, "雷霆之弓")
        self.assertEqual(hero.equipped_off_hand, "")
        self.assertEqual(hero.weapon_accuracy_attributes, ["DEX", "DEX"])
        self.assertEqual(hero.weapon_damage, 8)
        self.assertEqual(hero.weapon_type, "lightning")
        self.assertEqual(hero.weapon_range, "ranged")
        self.assertEqual(hero.equipment_affinities["lightning"], Affinity.RESIST)

    def test_weapon_can_target_magic_defense_automatically(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=1000))
        characters.add(self.enemy("铁卫", physical_defense=12, magic_defense=6))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "地狱指虎", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([3, 3])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "铁卫"}))

        roll = resolution.payload["roll"]
        self.assertEqual(roll.target_number, 6)
        self.assertTrue(roll.success)
        self.assertEqual(roll.damage_type, "dark")
        self.assertEqual(characters.get("铁卫").hp, 31)

    def test_on_hit_status_from_weapon_is_applied(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=3000))
        characters.add(self.enemy("魔兽"))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "毒蜂镖", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([5, 5])
        interceptor = self.interceptor(rules, characters)

        interceptor.resolve(Action(ActionType.ATTACK, {"actor": "阿凛", "target": "魔兽"}))

        self.assertIn(StatusEffect.POISONED, characters.get("魔兽").statuses)

    def test_armor_shield_and_accessory_defenses_are_recalculated(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=5000, abilities=["可装备职业盔甲"]))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))

        economy.buy_item("阿凛", "水晶板甲", equip=True)
        economy.buy_item("阿凛", "巫术戒指", equip=True)

        hero = characters.get("阿凛")
        self.assertEqual(hero.defenses["physical"], 11)
        self.assertEqual(hero.defenses["magic"], 9)
        self.assertEqual(hero.initiative, -3)
        self.assertEqual(hero.equipment_affinities["dark"], Affinity.RESIST)

    def test_accessory_status_immunity_blocks_new_statuses(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=3000))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))

        economy.buy_item("阿凛", "蜕生手套", equip=True)

        self.assertFalse(characters.add_status("阿凛", StatusEffect.SLOW))
        self.assertFalse(characters.add_status("阿凛", StatusEffect.POISONED))
        self.assertEqual(characters.get("阿凛").statuses, [])

    def test_spell_bonus_and_spell_damage_bonus_are_used(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=5000, mp=40))
        characters.add(self.enemy("魔偶", magic_defense=10))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))
        economy.buy_item("阿凛", "黑色罩袍", equip=True)
        economy.buy_item("阿凛", "黄色尖帽", equip=True)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 5])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(ActionType.SPELL, {"actor": "阿凛", "target": "魔偶", "spell_name": "光照射线"})
        )

        roll = resolution.payload["roll"]
        self.assertEqual(roll.total, 10)
        self.assertTrue(roll.success)
        self.assertEqual(roll.damage, 25)
        self.assertEqual(characters.get("魔偶").hp, 15)

    def interceptor(self, rules, characters):
        return ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

    def hero(self, name, *, zenit=0, hp=40, mp=40, abilities=None) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=hp,
            max_mp=60,
            mp=mp,
            crisis_threshold=20,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
            fabula_points=3,
            inventory_points=6,
            max_inventory_points=6,
            zenit=zenit,
            abilities=abilities or [],
        )

    def enemy(self, name, *, hp=40, physical_defense=10, magic_defense=10) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=max(40, hp),
            hp=hp,
            max_mp=20,
            mp=20,
            crisis_threshold=max(40, hp) // 2,
            defenses={"physical": physical_defense, "magic": magic_defense},
            traits=["enemy"],
        )


if __name__ == "__main__":
    unittest.main()
