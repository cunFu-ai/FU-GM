import unittest

from fu_gm.prompt_cache import (
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    build_cache_friendly_messages,
    system_reminder,
    with_static_boundary,
)


class PromptCacheTests(unittest.TestCase):
    def test_static_boundary_is_stable_and_idempotent(self) -> None:
        prompt = "稳定规则"
        first = with_static_boundary(prompt)
        second = with_static_boundary(first)
        self.assertEqual(first, second)
        self.assertIn(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, first)

    def test_dynamic_reminders_go_to_user_message_not_system_prompt(self) -> None:
        messages = build_cache_friendly_messages(
            static_system_prompt="静态规则",
            reminders=[("当前 NPC 人设档案", "帝国机甲想碾碎反抗。")],
            user_content="输出 NPCAct JSON。",
        )
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")
        self.assertIn("静态规则", messages[0].content)
        self.assertNotIn("帝国机甲", messages[0].content)
        self.assertTrue(messages[0].cache_breakpoint)
        self.assertEqual(messages[0].cache_family, "system")
        self.assertIn("<system-reminder", messages[1].content)
        self.assertIn("帝国机甲想碾碎反抗。", messages[1].content)
        self.assertTrue(messages[1].content.endswith("输出 NPCAct JSON。"))

    def test_cache_family_can_separate_prompt_call_paths(self) -> None:
        messages = build_cache_friendly_messages(
            static_system_prompt="稳定规则",
            user_content="当前消息",
            cache_family="gm-post-tool",
        )

        self.assertEqual(messages[0].cache_family, "gm-post-tool")
        self.assertFalse(messages[1].cache_breakpoint)

    def test_layered_breakpoints_are_normalized_without_changing_message_shape(self) -> None:
        messages = build_cache_friendly_messages(
            static_system_prompt="核心规则\n阶段规则",
            user_content="本轮动态状态",
            reminders=[("当前状态", "场景仍在继续")],
            cache_family="gm-init-scene",
            cache_breakpoint_offsets=(4, 9, 9, 9999),
            user_cache_breakpoint_offsets=(2,),
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")
        self.assertEqual(
            messages[0].cache_breakpoint_offsets,
            (4, 9, len(messages[0].content)),
        )
        self.assertTrue(messages[1].cache_breakpoint)
        self.assertGreater(messages[1].cache_breakpoint_offsets[0], 2)

    def test_system_reminder_handles_empty_content(self) -> None:
        reminder = system_reminder("当前记忆", "")
        self.assertIn('title="当前记忆"', reminder)
        self.assertIn("无。", reminder)


if __name__ == "__main__":
    unittest.main()
