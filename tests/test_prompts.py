import unittest

from fu_gm.prompts import (
    CORE_GM_CONTRACT,
    EXPRESSOR_SYSTEM_PROMPT,
    FABULA_ULTIMA_CORE_SYSTEM_PROMPT,
    SESSION_ZERO_SYSTEM_PROMPT,
)


class PromptTests(unittest.TestCase):
    def test_runtime_prompts_use_short_core_contract_not_full_rulebook_prompt(self) -> None:
        for prompt in (
            EXPRESSOR_SYSTEM_PROMPT,
            SESSION_ZERO_SYSTEM_PROMPT,
        ):
            self.assertTrue(prompt.startswith(CORE_GM_CONTRACT))
            self.assertFalse(prompt.startswith(FABULA_ULTIMA_CORE_SYSTEM_PROMPT))

    def test_expressor_prompt_no_longer_carries_full_rulebook_intro(self) -> None:
        self.assertNotIn("要玩这款游戏，你需要以下道具", EXPRESSOR_SYSTEM_PROMPT)
        self.assertNotIn("如果你是一名玩家，以下是你应该如何进入游戏", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("规则面板是权威", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("不要复述玩家刚才声明的动作", EXPRESSOR_SYSTEM_PROMPT)

    def test_session_zero_prompt_keeps_creation_contract(self) -> None:
        self.assertIn("共同创作者", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("界限与帷幕", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("贡献较少", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("kingdom_contributors", SESSION_ZERO_SYSTEM_PROMPT)
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
