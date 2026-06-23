import unittest

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

    def test_alias_lookup_keeps_project_canonical_skill_names(self) -> None:
        self.assertEqual(normalize_skill_reference_name("奥灵回响(+4)"), "奥灵回响")
        self.assertEqual(normalize_skill_reference_name("便携装置（+5）"), "便携装置")
        self.assertEqual(normalize_skill_reference_name("御魂系仪式"), "御魂系仪式")
        self.assertEqual(normalize_skill_reference_name("技术升级"), "升级")

        self.assertEqual(get_skill_reference("奥灵回响").name, "奥灵回响")
        self.assertEqual(get_skill_reference("洗劫一空").name, "劫掠")

    def test_search_can_filter_by_kind_class_and_alias_text(self) -> None:
        tinkerer = search_skill_references(kind="class", class_name="造物使", text="炼金")
        heroic = search_skill_references(kind="hero", text="近战")
        npc = search_skill_references(kind="npc", text="召唤")

        self.assertTrue(any(skill.name == "便携装置" for skill in tinkerer))
        self.assertTrue(any(skill.name == "强力攻击" for skill in heroic))
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
                SKILL_COVERAGE_REFERENCE_ONLY,
            },
        )

        self.assertEqual(skill_implementation_coverage("暗影击").category, SKILL_COVERAGE_HARD_RULE)
        self.assertEqual(skill_implementation_coverage("元素魔法").category, SKILL_COVERAGE_PASSIVE_HARD)
        self.assertEqual(skill_implementation_coverage("野性之语").category, SKILL_COVERAGE_GM_JUDGEMENT)
        self.assertEqual(skill_implementation_coverage("奥灵回响").category, SKILL_COVERAGE_REFERENCE_ONLY)
        self.assertEqual(skill_implementation_coverage("强力攻击").category, SKILL_COVERAGE_PASSIVE_HARD)


if __name__ == "__main__":
    unittest.main()
