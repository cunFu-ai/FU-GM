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


class Phase2RulesTests(unittest.TestCase):
    def test_inventory_remedy_spends_ip_and_heals(self) -> None:
        characters = CharacterManager()
        hero = self.hero("阿凛", hp=10, inventory_points=6)
        characters.add(hero)
        interceptor = self.interceptor(RulesEngine(), characters)

        resolution = interceptor.resolve(
            Action(ActionType.USE_INVENTORY, {"actor": "阿凛", "item_name": "治疗剂", "target": "阿凛"})
        )

        self.assertEqual(characters.get("阿凛").inventory_points, 3)
        self.assertEqual(characters.get("阿凛").hp, 40)
        self.assertIn("治疗剂", resolution.rules_text)

    def test_inventory_elixir_clears_all_statuses(self) -> None:
        characters = CharacterManager()
        hero = self.hero("阿凛", inventory_points=6)
        hero.statuses = [StatusEffect.SLOW, StatusEffect.POISONED]
        characters.add(hero)
        conflict = ConflictManager(characters)
        conflict.state.active_statuses["阿凛"] = [StatusEffect.SLOW, StatusEffect.POISONED]
        interceptor = self.interceptor(RulesEngine(), characters, conflict)

        resolution = interceptor.resolve(
            Action(ActionType.USE_INVENTORY, {"actor": "阿凛", "item_name": "万能药", "target": "阿凛"})
        )

        self.assertEqual(characters.get("阿凛").inventory_points, 4)
        self.assertEqual(characters.get("阿凛").statuses, [])
        self.assertNotIn("阿凛", conflict.state.active_statuses)
        self.assertIn("解除所有异常状态", resolution.rules_text)

    def test_alchemy_damage_uses_target_and_effect_rolls(self) -> None:
        characters = CharacterManager()
        hero = self.hero(
            "造物使",
            inventory_points=6,
            skills={"便携装置": 1},
            skill_options={"便携装置": ["炼金装置"]},
        )
        enemy = self.enemy("帝国机甲", hp=40)
        characters.add(hero)
        characters.add(enemy)
        rules = RulesEngine()
        rules._rng = FakeRandom([8, 9])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.TINKERER_GADGET,
                {
                    "actor": "造物使",
                    "gadget_type": "alchemy",
                    "tier": "basic",
                    "target_roll": 7,
                    "effect_roll": 4,
                },
            )
        )

        self.assertEqual(characters.get("造物使").inventory_points, 3)
        self.assertEqual(characters.get("帝国机甲").hp, 20)
        self.assertEqual(resolution.payload["gadget_result"].damage_results[0]["damage_type"], "lightning")

    def test_attack_with_infusion_spends_ip_changes_damage_type_and_bonus(self) -> None:
        characters = CharacterManager()
        hero = self.hero(
            "造物使",
            inventory_points=6,
            weapon_damage=4,
            skills={"便携装置": 1},
            skill_options={"便携装置": ["注魔装置"]},
        )
        enemy = self.enemy("帝国机甲", hp=40, physical_defense=8)
        enemy.affinities["lightning"] = Affinity.WEAK
        characters.add(hero)
        characters.add(enemy)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        interceptor = self.interceptor(rules, characters)

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "造物使",
                    "target": "帝国机甲",
                    "attributes": ["DEX", "MIG"],
                    "infusion_name": "电压",
                },
            )
        )

        self.assertEqual(characters.get("造物使").inventory_points, 4)
        self.assertEqual(resolution.payload["roll"].damage_type, "lightning")
        self.assertEqual(resolution.payload["roll"].damage, 26)
        self.assertEqual(characters.get("帝国机甲").hp, 14)

    def test_venom_infusion_applies_poison_on_hit(self) -> None:
        characters = CharacterManager()
        hero = self.hero(
            "造物使",
            inventory_points=6,
            weapon_damage=4,
            skills={"便携装置": 3},
            skill_options={"便携装置": ["注魔装置", "注魔装置", "注魔装置"]},
        )
        enemy = self.enemy("帝国士兵", hp=40, physical_defense=8)
        characters.add(hero)
        characters.add(enemy)
        rules = RulesEngine()
        rules._rng = FakeRandom([4, 4])
        interceptor = self.interceptor(rules, characters)

        interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "造物使",
                    "target": "帝国士兵",
                    "attributes": ["DEX", "MIG"],
                    "infusion_name": "毒液",
                },
            )
        )

        self.assertIn(StatusEffect.POISONED, characters.get("帝国士兵").statuses)

    def test_magicannon_equips_ranged_weapon(self) -> None:
        characters = CharacterManager()
        hero = self.hero(
            "造物使",
            inventory_points=6,
            skills={"便携装置": 2},
            skill_options={"便携装置": ["魔导装置", "魔导装置"]},
        )
        characters.add(hero)
        interceptor = self.interceptor(RulesEngine(), characters)

        interceptor.resolve(
            Action(
                ActionType.TINKERER_GADGET,
                {
                    "actor": "造物使",
                    "gadget_type": "magitech",
                    "mode": "魔法加农炮",
                    "target": "帝国机甲",
                    "damage_type": "fire",
                },
            )
        )

        updated = characters.get("造物使")
        self.assertEqual(updated.inventory_points, 4)
        self.assertEqual(updated.equipped_main_hand, "魔法加农炮（fire）")
        self.assertEqual(updated.weapon_range, "ranged")
        self.assertEqual(updated.weapon_type, "fire")

    def test_shop_restock_and_equipment_permission(self) -> None:
        characters = CharacterManager()
        hero = self.hero("阿凛", inventory_points=2, zenit=500)
        characters.add(hero)
        interceptor = self.interceptor(RulesEngine(), characters)

        interceptor.resolve(
            Action(ActionType.SHOP, {"actor": "阿凛", "mode": "restock", "item_name": "库存点", "quantity": 3})
        )

        self.assertEqual(characters.get("阿凛").inventory_points, 5)
        self.assertEqual(characters.get("阿凛").zenit, 470)
        interceptor.resolve(
            Action(
                ActionType.SHOP,
                {
                    "actor": "阿凛",
                    "mode": "buy",
                    "item_name": "细剑",
                    "equip": False,
                },
            )
        )
        self.assertIn("细剑", characters.get("阿凛").equipment)
        with self.assertRaisesRegex(ValueError, "职业近战武器"):
            interceptor.resolve(
                Action(
                    ActionType.EQUIP,
                    {
                        "actor": "阿凛",
                        "items": ["细剑"],
                    },
                )
            )

    def test_open_rare_chest_persists_asset(self) -> None:
        characters = CharacterManager()
        hero = self.hero("阿凛", zenit=0)
        characters.add(hero)
        world = WorldState()
        interceptor = self.interceptor(RulesEngine(), characters, world_state=world)

        resolution = interceptor.resolve(
            Action(
                ActionType.OPEN_CHEST,
                {"actor": "阿凛", "chest_name": "月井宝箱", "fixed_item": "星屑罗盘", "fixed_zenit": 120},
            )
        )

        self.assertEqual(characters.get("阿凛").zenit, 120)
        self.assertIn("星屑罗盘", characters.get("阿凛").equipment)
        self.assertEqual(world.persistent_changes[0].name, "星屑罗盘")
        self.assertIn("月井宝箱", resolution.rules_text)
        self.assertIn("宝箱硬结算", resolution.payload["chest_reward"].hard_rule_summary)
        self.assertIn("奖励来历", resolution.payload["chest_reward"].llm_narrative_prompt)

    def test_elite_enemy_gets_extra_turn_before_round_advances(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("瓦莉亚"))
        characters.add(self.enemy("精英机甲"))
        conflict = ConflictManager(characters)
        conflict.register_enemy("精英机甲", EnemyRank.ELITE)
        conflict.start_scene("桥头战", ["瓦莉亚", "精英机甲"])

        self.assertEqual(conflict.next_turn(), "精英机甲")
        self.assertEqual(conflict.next_turn(), "精英机甲")
        self.assertIn("奖励回合", conflict.format_phase())
        self.assertEqual(conflict.next_turn(), "瓦莉亚")
        self.assertEqual(conflict.state.round_number, 2)

    def interceptor(self, rules, characters, conflict=None, world_state=None):
        return ActionInterceptor(
            rules,
            characters,
            ClockManager(),
            conflict or ConflictManager(characters),
            world_state or WorldState(),
        )

    def hero(
        self,
        name,
        *,
        hp=40,
        mp=40,
        inventory_points=6,
        zenit=0,
        weapon_damage=4,
        abilities=None,
        skills=None,
        skill_options=None,
    ) -> Character:
        return Character(
            name=name,
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
            inventory_points=inventory_points,
            max_inventory_points=6,
            zenit=zenit,
            abilities=abilities or [],
            skills=skills or {},
            skill_options=skill_options or {},
        )

    def enemy(self, name, *, hp=40, mp=20, physical_defense=10) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=max(40, hp),
            hp=hp,
            max_mp=max(20, mp),
            mp=mp,
            crisis_threshold=max(40, hp) // 2,
            defenses={"physical": physical_defense, "magic": 10},
            traits=["enemy"],
        )


if __name__ == "__main__":
    unittest.main()
