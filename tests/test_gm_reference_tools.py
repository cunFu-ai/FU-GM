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
            {"kind": "skill", "class_name": "旅人", "limit": 10},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["count"], 5)
        self.assertTrue(all(row["class_name"] == "旅人" for row in receipt.result["references"]))
        self.assertNotIn("terminal_public_result", receipt.result)
        self.assertFalse(receipt.lock_public_reply)
        companion = next(
            row for row in receipt.result["references"] if row["name"] == "忠诚伙伴"
        )
        self.assertEqual(companion["rank_notation"]["maximum_acquisitions"], 5)
        self.assertIn("不是检定或数值修正", companion["rank_notation"]["meaning"])


if __name__ == "__main__":
    unittest.main()
