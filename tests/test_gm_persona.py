from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fu_gm.config import LLMConfig
from fu_gm.components.gm_agent_prompts import (
    CORE_AGENT_SYSTEM_PREFIX,
    CORE_PUBLIC_EXPRESSION_CONTRACT,
    POST_TOOL_SYSTEM_PROMPT,
    build_initial_gm_system_prompt,
)
from fu_gm.expressor import LLMExpressor
from fu_gm.gm_persona import (
    DEFAULT_GM_PERSONA,
    GMPersonaProfile,
    load_gm_persona_text,
    persona_mode_for_context,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry
from fu_gm.llm_client import OpenAICompatibleClient


PERSONA = """
# GM 人格档案：测试

## 核心人格

核心声音：自然地在群聊里说话。

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

## 示例：大成功

大成功时可以短短惊叹一句。

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
    def test_builtin_persona_knows_the_game_runs_in_an_online_group_chat(self) -> None:
        self.assertIn("同一个线上群聊", DEFAULT_GM_PERSONA)
        self.assertIn("群聊界面已经展示发言者身份", DEFAULT_GM_PERSONA)
        self.assertIn("后台登记本身不构成新的谈话内容", DEFAULT_GM_PERSONA)
        self.assertIn("通常直接承接下一项讨论", DEFAULT_GM_PERSONA)
        self.assertNotIn("敲桌", DEFAULT_GM_PERSONA)
        self.assertNotIn("像坐在同一张桌边", DEFAULT_GM_PERSONA)

    def test_profile_always_injects_the_complete_document(self) -> None:
        profile = GMPersonaProfile.from_markdown(PERSONA)

        prompt = profile.prompt_block("session_zero")

        self.assertEqual(profile.document, PERSONA)
        self.assertEqual(prompt, PERSONA)
        self.assertTrue(prompt.startswith("# GM 人格档案：测试"))
        self.assertIn("核心声音", prompt)
        self.assertIn("第零章接住", prompt)
        self.assertIn("好奇它如何影响行动", prompt)
        self.assertIn("群聊时先判断", prompt)
        self.assertIn("场景里让世界直接回应", prompt)
        self.assertIn("调查成功时给出一条具体发现", prompt)
        self.assertIn("冲突中保持裁定清楚", prompt)
        self.assertIn("工具收尾只说", prompt)

    def test_modes_overlays_and_example_flags_do_not_change_persona_bytes(self) -> None:
        profile = GMPersonaProfile.from_markdown(PERSONA)

        variants = (
            profile.prompt_block("table_chat"),
            profile.prompt_block("session_zero"),
            profile.prompt_block("scene", overlays=("heartbeat",)),
            profile.prompt_block("conflict", include_examples=False),
            profile.prompt_block("post_tool"),
        )

        self.assertTrue(all(prompt == PERSONA for prompt in variants))
        self.assertIn("大成功时可以短短惊叹一句", variants[-1])

    def test_builtin_and_external_personas_expose_the_same_modes(self) -> None:
        builtin = GMPersonaProfile.from_markdown(DEFAULT_GM_PERSONA)
        style_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "gm_styles"
            / "acg_highschool_gm.md"
        )
        external = GMPersonaProfile.from_markdown(
            style_path.read_text(encoding="utf-8")
        )

        self.assertEqual(set(builtin.modes), set(external.modes))
        self.assertEqual(
            set(builtin.modes),
            {
                "table_chat",
                "session_zero",
                "scene",
                "conflict",
                "heartbeat",
                "post_tool",
            },
        )
        self.assertEqual(builtin.prompt_block("scene"), builtin.document)
        self.assertEqual(external.prompt_block("scene"), external.document)
        self.assertIn("第零章", builtin.prompt_block("scene"))
        self.assertIn("第零章", external.prompt_block("scene"))

    def test_plain_text_style_remains_a_complete_core_persona(self) -> None:
        profile = GMPersonaProfile.from_markdown("语气温和，回答具体。")

        self.assertIn("语气温和", profile.prompt_block("scene"))

    def test_document_only_profile_keeps_constructor_compatibility(self) -> None:
        profile = GMPersonaProfile(document=PERSONA)

        prompt = profile.prompt_block("table_chat")

        self.assertEqual(prompt, PERSONA)
        self.assertIn("第零章接住", prompt)

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

    def test_core_contract_prefix_stays_identical_across_session_modes(self) -> None:
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=GMToolRegistry(),
            gm_personality_prompt=PERSONA,
        )
        cases = (
            ("session_zero", {}),
            ("adventure", {}),
            ("adventure", {"runtime": {"conflict": {"active": True}}}),
        )
        system_messages = []
        for gate_status, observed_state in cases:
            context = GMToolExecutionContext(
                campaign_id="persona-cache",
                session_id="s1",
                channel_id="group",
                speaker="阿凛",
                gate_status=gate_status,
                directly_addressed=True,
            )
            system_messages.append(
                agent._build_decision_messages(
                    current_message="继续。",
                    recent_context="",
                    context=context,
                    observed_state=observed_state,
                    receipts=[],
                    history=[],
                )[0]
            )

        for message in system_messages:
            core_prefix_end = message.cache_breakpoint_offsets[0]
            self.assertEqual(message.cache_family, "gm-agent")
            self.assertEqual(core_prefix_end, len(CORE_AGENT_SYSTEM_PREFIX))
            self.assertEqual(
                message.content[:core_prefix_end],
                CORE_AGENT_SYSTEM_PREFIX,
            )
            self.assertEqual(message.content.count(PERSONA), 1)
            self.assertLess(
                message.content.index(PERSONA),
                message.content.index(CORE_PUBLIC_EXPRESSION_CONTRACT),
            )

        self.assertEqual(
            len(
                {
                    message.content[: len(CORE_AGENT_SYSTEM_PREFIX)]
                    for message in system_messages
                }
            ),
            1,
        )

        cache_client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.invalid/v1",
                api_key="test-only",
                action_model="fake",
                expressor_model="fake",
                prompt_cache_mode="key",
            )
        )
        metadata = [
            cache_client._prompt_cache_request_metadata(
                endpoint_url="https://example.invalid/v1/chat/completions",
                model="fake",
                messages=agent._build_decision_messages(
                    current_message="继续。",
                    recent_context="",
                    context=GMToolExecutionContext(
                        campaign_id="persona-cache",
                        session_id="s1",
                        channel_id="group",
                        speaker="阿凛",
                        gate_status=gate_status,
                        directly_addressed=True,
                    ),
                    observed_state=observed_state,
                    receipts=[],
                    history=[],
                ),
                operation="gm_tool_agent",
            )
            for gate_status, observed_state in cases
        ]
        self.assertEqual(len({item["key"] for item in metadata}), 1)
        self.assertEqual(len({item["base_fingerprint"] for item in metadata}), 1)
        self.assertGreater(
            len({item["prefix_fingerprint"] for item in metadata}),
            1,
        )

    def test_core_agent_receives_complete_expression_persona(self) -> None:
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

        self.assertEqual(
            prompt,
            "\n\n".join(
                (
                    CORE_AGENT_SYSTEM_PREFIX,
                    PERSONA,
                    CORE_PUBLIC_EXPRESSION_CONTRACT,
                    build_initial_gm_system_prompt(
                        gate_status="session_zero",
                        conflict_active=False,
                    ),
                )
            ),
        )
        self.assertNotIn("DeepSeek Expressor负责最终公开措辞", prompt)
        self.assertNotIn("不模仿时悠人格", prompt)
        self.assertNotIn("不追求文风", prompt)
        self.assertIn("当前阶段：开团前与第零章", prompt)
        self.assertIn("只输出一个JSON对象", prompt)

        different_persona_agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=GMToolRegistry(),
            gm_personality_prompt="完全不同的公开人格。",
        )
        self.assertNotEqual(
            prompt,
            different_persona_agent._system_prompt(context, observed_state={}),
        )
        self.assertIn(
            "完全不同的公开人格。",
            different_persona_agent._system_prompt(context, observed_state={}),
        )

    def test_table_chat_heartbeat_uses_persona_without_scene_state_contract(self) -> None:
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
            speaker="系统主动节拍",
            gate_status="adventure",
            metadata={
                "system_gm_beat_request": True,
                "heartbeat_action": "adventure_table_nudge",
                "heartbeat_persona_chat_only": True,
            },
        )

        prompt = agent._system_prompt(context, observed_state={"scene": {"name": "牢区"}})

        self.assertTrue(prompt.startswith(PERSONA))
        self.assertIn("这是第一章开始后的现实群聊闲置判断", prompt)
        self.assertNotIn(CORE_AGENT_SYSTEM_PREFIX, prompt)
        self.assertNotIn("当前聚焦场景与权威状态", prompt)

    def test_post_tool_prompt_keeps_persona_and_transaction_contract(self) -> None:
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

        self.assertEqual(
            prompt,
            "\n\n".join(
                (
                    CORE_AGENT_SYSTEM_PREFIX,
                    PERSONA,
                    CORE_PUBLIC_EXPRESSION_CONTRACT,
                    POST_TOOL_SYSTEM_PROMPT,
                )
            ),
        )
        self.assertNotIn("DeepSeek Expressor负责最终公开措辞", prompt)
        self.assertNotIn("不模仿时悠人格", prompt)
        self.assertNotIn("不追求文风", prompt)

    def test_expressor_places_complete_persona_before_expression_protocol(self) -> None:
        expressor = LLMExpressor(
            client=_UnusedClient(),
            model="fake",
            allow_fallback=False,
            gm_personality_prompt=PERSONA,
        )

        prompt = expressor._expression_system_prompt("conflict")

        self.assertTrue(prompt.startswith(PERSONA))
        self.assertIn("冲突中保持裁定清楚", prompt)
        self.assertIn("场景里让世界直接回应", prompt)
        self.assertIn("失败时让阻碍出现在现场", prompt)
        self.assertLess(
            prompt.index("# GM 人格档案：测试"),
            prompt.index("【规则面板】逐字保留"),
        )


if __name__ == "__main__":
    unittest.main()
