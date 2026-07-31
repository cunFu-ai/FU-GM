from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fu_gm.expressor import LLMExpressor
from fu_gm.gm_persona import (
    GMPersonaProfile,
    load_gm_persona_text,
    persona_mode_for_context,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry


PERSONA = """
# GM 人格档案：测试

## 核心人格

核心声音：自然地坐在桌边。

## 模式：群聊

群聊时先判断是否需要开口。

## 示例：群聊

玩家问规则时，给一句直接回答。

## 模式：第零章

第零章接住尚在讨论的点子。

## 示例：第零章

玩家提出主题时，好奇它如何影响行动。

## 模式：场景

场景里让世界直接回应。

## 示例：场景

调查成功时给出一条具体发现。

## 模式：冲突

冲突中保持裁定清楚。

## 示例：冲突

失败时让阻碍出现在现场。

## 模式：主动节拍

主动节拍必须带来一个真实变化。

## 模式：工具收尾

工具收尾只说玩家需要知道的结果。
""".strip()


class _UnusedClient:
    config = type("_Config", (), {"timeout_seconds": 30.0})()

    def create_chat_completion(self, **_kwargs: object) -> str:
        raise AssertionError("本测试不应调用模型。")


class GMPersonaTests(unittest.TestCase):
    def test_profile_selects_only_current_mode_and_example(self) -> None:
        profile = GMPersonaProfile.from_markdown(PERSONA)

        prompt = profile.prompt_block("session_zero")

        self.assertIn("核心声音", prompt)
        self.assertIn("第零章接住", prompt)
        self.assertIn("好奇它如何影响行动", prompt)
        self.assertNotIn("场景里让世界直接回应", prompt)
        self.assertNotIn("调查成功时给出一条具体发现", prompt)

    def test_plain_text_style_remains_a_complete_core_persona(self) -> None:
        profile = GMPersonaProfile.from_markdown("语气温和，回答具体。")

        self.assertIn("语气温和", profile.prompt_block("scene"))

    def test_file_loader_resolves_relative_style_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "persona.md"
            path.write_text("桌边声音。", encoding="utf-8")

            text, source = load_gm_persona_text(
                environ={"FU_GM_STYLE_FILE": "persona.md"},
                base_dir=tempdir,
            )

        self.assertEqual(text, "桌边声音。")
        self.assertEqual(source, str(path.resolve()))

    def test_context_mode_uses_phase_and_authoritative_conflict_state(self) -> None:
        mode, overlays = persona_mode_for_context(
            gate_status="session_zero",
            metadata={},
            state_summary={},
        )
        self.assertEqual((mode, overlays), ("session_zero", ()))

        mode, overlays = persona_mode_for_context(
            gate_status="adventure",
            metadata={"system_gm_beat_request": True},
            state_summary={"runtime": {"conflict": {"active": True}}},
        )
        self.assertEqual(mode, "conflict")
        self.assertEqual(overlays, ("heartbeat",))

    def test_core_agent_injects_current_mode_without_other_mode_examples(self) -> None:
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=GMToolRegistry(),
            gm_personality_prompt=PERSONA,
        )
        context = GMToolExecutionContext(
            campaign_id="persona",
            session_id="s1",
            channel_id="group",
            speaker="阿凛",
            gate_status="session_zero",
            directly_addressed=True,
        )

        prompt = agent._system_prompt(context, observed_state={})

        self.assertIn("核心声音", prompt)
        self.assertIn("第零章接住", prompt)
        self.assertIn("好奇它如何影响行动", prompt)
        self.assertNotIn("场景里让世界直接回应", prompt)
        self.assertIn("只输出一个JSON对象", prompt)

    def test_post_tool_prompt_adds_compact_closing_mode_without_examples(self) -> None:
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=GMToolRegistry(),
            gm_personality_prompt=PERSONA,
        )
        context = GMToolExecutionContext(
            campaign_id="persona",
            session_id="s1",
            channel_id="group",
            speaker="阿凛",
            gate_status="session_zero",
            directly_addressed=True,
        )

        prompt = agent._system_prompt(
            context,
            observed_state={},
            has_receipts=True,
        )

        self.assertIn("工具收尾只说", prompt)
        self.assertNotIn("好奇它如何影响行动", prompt)

    def test_expressor_receives_only_requested_mode(self) -> None:
        expressor = LLMExpressor(
            client=_UnusedClient(),
            model="fake",
            allow_fallback=False,
            gm_personality_prompt=PERSONA,
        )

        prompt = expressor._expression_system_prompt("conflict")

        self.assertIn("冲突中保持裁定清楚", prompt)
        self.assertNotIn("场景里让世界直接回应", prompt)
        self.assertNotIn("失败时让阻碍出现在现场", prompt)


if __name__ == "__main__":
    unittest.main()
