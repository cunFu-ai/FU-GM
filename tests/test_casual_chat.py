import tempfile
import unittest

from fu_gm.casual_chat import CasualChatResponder
from fu_gm.components.session_log_manager import SessionLogManager
from fu_gm.components.world_state import WorldState


class CasualChatResponderTests(unittest.TestCase):
    def test_class_options_question_uses_fixed_class_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            responder = CasualChatResponder(log_manager=SessionLogManager(tmpdir))
            response = responder.respond(
                campaign_id="default",
                speaker="阿凛",
                message="有什么职业可以选择？",
                world_state=WorldState(),
            )

        self.assertIn("奥灵使", response.reply)
        self.assertIn("武器大师", response.reply)
        self.assertIn("起始角色通常为 5 级", response.reply)

    def test_spell_options_question_uses_standard_spellbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            responder = CasualChatResponder(log_manager=SessionLogManager(tmpdir))
            response = responder.respond(
                campaign_id="default",
                speaker="阿凛",
                message="元素使法术有哪些？",
                world_state=WorldState(),
            )

        self.assertIn("元素幕障", response.reply)
        self.assertIn("元素武器", response.reply)
        self.assertIn("巨岩", response.reply)
        self.assertNotIn("火焰箭", response.reply)
        self.assertNotIn("冰霜之触", response.reply)
        self.assertNotIn("土石铠甲", response.reply)
        self.assertNotIn("本地法术表", response.reply)
        self.assertNotIn("不会临场", response.reply)
        self.assertNotIn("不会编", response.reply)

    def test_spell_detail_question_uses_spellbook_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            responder = CasualChatResponder(log_manager=SessionLogManager(tmpdir))
            response = responder.respond(
                campaign_id="default",
                speaker="阿凛",
                message="巨岩的效果是什么？",
                world_state=WorldState(),
            )

        self.assertIn("巨岩", response.reply)
        self.assertIn("精神值消耗 20", response.reply)
        self.assertIn("无视抵抗相性", response.reply)

    def test_general_chat_without_model_does_not_use_keyword_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            responder = CasualChatResponder(log_manager=SessionLogManager(tmpdir))
            response = responder.respond(
                campaign_id="default",
                speaker="阿凛",
                message="悠老师，最近怎么样？",
                world_state=WorldState(),
            )

        self.assertEqual(response.reply, "")


if __name__ == "__main__":
    unittest.main()
