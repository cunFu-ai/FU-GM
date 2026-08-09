import unittest

from fu_gm.check_difficulty import OPEN_CHECK_DIFFICULTY_GUIDANCE
from fu_gm.components.gm_agent_prompts import build_initial_gm_system_prompt
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

    def test_adventure_agent_prompt_keeps_compact_open_check_difficulty_rubric(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="adventure")

        self.assertIn(OPEN_CHECK_DIFFICULTY_GUIDANCE, prompt)
        self.assertIn("难度等级7为简单", prompt)
        self.assertIn("难度等级10为正常", prompt)
        self.assertIn("难度等级13为困难", prompt)
        self.assertIn("难度等级16为非常困难", prompt)
        self.assertIn("结果并不真正存在不确定性", prompt)
        self.assertIn("失败不会带来有意义的后果", prompt)
        self.assertIn("不要因为上一项检定用了某个难度等级就继续沿用", prompt)

    def test_agent_prompt_uses_general_constraints_not_historical_chat_examples(self) -> None:
        prompts = (
            build_initial_gm_system_prompt(gate_status="session_zero"),
            build_initial_gm_system_prompt(gate_status="adventure"),
        )
        historical_examples = (
            "我们要不要问会长",
            "登记由谁负责比较合适",
            "他很坏啊都不理你",
            "沿已走通路线走在队尾并观察追兵",
            "把A放到B西边",
            "我们一起走",
        )

        for prompt in prompts:
            for example in historical_examples:
                self.assertNotIn(example, prompt)


if __name__ == "__main__":
    unittest.main()
