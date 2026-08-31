import unittest

from fu_gm.gm_reference_tools import GMReferenceToolService
from fu_gm.gm_tool_agent import GMToolExecutionContext, GMToolRegistry


def context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="rules",
        session_id="s1",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
    )


def committed_action_context() -> GMToolExecutionContext:
    result = context()
    result.metadata["_gm_message_semantics"] = {
        "version": "1",
        "events": [
            {
                "event_id": "evt-skill-action",
                "speaker": "阿凛",
                "relation": "gm",
                "dialogue_act": "action_declaration",
                "action_commitment": "committed",
                "responds_to_event_id": "",
                "reason": "玩家正在使用技能执行行动。",
            }
        ],
    }
    return result


class GMReferenceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = GMReferenceToolService()

    def test_registry_exposes_read_only_reference_tools(self) -> None:
        registry = GMToolRegistry()
        self.service.register_tools(registry)

        schemas = {item["name"]: item for item in registry.schemas()}

        self.assertEqual(set(schemas), {"get_rule_reference", "search_rule_references"})
        self.assertEqual(schemas["get_rule_reference"]["side_effect"], "read")

    def test_exact_skill_lookup_uses_current_canonical_translation(self) -> None:
        receipt = self.service.get_rule_reference(
            context(),
            {"kind": "skill", "name": "魔法炮击"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["name"], "魔法炮击")
        self.assertEqual(receipt.result["class_name"], "元素使")
        self.assertIn("施法检定", receipt.result["summary"])
        self.assertEqual(receipt.result["rank_notation"]["maximum_acquisitions"], 3)
        self.assertIn("不表示角色当前已经达到该等级", receipt.result["rank_notation"]["meaning"])
        self.assertIs(receipt.result["terminal_public_result"], True)
        self.assertTrue(receipt.lock_public_reply)

    def test_exact_class_lookup_returns_role_benefits_and_skill_names(self) -> None:
        receipt = self.service.get_rule_reference(
            context(),
            {"kind": "class", "name": "守护者"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["name"], "守护者")
        self.assertEqual(receipt.result["free_benefits"]["hp"], 5)
        self.assertIn(
            "可装备职业盾牌",
            receipt.result["free_benefits"]["abilities"],
        )
        self.assertIn("挺身守护", receipt.result["skill_names"])

    def test_spell_lookup_returns_rule_fields_with_chinese_attributes(self) -> None:
        receipt = self.service.get_rule_reference(
            context(),
            {"kind": "spell", "name": "炎弹"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["name"], "炎弹")
        self.assertEqual(receipt.result["school"], "元素使法术")
        self.assertEqual(receipt.result["attributes"], ["洞察", "意志"])
        self.assertNotIn("INS", receipt.public_fallback_reply)

    def test_skill_lookup_exposes_data_driven_followup_choice(self) -> None:
        receipt = self.service.get_rule_reference(
            context(),
            {"kind": "skill", "name": "元素魔法"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        requirement = receipt.result["choice_requirements"][0]
        self.assertEqual(requirement["choice_key"], "granted_spell")
        self.assertEqual(requirement["storage_field"], "spells")
        self.assertEqual(requirement["count_mode"], "per_rank")
        self.assertTrue(requirement["required_for_creation"])
        self.assertIn("炎弹", requirement["allowed_values"])
        self.assertIn("元素武器", requirement["allowed_values"])

    def test_lookup_supporting_committed_action_does_not_end_agent_turn(self) -> None:
        receipt = self.service.get_rule_reference(
            committed_action_context(),
            {"kind": "skill", "name": "保镖"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertNotIn("terminal_public_result", receipt.result)
        self.assertFalse(receipt.lock_public_reply)

    def test_equipment_alias_lookup_returns_authoritative_item(self) -> None:
        receipt = self.service.get_rule_reference(
            context(),
            {"kind": "equipment", "name": "临时武器(近战)"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["name"], "临时武器（近战）")
        self.assertEqual(receipt.result["accuracy_attributes"], ["敏捷", "力量"])

    def test_search_by_class_does_not_dump_unrelated_skills(self) -> None:
        receipt = self.service.search_rule_references(
            context(),
            {
                "kind": "skill",
                "class_name": "旅人",
                "skill_kind": "class",
                "view": "shortlist",
                "limit": 10,
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["count"], 5)
        self.assertTrue(all(row["class_name"] == "旅人" for row in receipt.result["references"]))
        self.assertNotIn("terminal_public_result", receipt.result)
        self.assertFalse(receipt.lock_public_reply)
        companion = next(
            row for row in receipt.result["references"] if row["name"] == "忠诚伙伴"
        )
        self.assertEqual(companion["max_ranks"], 5)
        self.assertEqual(companion["choice_labels"], ["伙伴资料"])
        self.assertNotIn("rank_notation", companion)
        self.assertNotIn("choice_requirements", companion)
        self.assertNotIn("hero_draft_patch", companion)

    def test_class_shortlist_uses_role_tags_without_loading_all_skills(self) -> None:
        receipt = self.service.search_rule_references(
            context(),
            {
                "kind": "class",
                "query_tags": ["防护"],
                "view": "shortlist",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        names = [row["name"] for row in receipt.result["references"]]
        self.assertIn("守护者", names)
        self.assertLessEqual(receipt.result["count"], 3)
        self.assertTrue(
            all("skill_names" not in row for row in receipt.result["references"])
        )

    def test_spell_overview_is_compact_and_reports_more_results(self) -> None:
        receipt = self.service.search_rule_references(
            context(),
            {
                "kind": "spell",
                "school": "元素使法术",
                "view": "overview",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["count"], 3)
        self.assertEqual(receipt.result["total_count"], 13)
        self.assertTrue(receipt.result["has_more"])
        self.assertEqual(receipt.result["next_cursor"], 3)
        self.assertTrue(receipt.result["categories"])
        self.assertNotIn("duration", receipt.result["references"][0])

    def test_names_view_supports_cursor_without_repeating_previous_page(self) -> None:
        first = self.service.search_rule_references(
            context(),
            {"kind": "class", "view": "names", "limit": 5},
        )
        second = self.service.search_rule_references(
            context(),
            {
                "kind": "class",
                "view": "names",
                "limit": 5,
                "cursor": first.result["next_cursor"],
            },
        )

        first_names = {row["name"] for row in first.result["references"]}
        second_names = {row["name"] for row in second.result["references"]}
        self.assertFalse(first_names & second_names)
        self.assertEqual(first.result["total_count"], 15)
        self.assertEqual(second.result["cursor"], 5)

    def test_spell_tag_search_does_not_treat_ignore_resistance_as_defense(self) -> None:
        receipt = self.service.search_rule_references(
            context(),
            {
                "kind": "spell",
                "school": "元素使法术",
                "query_tags": ["防护"],
                "view": "shortlist",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        names = [row["name"] for row in receipt.result["references"]]
        self.assertEqual(names, ["元素幕障", "气旋"])
        self.assertNotIn("焰流", names)


if __name__ == "__main__":
    unittest.main()
