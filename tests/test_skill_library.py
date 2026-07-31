import unittest

from fu_gm.components.skill_trigger_manager import gm_judgement_windows, gm_judgement_windows_for
from fu_gm.models import Character
from fu_gm.skill_library import (
    CLASS_SKILL_REFERENCES,
    HERO_SKILL_REFERENCES,
    NPC_SKILL_REFERENCES,
    SKILL_COVERAGE_GM_JUDGEMENT,
    SKILL_COVERAGE_HARD_RULE,
    SKILL_COVERAGE_PASSIVE_HARD,
    SKILL_COVERAGE_REFERENCE_ONLY,
    get_skill_reference,
    normalize_skill_reference_name,
    search_skill_references,
    skill_implementation_coverage,
    skill_implementation_table,
)


class SkillLibraryTests(unittest.TestCase):
    def test_library_contains_extracted_class_hero_and_npc_skill_references(self) -> None:
        self.assertEqual(len(CLASS_SKILL_REFERENCES), 75)
        self.assertEqual(len(HERO_SKILL_REFERENCES), 31)
        self.assertGreaterEqual(len(NPC_SKILL_REFERENCES), 16)

    def test_alias_lookup_uses_rulebook_canonical_skill_names(self) -> None:
        self.assertEqual(normalize_skill_reference_name("奥灵回响(+4)"), "奥灵回响")
        self.assertEqual(normalize_skill_reference_name("便携装置（+5）"), "便携装置")
        self.assertEqual(normalize_skill_reference_name("御魂系仪式"), "御魂系仪式")
        self.assertEqual(normalize_skill_reference_name("技术升级"), "技术升级")
        self.assertEqual(normalize_skill_reference_name("升级"), "技术升级")

        self.assertEqual(get_skill_reference("奥灵回响").name, "奥灵回响")
        self.assertEqual(get_skill_reference("洗劫一空").name, "洗劫一空")
        self.assertEqual(get_skill_reference("劫掠").name, "洗劫一空")

    def test_search_can_filter_by_kind_class_and_alias_text(self) -> None:
        tinkerer = search_skill_references(kind="class", class_name="造物使", text="炼金")
        heroic = search_skill_references(kind="hero", text="近战")
        npc = search_skill_references(kind="npc", text="召唤")

        self.assertTrue(any(skill.name == "便携装置" for skill in tinkerer))
        self.assertTrue(any(skill.name == "猛力打击" for skill in heroic))
        self.assertTrue(any(skill.name == "特殊行动" for skill in npc))

    def test_skill_implementation_coverage_classifies_all_core_skills(self) -> None:
        class_rows = skill_implementation_table(kind="class")
        hero_rows = skill_implementation_table(kind="hero")
        categories = {row.category for row in class_rows + hero_rows}

        self.assertEqual(len(class_rows), 75)
        self.assertEqual(len(hero_rows), 31)
        self.assertEqual(
            categories,
            {
                SKILL_COVERAGE_HARD_RULE,
                SKILL_COVERAGE_PASSIVE_HARD,
                SKILL_COVERAGE_GM_JUDGEMENT,
            },
        )

        self.assertEqual(skill_implementation_coverage("暗影击").category, SKILL_COVERAGE_HARD_RULE)
        self.assertEqual(skill_implementation_coverage("元素魔法").category, SKILL_COVERAGE_PASSIVE_HARD)
        self.assertEqual(
            skill_implementation_coverage("野性之语").category,
            SKILL_COVERAGE_PASSIVE_HARD,
        )
        self.assertEqual(
            skill_implementation_coverage("忠诚伙伴").category,
            SKILL_COVERAGE_HARD_RULE,
        )
        self.assertEqual(skill_implementation_coverage("奥灵回响").category, SKILL_COVERAGE_GM_JUDGEMENT)
        self.assertEqual(skill_implementation_coverage("肾上腺素").category, SKILL_COVERAGE_PASSIVE_HARD)
        self.assertEqual(skill_implementation_coverage("强力攻击").category, SKILL_COVERAGE_PASSIVE_HARD)
        self.assertEqual(skill_implementation_coverage("猛力打击").category, SKILL_COVERAGE_PASSIVE_HARD)
        self.assertEqual(skill_implementation_coverage("天灾骤降").category, SKILL_COVERAGE_HARD_RULE)
        self.assertEqual(skill_implementation_coverage("魔法炮击").category, SKILL_COVERAGE_HARD_RULE)
        self.assertEqual(skill_implementation_coverage("以械引咒").category, SKILL_COVERAGE_HARD_RULE)
        self.assertEqual(skill_implementation_coverage("双盾战士").category, SKILL_COVERAGE_HARD_RULE)

    def test_gm_judgement_windows_cover_high_priority_reference_skills(self) -> None:
        windows = {window.skill: window for window in gm_judgement_windows()}

        self.assertIn("奥灵回响", windows)
        self.assertIn("痛楚", windows)
        self.assertIn("苦痛教训", windows)
        self.assertIn("阴狠手段", windows)
        self.assertIn("治愈之力", windows)
        self.assertIn("顺势施放", windows["奥灵回响"].guidance)
        self.assertIn("技能等级", windows["奥灵回响"].guidance)

    def test_gm_judgement_windows_filter_by_actor_skills(self) -> None:
        hero = Character(
            name="卡米拉",
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=50,
            mp=50,
            skills={"苦痛教训": 1, "阴狠手段": 2},
        )

        windows = gm_judgement_windows_for(hero)
        names = {window.skill for window in windows}

        self.assertEqual(names, {"苦痛教训", "阴狠手段"})

    def test_no_fixed_core_skill_is_left_reference_only(self) -> None:
        rows = skill_implementation_table()
        self.assertFalse([row.name for row in rows if row.category == SKILL_COVERAGE_REFERENCE_ONLY])


if __name__ == "__main__":
    unittest.main()
