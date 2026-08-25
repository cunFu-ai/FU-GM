from __future__ import annotations

import unittest

from fu_gm.components.character_card_manager import (
    CHARACTER_CARD_SCHEMA,
    CharacterCardError,
    CharacterCardManager,
)
from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import Bond, HeroCreationProfile


def profile() -> HeroCreationProfile:
    return HeroCreationProfile(
        player_name="阿凛",
        hero_name="米菈",
        identity="逃离财团实验室的魔导技师",
        theme="自由",
        origin="永雨工业城下层",
        classes={"造物使": 2, "御魂使": 2, "守护者": 1},
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        bonds=[Bond(target="永雨工业城下层", emotions=["信赖"])],
        skills={"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
        skill_options={"便携装置": ["魔导装置"]},
        spells=["治愈", "护盾"],
        equipment=["钢匕首", "符文盾", "旅行装束"],
        notes=["她知道辉钢财团的地下能源管线。"],
    )


class CharacterCardTests(unittest.TestCase):
    def manager(self) -> tuple[CharacterCreationManager, CharacterCardManager]:
        creation = CharacterCreationManager(CharacterManager(), WorldState())
        return creation, CharacterCardManager(creation)

    def test_catalog_exposes_authoritative_creation_choices(self) -> None:
        _creation, cards = self.manager()

        catalog = cards.catalog()

        self.assertEqual(catalog["schema"], CHARACTER_CARD_SCHEMA)
        self.assertEqual(len(catalog["classes"]), 15)
        self.assertEqual(catalog["equipment_budget"], 500)
        guardian = next(item for item in catalog["classes"] if item["name"] == "守护者")
        self.assertIn("可装备职业盾牌", guardian["benefit"]["abilities"])
        self.assertTrue(any(item["name"] == "铁壁" for item in guardian["skills"]))
        spell_names = [item["name"] for item in catalog["spells"]]
        self.assertIn("治愈术", spell_names)
        self.assertNotIn("治愈", spell_names)
        self.assertEqual(catalog["spell_aliases"]["治愈"], "治愈术")

    def test_text_export_contains_rules_data_but_no_visual_payload(self) -> None:
        creation, cards = self.manager()
        character = creation.create_player_character(profile(), fate_roll=(2, 5)).character
        character.appearance = {"hair": "绝不能出现在文本里的银白短发"}
        character.portrait = {
            "asset_url": "/private/mira.png",
            "positive_prompt": "secret portrait prompt",
        }
        card = cards.export_character(character, profile=profile())

        exported = cards.export_character_text(card)

        self.assertIn("《最终物语》角色卡", exported)
        self.assertIn("角色名：米菈", exported)
        self.assertIn("- 治愈术", exported)
        self.assertIn("- 屏障", exported)
        self.assertIn("主手 钢匕首", exported)
        self.assertNotIn("银白短发", exported)
        self.assertNotIn("secret portrait prompt", exported)
        self.assertNotIn("asset_url", exported)

    def test_card_round_trip_preserves_roll_presentation_and_extensions(self) -> None:
        source_creation, source_cards = self.manager()
        created = source_creation.create_player_character(
            profile(),
            fate_roll=(2, 5),
        )
        created.character.card_id = "hero-mira"
        created.character.appearance = {"hair": "银白短发", "eyes": "琥珀色"}
        created.character.portrait = {"model_profile": "anima", "seed": 73}
        created.character.extensions = {
            "cunfu.homebrew": {"features": ["巨岩之外的自定义内容"]}
        }

        exported = source_cards.export_character(created.character)

        target_creation, target_cards = self.manager()
        imported, imported_profile, warnings = target_cards.import_card(exported)

        self.assertEqual(warnings, [])
        self.assertEqual(imported.card_id, "hero-mira")
        self.assertEqual(imported.creation_fate_roll, [2, 5])
        self.assertEqual(imported.zenit, 170)
        self.assertEqual(imported.appearance["hair"], "银白短发")
        self.assertEqual(imported.portrait["seed"], 73)
        self.assertEqual(imported.extensions, created.character.extensions)
        self.assertEqual(imported_profile.player_name, "阿凛")
        self.assertTrue(target_creation.character_manager.exists("米菈"))
        self.assertEqual(target_creation.world_state.party_sheet.members[0].hero_name, "米菈")

    def test_starting_card_recomputes_derived_values(self) -> None:
        creation, cards = self.manager()
        source = creation.create_player_character(profile(), fate_roll=(3, 4)).character
        exported = cards.export_character(source)
        exported["derived"]["max_hp"] = 9999
        exported["character_snapshot"]["max_hp"] = 9999

        target_creation, target_cards = self.manager()
        imported, _profile, _warnings = target_cards.import_card(exported)

        self.assertEqual(imported.max_hp, 50)
        self.assertEqual(imported.hp, 50)
        self.assertTrue(target_creation.character_manager.exists("米菈"))

    def test_preview_does_not_register_or_require_fate_roll(self) -> None:
        creation, cards = self.manager()

        preview = cards.preview_build({"build": CharacterCardManager.build_from_profile(profile())})

        self.assertTrue(preview["valid"])
        self.assertTrue(preview["fate_pending"])
        self.assertIsNone(preview["derived"]["starting_zenit"])
        self.assertFalse(creation.character_manager.exists("米菈"))

    def test_duplicate_requires_explicit_conflict_mode(self) -> None:
        creation, cards = self.manager()
        source = creation.create_player_character(profile(), fate_roll=(3, 4)).character
        source.card_id = "same-card"
        exported = cards.export_character(source)

        with self.assertRaises(CharacterCardError):
            cards.import_card(exported)

        copied, _profile, warnings = cards.import_card(exported, conflict="copy")
        self.assertNotEqual(copied.name, "米菈")
        self.assertTrue(warnings)

    def test_rejects_unknown_schema(self) -> None:
        _creation, cards = self.manager()

        result = cards.validate_card({"$schema": "someone-else.card", "schema_version": 1})

        self.assertFalse(result["valid"])
        self.assertIn("不支持", result["errors"][0])

    def test_legacy_fu_gm_schema_is_normalized_without_losing_extensions(self) -> None:
        creation, cards = self.manager()
        source = creation.create_player_character(profile(), fate_roll=(3, 4)).character
        source.extensions = {"cunfu.homebrew": {"features": ["巨岩扩展内容"]}}
        exported = cards.export_character(source)
        exported["$schema"] = "fu-gm.character-card"
        exported["ruleset"]["catalog_revision"] = "fu-core-scn-1.03-r1"

        result = cards.validate_card(exported)

        self.assertTrue(result["valid"])
        self.assertEqual(CHARACTER_CARD_SCHEMA, result["card"]["$schema"])
        self.assertEqual(source.extensions, result["card"]["extensions"])
        self.assertTrue(any("旧版 FU-GM" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
