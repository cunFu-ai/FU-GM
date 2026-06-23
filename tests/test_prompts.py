import unittest

from fu_gm.prompts import (
    ACTION_BRAIN_SYSTEM_PROMPT,
    EXPRESSOR_SYSTEM_PROMPT,
    FABULA_ULTIMA_CORE_SYSTEM_PROMPT,
    SESSION_ZERO_SYSTEM_PROMPT,
)


class PromptTests(unittest.TestCase):
    def test_action_prompt_starts_with_core_prompt(self) -> None:
        self.assertTrue(ACTION_BRAIN_SYSTEM_PROMPT.startswith(FABULA_ULTIMA_CORE_SYSTEM_PROMPT))

    def test_expressor_prompt_starts_with_core_prompt(self) -> None:
        self.assertTrue(EXPRESSOR_SYSTEM_PROMPT.startswith(FABULA_ULTIMA_CORE_SYSTEM_PROMPT))

    def test_session_zero_prompt_starts_with_core_prompt(self) -> None:
        self.assertTrue(SESSION_ZERO_SYSTEM_PROMPT.startswith(FABULA_ULTIMA_CORE_SYSTEM_PROMPT))
        self.assertIn("共同创作者", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("界限与帷幕", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("current_participant", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("map_locations", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("archipelago", SESSION_ZERO_SYSTEM_PROMPT)

    def test_core_prompt_contains_fabula_ultima_key_sections(self) -> None:
        self.assertIn("这是一款关于传奇英雄和悲剧对手的游戏", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("八大支柱", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("冲突场景", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("物语点", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("终结点", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
