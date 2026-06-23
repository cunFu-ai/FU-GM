import json
import tempfile
import unittest

from fu_gm.components.session_log_manager import LLMStorySummarizer, SessionLogManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import MemoryVisibility


class FakeTransport:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {"choices": [{"message": {"content": self.content}}]}


class SessionLogManagerTests(unittest.TestCase):
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
            "title": "愿望宝箱之战",
            "public_summary": "阿凛和白河在星尘迷宫击败宝箱王，净化了会吞噬愿望的星尘宝箱。",
            "short_memory": "阿凛与白河净化星尘宝箱，宝箱王暂时退场。",
            "timeline": ["英雄进入星尘迷宫。", "宝箱王现身。", "星尘宝箱被净化。"],
            "spotlight_characters": ["阿凛", "白河"],
            "important_npcs": ["宝箱王"],
            "locations": ["星尘迷宫"],
            "rewards": ["银爪"],
            "unresolved_threads": ["宝箱王为何执着于愿望？"],
            "private_notes": ["宝箱王是未来倒影。"],
            "entities": ["阿凛", "白河", "宝箱王", "星尘迷宫"],
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

            self.assertEqual(summary.title, "愿望宝箱之战")
            self.assertIn("llm", summary.tags)
            self.assertEqual(summary.private_notes, ["宝箱王是未来倒影。"])
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
            "title": "镜面金库序幕",
            "public_summary": "阿凛和白河进入旧港星匣金库，发现旋转镜面机关与宝箱侧室。",
            "short_memory": "队伍探索旧港星匣金库，发现镜面机关和银爪线索。",
            "timeline": [],
            "spotlight_characters": [],
            "important_npcs": [],
            "locations": [],
            "rewards": [],
            "unresolved_threads": [],
            "private_notes": [],
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


if __name__ == "__main__":
    unittest.main()
