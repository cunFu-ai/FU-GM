import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.core_bestiary import CORE_BESTIARY_ENTRIES, bestiary_entry_by_name, search_bestiary_entries
from fu_gm.models import (
    Affinity,
    Character,
    DungeonExploreMode,
    DungeonImportance,
    DungeonPreparation,
    EncounterDifficulty,
    EnemyRank,
    EquipmentItemType,
    StatusEffect,
)


class GMDesignToolsTests(unittest.TestCase):
    def test_core_bestiary_examples_are_structured_and_searchable(self) -> None:
        self.assertEqual(len(CORE_BESTIARY_ENTRIES), 56)
        self.assertEqual(len({entry.name for entry in CORE_BESTIARY_ENTRIES}), len(CORE_BESTIARY_ENTRIES))

        lamp = bestiary_entry_by_name("魔法提灯")
        self.assertIsNotNone(lamp)
        assert lamp is not None
        self.assertEqual(lamp.level, 5)
        self.assertEqual(lamp.species, "构装体")
        self.assertEqual(lamp.affinities["poison"], Affinity.IMMUNE)
        self.assertIn(StatusEffect.POISONED, lamp.status_immunities)
        self.assertIn("构装体", lamp.bestiary_header())

        flyers = search_bestiary_entries(text="飞行")
        self.assertIn("锋翼鸟", {entry.name for entry in flyers})
        low_beasts = search_bestiary_entries(species="野兽", max_level=5)
        self.assertEqual({"巨齿百足虫", "硕鼠", "灰嚎怪", "吸血蝙蝠"}, {entry.name for entry in low_beasts})
        self.assertEqual(8, len(search_bestiary_entries(species="人型")))
        self.assertEqual(8, len(search_bestiary_entries(species="不死族")))
        self.assertFalse([entry.name for entry in CORE_BESTIARY_ENTRIES if not (entry.attacks or entry.spells or entry.other_actions)])
        self.assertEqual(2, len(bestiary_entry_by_name("陷龙花").attacks))
        self.assertTrue(bestiary_entry_by_name("陷龙花").spells)
        self.assertIn("吞噬脱困", " ".join(bestiary_entry_by_name("陷龙花").traits_rules))

    def test_dungeon_design_uses_generation_tables_and_starts_clocks(self) -> None:
        clocks = ClockManager()
        manager = DungeonManager(clocks, RulesEngine(seed=1))

        brief = manager.design_dungeon(
            "巴别尔遗核",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 5, "focus": 17, "inhabitants": 12, "peculiarity": 12},
        )
        state = manager.start_from_brief(brief, location="旧帝国荒原")

        self.assertEqual(brief.concept, "魔导巨像内部")
        self.assertEqual(brief.focus, "一部魔导科技战争机器的原型机")
        self.assertEqual(brief.recommended_mode, DungeonExploreMode.DETAILED)
        self.assertIn("巴别尔遗核：高度警戒", state.danger_clocks)
        self.assertTrue(clocks.exists("巴别尔遗核：高度警戒"))
        self.assertIn("不要为了地下城而地下城", " ".join(state.notes))
        self.assertIn("一部魔导科技战争机器的原型机", manager.format_status())

    def test_improvised_minor_dungeon_is_simplified(self) -> None:
        manager = DungeonManager(ClockManager(), RulesEngine(seed=2))

        brief = manager.design_dungeon(
            importance="minor",
            preparation="improvised",
            rolls={"concept": 19, "focus": 11, "inhabitants": 2, "peculiarity": 8},
        )

        self.assertEqual(brief.recommended_mode, DungeonExploreMode.SKIP)
        self.assertEqual(len(brief.danger_clocks), 1)
        self.assertIn("单次团队检定", " ".join(brief.guidance))

    def test_reward_budget_follows_party_level_and_player_count_table(self) -> None:
        economy = self.economy()

        budget = economy.reward_budget(party_level=26, pc_count=5)

        self.assertEqual(budget.tier, 20)
        self.assertEqual(budget.average_value, 2000)
        self.assertEqual(budget.max_item_value, 1500)

    def test_reward_budget_uses_corrected_level_40_four_player_value(self) -> None:
        economy = self.economy()

        budget = economy.reward_budget(party_level=42, pc_count=4)

        self.assertEqual(budget.average_value, 4000)
        self.assertIsNone(budget.max_item_value)

    def test_session_reward_uses_budget_instead_of_flat_level_formula(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛"))
        characters.add(self.hero("白河"))
        characters.add(self.hero("织夜"))
        economy = self.economy(characters)

        reward = economy.award_session_treasure(["阿凛", "白河", "织夜"], party_level=5)

        self.assertEqual(reward.zenit, 750)
        self.assertEqual(characters.get("阿凛").zenit, 250)
        self.assertIn("平均奖励 750Z", reward.summary)
        self.assertIn("阶段奖励硬结算", reward.hard_rule_summary)
        self.assertIn("授奖场面", reward.llm_narrative_prompt)

    def test_rare_weapon_design_applies_quality_price_and_martial_limit(self) -> None:
        economy = self.economy()

        design = economy.design_rare_weapon(
            "银焰指虎",
            "铁指虎",
            damage_type="light",
            accuracy_bonus=1,
            extra_damage_bonus=4,
            quality_names=["护身符"],
        )

        self.assertEqual(design.price, 1350)
        self.assertEqual(design.damage_bonus, 10)
        self.assertEqual(design.accuracy_modifier, 1)
        self.assertEqual(design.required_ability, "可装备职业近战武器")
        self.assertEqual(design.qualities[0].name, "护身符")
        self.assertIn("强力数值修正", " ".join(design.notes))

    def test_rare_protective_item_design_supports_accessories(self) -> None:
        economy = self.economy()

        design = economy.design_rare_protective_item(
            "探险家腰带",
            "腰带",
            item_type=EquipmentItemType.ACCESSORY,
            quality_names=["先攻强化"],
        )

        self.assertEqual(design.price, 500)
        self.assertEqual(design.item_type, EquipmentItemType.ACCESSORY)
        self.assertEqual(design.qualities[0].description, "你的先攻获得 +4 修正值。")

    def test_encounter_design_uses_soldier_equivalent_guidelines(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", level=12, max_hp=50))
        characters.add(self.hero("白河", level=10, max_hp=40))
        characters.add(self.hero("织夜", level=10, max_hp=45))
        encounter = EncounterManager(characters, ConflictManager(characters))

        design = encounter.design_encounter(["阿凛", "白河", "织夜"], difficulty=EncounterDifficulty.HARD)

        self.assertEqual(design.party_level, 12)
        self.assertEqual(design.soldier_equivalent, 4)
        self.assertEqual(design.expected_enemy_damage, 15)
        self.assertIn("敌人进入危机状态", design.transparency_notes[0])
        self.assertIn("有意义的战斗", design.battle_principles[0])
        self.assertEqual(design.ideal_duration_rounds, "3-4")
        self.assertIn("三场简单战斗", design.resource_pressure_notes[0])
        self.assertIn("队伍等级 +5", design.level_relationship_notes[1])

    def test_npc_design_builds_stats_species_rules_and_rank_budget(self) -> None:
        encounter = EncounterManager(CharacterManager(), ConflictManager(CharacterManager()))

        draft = encounter.design_npc(
            "火法师安吉拉",
            level=20,
            species="人型",
            traits=["野心", "傲慢", "渊博", "冷酷"],
            attribute_overrides={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 10},
            weaknesses=["冰"],
            additional_affinities={"火": "抗", "暗": "抗"},
            armor_initiative_modifier=-2,
            physical_defense=9,
            magic_defense=10,
            rank=EnemyRank.ELITE,
            selected_skill_names=["施法者", "伤害抵抗", "特殊攻击"],
        )

        self.assertEqual(draft.bestiary_header(), "火法师安吉拉\n20级·人型（精英，等效 2 名小兵）")
        self.assertEqual(draft.max_hp, 160)
        self.assertEqual(draft.crisis_threshold, 80)
        self.assertEqual(draft.max_mp, 70)
        self.assertEqual(draft.initiative, 8)
        self.assertEqual(draft.defenses, {"physical": 9, "magic": 10})
        self.assertEqual(draft.check_bonus, 2)
        self.assertEqual(draft.extra_damage, 5)
        self.assertEqual(draft.skill_budget, 7)
        self.assertEqual(draft.action_count, 2)
        self.assertEqual(draft.affinities["ice"], Affinity.WEAK)
        self.assertEqual(draft.affinities["fire"], Affinity.RESIST)
        self.assertIn("该物种可装备物品", " ".join(draft.notes))
        self.assertEqual([skill.name for skill in draft.selected_skills], ["施法者", "伤害抵抗", "特殊攻击"])

    def test_npc_species_rules_apply_affinities_and_status_immunities(self) -> None:
        encounter = EncounterManager(CharacterManager(), ConflictManager(CharacterManager()))

        construct = encounter.design_npc(
            "魔法提灯",
            level=5,
            species="构装体",
            attribute_overrides={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
        )
        undead = encounter.design_npc("白骨骑士", level=5, species="不死族")
        plant = encounter.design_npc("仙人掌幼体", level=5, species="植物", weaknesses=["火"])

        self.assertEqual(construct.max_hp, 40)
        self.assertEqual(construct.max_mp, 55)
        self.assertEqual(construct.affinities["poison"], Affinity.IMMUNE)
        self.assertEqual(construct.affinities["earth"], Affinity.RESIST)
        self.assertIn(StatusEffect.POISONED, construct.status_immunities)
        self.assertEqual(undead.affinities["light"], Affinity.WEAK)
        self.assertEqual(undead.affinities["dark"], Affinity.IMMUNE)
        self.assertIn(StatusEffect.DAZED, plant.status_immunities)
        self.assertIn(StatusEffect.SHAKEN, plant.status_immunities)
        self.assertIn(StatusEffect.ENRAGED, plant.status_immunities)
        self.assertEqual(plant.skill_budget, 4)

    def test_battle_mechanic_suggestions_keep_boss_parts_optional(self) -> None:
        encounter = EncounterManager(CharacterManager(), ConflictManager(CharacterManager()))

        normal = encounter.battle_mechanic_suggestions()
        boss = encounter.battle_mechanic_suggestions(boss=True)

        self.assertTrue(any("守卫" in item for item in normal))
        self.assertTrue(any("元素光环" in item for item in normal))
        self.assertFalse(any("多部件" in item for item in normal))
        self.assertTrue(any("多阶段" in item for item in boss))
        self.assertTrue(any("不要默认套用" in item for item in boss if "多部件" in item))

    def test_enemy_level_relationship_warns_without_forcing_design(self) -> None:
        encounter = EncounterManager(CharacterManager(), ConflictManager(CharacterManager()))

        self.assertIn("较为合适", encounter.enemy_level_relationship(10, 15))
        self.assertIn("大威胁", encounter.enemy_level_relationship(10, 20))
        self.assertIn("过强", encounter.enemy_level_relationship(10, 22))

    def test_apply_elite_and_champion_rank_templates(self) -> None:
        characters = CharacterManager()
        characters.add(self.enemy("精英魔像", max_hp=40, max_mp=20, initiative=8))
        characters.add(self.enemy("巴别尔核心", max_hp=50, max_mp=30, initiative=9))
        conflict = ConflictManager(characters)
        encounter = EncounterManager(characters, conflict)

        elite = encounter.apply_rank_template("精英魔像", EnemyRank.ELITE)
        champion = encounter.apply_rank_template("巴别尔核心", EnemyRank.CHAMPION, champion_value=3, ultima_points=5, is_villain=True)

        self.assertEqual(elite.max_hp, 80)
        self.assertEqual(elite.initiative, 10)
        self.assertEqual(conflict.state.enemy_action_counts["精英魔像"], 2)
        self.assertEqual(champion.max_hp, 150)
        self.assertEqual(champion.max_mp, 60)
        self.assertEqual(champion.initiative, 12)
        self.assertEqual(conflict.state.enemy_action_counts["巴别尔核心"], 3)
        self.assertIn("villain", champion.traits)

    def economy(self, characters=None):
        return EconomyManager(characters or CharacterManager(), WorldState(), RulesEngine(seed=1))

    def hero(self, name, *, level=5, max_hp=40) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=max_hp,
            hp=max_hp,
            max_mp=40,
            mp=40,
            level=level,
            crisis_threshold=max_hp // 2,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
        )

    def enemy(self, name, *, max_hp=40, max_mp=20, initiative=8) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=max_hp,
            hp=max_hp,
            max_mp=max_mp,
            mp=max_mp,
            level=5,
            crisis_threshold=max_hp // 2,
            defenses={"physical": 10, "magic": 10},
            traits=["enemy"],
            initiative=initiative,
        )


if __name__ == "__main__":
    unittest.main()
