import unittest

from fu_gm.components.character_creation_manager import (
    HP_BONUS_CLASSES,
    IP_BONUS_CLASSES,
    MARTIAL_ARMOR_CLASSES,
    MARTIAL_MELEE_CLASSES,
    MARTIAL_RANGED_CLASSES,
    MARTIAL_SHIELD_CLASSES,
    MP_BONUS_CLASSES,
)
from fu_gm.components.skill_trigger_manager import gm_judgement_windows, gm_judgement_windows_for
from fu_gm.models import Character
from fu_gm.skill_library import (
    CLASS_REFERENCES,
    CLASS_SKILL_REFERENCES,
    CORE_CLASS_NAMES,
    HERO_SKILL_REFERENCES,
    NPC_SKILL_REFERENCES,
    SKILL_COVERAGE_GM_JUDGEMENT,
    SKILL_COVERAGE_HARD_RULE,
    SKILL_COVERAGE_PASSIVE_HARD,
    SKILL_COVERAGE_REFERENCE_ONLY,
    SPELL_GRANTING_SKILLS,
    get_class_reference,
    get_skill_reference,
    normalize_skill_reference_name,
    search_class_references,
    search_skill_references,
    skill_choice_requirements,
    skill_choice_specs,
    skill_implementation_coverage,
    skill_implementation_table,
)


class SkillLibraryTests(unittest.TestCase):
    def test_class_catalog_matches_the_authoritative_skill_classes(self) -> None:
        self.assertEqual(
            tuple(reference.name for reference in CLASS_REFERENCES),
            CORE_CLASS_NAMES,
        )
        guardian = get_class_reference("守护者")
        self.assertIsNotNone(guardian)
        self.assertEqual(guardian.hp_bonus, 5)
        self.assertIn("可装备职业盾牌", guardian.abilities)
        self.assertEqual(
            {item.name for item in CLASS_REFERENCES if item.hp_bonus},
            HP_BONUS_CLASSES,
        )
        self.assertEqual(
            {item.name for item in CLASS_REFERENCES if item.mp_bonus},
            MP_BONUS_CLASSES,
        )
        self.assertEqual(
            {item.name for item in CLASS_REFERENCES if item.ip_bonus},
            IP_BONUS_CLASSES,
        )
        abilities = {item.name: set(item.abilities) for item in CLASS_REFERENCES}
        self.assertEqual(
            {name for name, values in abilities.items() if "可装备职业近战武器" in values},
            MARTIAL_MELEE_CLASSES,
        )
        self.assertEqual(
            {name for name, values in abilities.items() if "可装备职业远程武器" in values},
            MARTIAL_RANGED_CLASSES,
        )
        self.assertEqual(
            {name for name, values in abilities.items() if "可装备职业盔甲" in values},
            MARTIAL_ARMOR_CLASSES,
        )
        self.assertEqual(
            {name for name, values in abilities.items() if "可装备职业盾牌" in values},
            MARTIAL_SHIELD_CLASSES,
        )

    def test_class_catalog_searches_role_tags_without_loading_skills(self) -> None:
        matches = search_class_references(tags=["旅行"])

        self.assertEqual([reference.name for reference in matches], ["旅人"])

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

    def test_acquisition_choices_are_catalog_data_not_prompt_special_cases(self) -> None:
        self.assertEqual(
            SPELL_GRANTING_SKILLS,
            {
                "元素魔法": "元素使法术",
                "熵系魔法": "熵术士法术",
                "灵魂魔法": "御魂使法术",
            },
        )
        element_magic = skill_choice_specs("元素魔法")[0]
        portable = skill_choice_specs("便携装置")[0]
        chimerist = skill_choice_specs("拟兽系仪式")[0]

        self.assertEqual(element_magic.storage_field, "spells")
        self.assertEqual(element_magic.count_mode, "per_rank")
        self.assertEqual(element_magic.option_source, "元素使法术")
        self.assertEqual(portable.storage_field, "skill_options")
        self.assertEqual(portable.count_mode, "per_rank")
        self.assertEqual(
            portable.options,
            ("炼金装置", "注魔装置", "魔导装置"),
        )
        self.assertEqual(
            chimerist.options,
            ("洞察+意志", "力量+意志"),
        )

    def test_optional_creation_choices_do_not_become_hard_draft_blockers(self) -> None:
        requirements = skill_choice_requirements(
            {"契约与召唤": 1, "忠诚伙伴": 2},
            include_optional=True,
        )
        by_key = {row["choice_key"]: row for row in requirements}

        self.assertFalse(by_key["initial_arcanum"]["required_for_creation"])
        self.assertEqual(
            by_key["initial_arcanum"]["timing"],
            "character_creation_optional",
        )
        self.assertFalse(by_key["companion_profile"]["required_for_creation"])
        self.assertEqual(
            by_key["companion_profile"]["timing"],
            "before_first_use",
        )
        self.assertEqual(
            skill_choice_requirements(
                {"契约与召唤": 1, "忠诚伙伴": 2},
                include_optional=False,
            ),
            [],
        )

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
