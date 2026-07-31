import json
import tempfile
import unittest

from fu_gm.components.session_log_manager import LLMStorySummarizer, SessionLogManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import MemoryVisibility, SessionTranscriptEntry


class FakeTransport:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {"choices": [{"message": {"content": self.content}}]}


class FailingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        raise TimeoutError("summary provider timed out")


class SessionLogManagerTests(unittest.TestCase):
    def test_append_turn_uses_stable_ids_for_both_sides_of_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(tmpdir)

            for _ in range(2):
                manager.append_turn(
                    "稳定日志",
                    "s1",
                    speaker="阿凛",
                    message="我检查门闩。",
                    gm_reply="门闩上留着新鲜划痕。",
                    channel_id="group-1",
                    message_id="qq-42",
                )

            entries = manager.load_transcript("稳定日志", "s1")

            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].message_id, "qq-42")
            self.assertEqual(entries[1].message_id, "fu-gm-reply:qq-42")
            self.assertTrue(manager.last_append_diagnostics["deduplicated"])

    def test_finalize_session_persists_transcript_summary_and_public_memory(self) -> None:
        world = WorldState()
        world.record_memory_event("宝箱王曾在星尘迷宫观察英雄。", entities=["宝箱王", "星尘迷宫"])

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(tmpdir)
            manager.append_turn(
                "星尘宝箱谭",
                "session-01",
                speaker="阿凛",
                message="我打开星尘迷宫的宝箱。",
                gm_reply="宝箱发出月光，宝箱王的影子苏醒了。",
                channel_id="group-1",
            )
            manager.append_message(
                "星尘宝箱谭",
                "session-01",
                speaker="GM后台",
                content="宝箱王其实在试探阿凛对捷径的渴望。",
                role="gm_private",
            )

            summary = manager.finalize_session(
                "星尘宝箱谭",
                "session-01",
                world_state=world,
                title="星尘迷宫第一夜",
            )

            self.assertTrue(manager.transcript_path("星尘宝箱谭", "session-01").exists())
            self.assertTrue(manager.transcript_txt_path("星尘宝箱谭", "session-01").exists())
            self.assertTrue(manager.summary_path("星尘宝箱谭", "session-01").exists())
            self.assertTrue(manager.memory_path("星尘宝箱谭", "session-01").exists())
            readable_transcript = manager.transcript_txt_path("星尘宝箱谭", "session-01").read_text(encoding="utf-8")
            self.assertIn("阿凛", readable_transcript)
            self.assertIn("我打开星尘迷宫的宝箱。", readable_transcript)
            self.assertEqual(summary.transcript_txt_path, str(manager.transcript_txt_path("星尘宝箱谭", "session-01")))
            self.assertIn("星尘迷宫第一夜", summary.short_memory)
            self.assertIn("宝箱王", summary.public_summary)
            self.assertTrue(any(event.kind == "session_story_summary" for event in world.memory_events))
            self.assertTrue(any(event.kind == "session_private_notes" for event in world.memory_events))

            public_memory = world.retrieve_relevant_memory("宝箱王 捷径", include_private=False)
            private_memory = world.retrieve_relevant_memory("宝箱王 捷径", include_private=True)
            self.assertFalse(any("试探阿凛" in item for item in public_memory))
            self.assertTrue(any("试探阿凛" in item for item in private_memory))

            recalls = manager.recall_story_memories("星尘宝箱谭", "星尘迷宫 宝箱王")
            self.assertTrue(any("星尘迷宫第一夜" in item for item in recalls))

    def test_llm_summarizer_writes_public_story_without_private_leakage(self) -> None:
        payload = {
            "title": "模型不应控制标题",
            "public_evidence_entry_ids": [0],
            "private_evidence_entry_ids": [1],
            "location_entry_ids": [],
            "reward_entry_ids": [],
            "unresolved_entry_ids": [],
            "spotlight_characters": ["阿凛"],
            "important_npcs": ["宝箱王"],
            "entities": ["阿凛", "宝箱王", "并未出现的人"],
            "tags": ["dungeon", "boss"],
        }
        transport = FakeTransport(json.dumps(payload, ensure_ascii=False))
        client = OpenAICompatibleClient(
            LLMConfig(api_key="test", api_base_url="https://example.com", action_model="model", expressor_model="model"),
            transport=transport,
        )
        world = WorldState()

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(
                tmpdir,
                summarizer=LLMStorySummarizer(client=client, model="model"),
            )
            manager.append_message("星尘宝箱谭", "session-02", speaker="阿凛", content="我攻击宝箱王。")
            manager.append_message(
                "星尘宝箱谭",
                "session-02",
                speaker="GM后台",
                content="宝箱王是未来倒影。",
                role="gm_private",
            )

            summary = manager.finalize_session("星尘宝箱谭", "session-02", world_state=world)

            self.assertEqual(summary.title, "跑团记录 session-02")
            self.assertIn("llm", summary.tags)
            self.assertEqual(summary.public_summary, "阿凛：我攻击宝箱王。")
            self.assertEqual(summary.private_notes, ["GM后台：宝箱王是未来倒影。"])
            self.assertNotIn("并未出现的人", summary.entities)
            self.assertFalse(any("未来倒影" in item for item in manager.recall_story_memories("星尘宝箱谭", "宝箱王")))
            public_memory = world.retrieve_relevant_memory("宝箱王 未来倒影", include_private=False)
            self.assertFalse(any("未来倒影" in item for item in public_memory))
            self.assertEqual(len(transport.calls), 1)

    def test_live_context_lines_include_public_transcript_but_hide_private_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(tmpdir)
            manager.append_message("实时团", "s1", speaker="阿凛", content="我调查永雨工业城的灵魂管线。")
            manager.append_message(
                "实时团",
                "s1",
                speaker="GM后台",
                content="真正黑手是白塔港议会。",
                role="gm_private",
            )

            lines = manager.live_context_lines("实时团", "s1")
            formatted = manager.format_live_context("实时团", "s1")

            self.assertTrue(any("灵魂管线" in line for line in lines))
            self.assertFalse(any("白塔港议会" in line for line in lines))
            self.assertIn("当前场次实时公开记录", formatted)

    def test_llm_summarizer_fills_empty_structured_fields_from_public_transcript(self) -> None:
        payload = {
            "public_evidence_entry_ids": [],
            "private_evidence_entry_ids": [],
            "location_entry_ids": [],
            "reward_entry_ids": [],
            "unresolved_entry_ids": [],
            "spotlight_characters": [],
            "important_npcs": [],
            "entities": [],
            "tags": [],
        }
        transport = FakeTransport(json.dumps(payload, ensure_ascii=False))
        client = OpenAICompatibleClient(
            LLMConfig(api_key="test", api_base_url="https://example.com", action_model="model", expressor_model="model"),
            transport=transport,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(
                tmpdir,
                summarizer=LLMStorySummarizer(client=client, model="model"),
            )
            manager.append_message(
                "星匣金库",
                "session-03",
                speaker="阿凛",
                content="我进入旧港星匣金库的旋转镜面走廊，准备解除机关。",
            )
            manager.append_message(
                "星匣金库",
                "session-03",
                speaker="白河",
                content="如果找到宝箱侧室，我想取得银爪作为奖励。",
            )
            manager.append_message(
                "星匣金库",
                "session-03",
                speaker="GM后台",
                content="银爪其实会指向反派的月相计划。",
                role="gm_private",
            )

            summary = manager.finalize_session("星匣金库", "session-03", world_state=WorldState())

            self.assertTrue(summary.timeline)
            self.assertIn("阿凛", summary.spotlight_characters)
            self.assertTrue(summary.locations)
            self.assertTrue(summary.rewards)
            self.assertIn("GM后台：银爪其实会指向反派的月相计划。", summary.private_notes)
            self.assertFalse(any("月相计划" in item for item in summary.locations + summary.rewards))

    def test_llm_summary_prompt_samples_long_transcript_without_diagnostic_metadata(self) -> None:
        entries = [
            SessionTranscriptEntry(
                campaign_id="长篇战役",
                session_id="s1",
                created_at=f"2026-07-16T00:{index:02d}:00+00:00",
                role="user" if index % 2 == 0 else "assistant",
                speaker="玩家" if index % 2 == 0 else "时悠",
                content=f"第{index}条公开剧情。" + ("风铃廊里的行动继续推进。" * 80),
                metadata={
                    "mode": "game",
                    "route_decision": "不应进入摘要" * 2000,
                    "http_response": {"debug": "不应进入摘要" * 2000},
                },
            )
            for index in range(100)
        ]
        summarizer = LLMStorySummarizer(client=object(), model="model")  # type: ignore[arg-type]

        prompt = summarizer._user_prompt(entries, title="漫长的一夜", world_state=WorldState())

        self.assertLess(len(prompt), 40000)
        self.assertNotIn("不应进入摘要", prompt)
        self.assertIn("第0条公开剧情", prompt)
        self.assertIn("第99条公开剧情", prompt)
        self.assertIn("日志裁剪器", prompt)
        self.assertIn('"entry_id": 0', prompt)
        self.assertIn('"entry_id": 99', prompt)
        self.assertNotIn("known_public_memory", prompt)

    def test_llm_selector_cannot_retrieve_an_unseen_or_hallucinated_entry_id(self) -> None:
        entries = [
            SessionTranscriptEntry(
                campaign_id="长篇战役",
                session_id="s1",
                created_at=f"2026-07-16T00:{index:02d}:00+00:00",
                role="user",
                speaker="玩家",
                content=(
                    "模型不该看到的中段秘密。"
                    if index == 50
                    else f"第{index}条公开行动。"
                ),
            )
            for index in range(100)
        ]
        summarizer = LLMStorySummarizer(client=object(), model="model")  # type: ignore[arg-type]

        summary = summarizer._summary_from_payload(
            {
                "public_evidence_entry_ids": [50, 99],
                "private_evidence_entry_ids": [],
                "location_entry_ids": [],
                "reward_entry_ids": [],
                "unresolved_entry_ids": [],
            },
            campaign_id="长篇战役",
            session_id="s1",
            title="",
            entries=entries,
            world_state=WorldState(),
        )

        self.assertNotIn("模型不该看到的中段秘密", summary.public_summary)
        self.assertIn("第99条公开行动", summary.public_summary)

    def test_finalize_session_degrades_summary_without_blocking_persistence(self) -> None:
        transport = FailingTransport()
        client = OpenAICompatibleClient(
            LLMConfig(
                api_key="test",
                api_base_url="https://example.com",
                action_model="model",
                expressor_model="model",
                timeout_seconds=1,
                endpoint_attempt_timeout_seconds=1,
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SessionLogManager(
                tmpdir,
                summarizer=LLMStorySummarizer(
                    client=client,
                    model="model",
                    allow_fallback=False,
                ),
            )
            manager.append_message(
                "长篇战役",
                "s1",
                speaker="阿凛",
                content="伊莉雅在风铃廊守住了失忆旅人。",
            )

            summary = manager.finalize_session(
                "长篇战役",
                "s1",
                world_state=WorldState(),
                title="风铃廊之夜",
            )

            self.assertIn("伊莉雅在风铃廊守住了失忆旅人", summary.public_summary)
            self.assertTrue(manager.summary_path("长篇战役", "s1").exists())
            self.assertTrue(manager.transcript_txt_path("长篇战役", "s1").exists())
            self.assertTrue(manager.last_finalize_diagnostics["summary_degraded"])
            self.assertIn("summary provider timed out", manager.last_finalize_diagnostics["summary_error"])
            self.assertEqual(manager.last_finalize_diagnostics["fallback"], "HeuristicStorySummarizer")
            self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
