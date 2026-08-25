import unittest

from fu_gm.check_difficulty import OPEN_CHECK_DIFFICULTY_GUIDANCE
from fu_gm.components.gm_agent_prompts import (
    HEARTBEAT_SYSTEM_PROMPT,
    SESSION_ZERO_SYSTEM_PROMPT as AGENT_SESSION_ZERO_SYSTEM_PROMPT,
    build_initial_gm_system_prompt,
)
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
        self.assertIn("【规则面板】逐字保留", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("自由补充仅呈现结构化回执明确支持的现场回应", EXPRESSOR_SYSTEM_PROMPT)

    def test_expressor_prompt_uses_one_positive_output_contract(self) -> None:
        self.assertEqual(EXPRESSOR_SYSTEM_PROMPT.count("【规则面板】逐字保留"), 1)
        self.assertEqual(
            EXPRESSOR_SYSTEM_PROMPT.count(
                "自由补充仅呈现结构化回执明确支持的现场回应"
            ),
            1,
        )
        self.assertEqual(EXPRESSOR_SYSTEM_PROMPT.count("无可补内容时输出零字符"), 1)
        self.assertNotIn("空字符串", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("不替玩家行动", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("GM 私密暗线", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("界限与帷幕优先于剧情张力", EXPRESSOR_SYSTEM_PROMPT)
        self.assertIn("硬规则由 Python 落地", EXPRESSOR_SYSTEM_PROMPT)

    def test_session_zero_prompt_keeps_creation_contract(self) -> None:
        self.assertIn("共同创作者", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("界限与帷幕", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("贡献较少", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("kingdom_contributors", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("map_locations", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("archipelago", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn(
            "不能只保存玩家给出的部分后静默",
            AGENT_SESSION_ZERO_SYSTEM_PROMPT,
        )
        self.assertIn("保存为待确认提案", AGENT_SESSION_ZERO_SYSTEM_PROMPT)

    def test_session_zero_incremental_choices_do_not_trigger_missing_field_chatter(self) -> None:
        runtime_prompt = build_initial_gm_system_prompt(gate_status="session_zero")

        self.assertIn("普通增量选择只需简短确认，不追问或提醒下一项", SESSION_ZERO_SYSTEM_PROMPT)
        self.assertIn("不主动罗列、追问或暗示下一项缺口", runtime_prompt)
        self.assertNotIn("只需要简短确认并追问下一个缺失项", SESSION_ZERO_SYSTEM_PROMPT)

    def test_confirmed_contribution_acknowledgement_does_not_paraphrase_player(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="session_zero")

        self.assertIn("状态写入属于后台工作", prompt)
        self.assertIn("不要先换词概括、评价这项贡献", prompt)
        self.assertIn("具体人物关系、风险或选择", prompt)

    def test_session_zero_heartbeat_varies_repeated_contribution_questions(self) -> None:
        self.assertIn("优先采用prompt_hint", HEARTBEAT_SYSTEM_PROMPT)
        self.assertIn("不能只替换名字复用同一句式", HEARTBEAT_SYSTEM_PROMPT)

    def test_core_prompt_contains_fabula_ultima_key_sections(self) -> None:
        self.assertIn("这是一款关于传奇英雄和悲剧对手的游戏", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("八大支柱", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("冲突场景", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("物语点", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)
        self.assertIn("终结点", FABULA_ULTIMA_CORE_SYSTEM_PROMPT)

    def test_runtime_agent_knows_canonical_classes_and_disambiguates_rule_queries(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="inactive")

        self.assertIn("奥灵使、拟兽使", prompt)
        self.assertIn("造物使、旅人、武器大师", prompt)
        self.assertIn("询问该职业的技能、可选项或规则效果时，优先按职业理解", prompt)
        self.assertIn("规则目录能够唯一回答时不得追问人物姓名", prompt)
        self.assertIn("class_name=该职业", prompt)
        self.assertIn("（+N）表示该技能最多可以取得N次", prompt)
        self.assertIn("不表示当前技能等级为N", prompt)

    def test_blank_campaign_prompt_separates_world_creation_from_map_editing(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="inactive")

        self.assertIn("空白战役与第零章", prompt)
        self.assertIn("先调用start_session进入session_zero", prompt)
        self.assertIn("每项独立世界事实交给create_world_setting", prompt)
        self.assertIn("不得改写成编辑或绘制地图成品", prompt)

    def test_adventure_agent_prompt_keeps_compact_open_check_difficulty_rubric(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="adventure")

        self.assertIn(OPEN_CHECK_DIFFICULTY_GUIDANCE, prompt)
        self.assertIn("难度等级7为简单", prompt)
        self.assertIn("难度等级10为正常", prompt)
        self.assertIn("难度等级13为困难", prompt)
        self.assertIn("难度等级16为非常困难", prompt)
        self.assertIn("结果并不真正存在不确定性", prompt)
        self.assertIn("失败不会带来有意义的后果", prompt)
        self.assertIn("独立于角色属性骰与上一项检定的难度", prompt)

    def test_adventure_movement_contract_is_scene_agnostic_and_atomic(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="adventure")

        self.assertIn("一次检定只结算玩家当前手段直接触及的一项障碍", prompt)
        self.assertIn("与当前位置直接相连的下一处落点", prompt)
        self.assertIn("宏观终点继续作为后续目标", prompt)
        self.assertIn("本事务刚触发且已精确登记后果", prompt)
        self.assertIn("命刻、到期承诺、当前NPC行动或结构化场景危害", prompt)

    def test_conflict_prompt_routes_out_of_turn_actions_to_inbox(self) -> None:
        prompt = build_initial_gm_system_prompt(
            gate_status="adventure",
            conflict_active=True,
        )

        self.assertIn("timing=defer", prompt)
        self.assertIn("异步行动收件箱", prompt)
        self.assertIn("不表示动作已经执行", prompt)
        self.assertIn("declare_check_action与declare_movement_check", prompt)
        self.assertIn("成功回执要求run_current_npc_turn", prompt)

    def test_adventure_agent_batches_first_present_npc_profile_and_reply(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="adventure")

        self.assertIn("优先一次call_tools", prompt)
        self.assertIn("create_npc_profile与decide_npc_response", prompt)
        self.assertIn("不先discover_capabilities", prompt)

    def test_adventure_agent_classifies_npc_improvisation_without_forcing_truth(self) -> None:
        prompt = build_initial_gm_system_prompt(gate_status="adventure")

        self.assertIn("不能自动证明NPC过去见过、认识或听说过", prompt)
        self.assertIn("用fact_effects", prompt)
        self.assertIn("即兴建立objective事实", prompt)
        self.assertIn("claim、rumor或lie", prompt)
        self.assertIn("锁定暗线或战役级真相", prompt)

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
