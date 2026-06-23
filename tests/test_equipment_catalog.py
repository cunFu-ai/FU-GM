import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.equipment_catalog import (
    ARTIFACT_EXAMPLES,
    BASIC_ARMOR_EXAMPLES,
    BASIC_EQUIPMENT_EXAMPLES,
    BASIC_SHIELD_EXAMPLES,
    BASIC_WEAPON_EXAMPLES,
    EQUIPMENT_EXAMPLES_BY_NAME,
    RARE_ACCESSORY_EXAMPLES,
    RARE_ARMOR_EXAMPLES,
    RARE_EQUIPMENT_EXAMPLES,
    RARE_SHIELD_EXAMPLES,
    RARE_WEAPON_EXAMPLES,
    get_equipment_example,
    search_equipment_examples,
)
from fu_gm.models import Character, EquipmentItemType


class EquipmentCatalogTests(unittest.TestCase):
    def test_catalog_contains_all_extracted_example_tables(self) -> None:
        self.assertEqual(len(BASIC_WEAPON_EXAMPLES), 21)
        self.assertEqual(len(BASIC_ARMOR_EXAMPLES), 9)
        self.assertEqual(len(BASIC_SHIELD_EXAMPLES), 2)
        self.assertEqual(len(BASIC_EQUIPMENT_EXAMPLES), 32)
        self.assertEqual(len(RARE_WEAPON_EXAMPLES), 113)
        self.assertEqual(len(RARE_ARMOR_EXAMPLES), 22)
        self.assertEqual(len(RARE_SHIELD_EXAMPLES), 12)
        self.assertEqual(len(RARE_ACCESSORY_EXAMPLES), 29)
        self.assertEqual(len(ARTIFACT_EXAMPLES), 11)
        self.assertEqual(len(RARE_EQUIPMENT_EXAMPLES), 176)

    def test_lookup_and_aliases_work(self) -> None:
        item = get_equipment_example("雷霆之弓")
        alias_item = get_equipment_example("案镇魔之书")

        self.assertIsNotNone(item)
        self.assertEqual(item.damage_type, "lightning")
        self.assertEqual(item.category, "弓")
        self.assertEqual(alias_item.name, "镇魔之书")

    def test_basic_equipment_templates_are_available_for_reference(self) -> None:
        shuriken = get_equipment_example("手里剑")
        improvised = get_equipment_example("临时武器(远程)")
        silk = get_equipment_example("丝质衬衫")

        self.assertEqual(shuriken.category, "投掷")
        self.assertEqual(shuriken.damage_bonus, 4)
        self.assertEqual(improvised.name, "临时武器（远程）")
        self.assertEqual(silk.physical_defense, "DEX+1")
        self.assertIn("basic", silk.tags)

    def test_search_filters_by_type_price_damage_and_text(self) -> None:
        lightning_bows = search_equipment_examples(
            item_type=EquipmentItemType.WEAPON,
            category="弓",
            max_price=1200,
            damage_type="lightning",
        )
        anti_status_items = search_equipment_examples(text="免疫所有异常状态")

        self.assertEqual([item.name for item in lightning_bows], ["雷霆之弓"])
        self.assertIn("断钢剑", [item.name for item in anti_status_items])
        self.assertIn("蜕生手套", [item.name for item in anti_status_items])

    def test_artifacts_are_excluded_unless_requested(self) -> None:
        without_artifacts = search_equipment_examples(text="星门")
        with_artifacts = search_equipment_examples(text="星门", include_artifacts=True)

        self.assertEqual(without_artifacts, [])
        self.assertEqual(with_artifacts[0].name, "星门魔典")

    def test_economy_manager_prices_and_persists_catalog_items(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="阿凛",
                attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=40,
                mp=40,
                defenses={"physical": 10, "magic": 10},
                traits=["pc"],
            )
        )
        world = WorldState()
        economy = EconomyManager(characters, world, RulesEngine(seed=1))

        self.assertEqual(economy.item_price("雷霆之弓"), 1000)
        reward = economy.open_chest("阿凛", "雷鸣宝箱", fixed_item="雷霆之弓", fixed_zenit=0)

        self.assertIn("雷霆之弓", reward.rare_items)
        self.assertIn("雷霆之弓", characters.get("阿凛").equipment)
        self.assertIn("雷系伤害", world.persistent_changes[0].description)
        self.assertIn("宝箱硬结算", reward.hard_rule_summary)
        self.assertIn("不得改变金币数", reward.llm_narrative_prompt)

    def test_database_index_has_no_missing_key_examples(self) -> None:
        for expected_name in ["祝福权杖", "戈尔贡之眼", "生化板甲", "精金塔盾", "重生之戒", "风之鳞"]:
            self.assertIn(expected_name, EQUIPMENT_EXAMPLES_BY_NAME)


if __name__ == "__main__":
    unittest.main()
