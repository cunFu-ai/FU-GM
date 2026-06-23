from __future__ import annotations

import tempfile
import unittest

from fu_gm.components.session_log_manager import SessionLogManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.components.world_state import WorldState
from fu_gm.models import MemoryVisibility


class TopicMemoryStoreTests(unittest.TestCase):
    def test_recall_scans_frontmatter_and_respects_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TopicMemoryStore(tmpdir)
            store.write_topic_memory(
                "星匣迷宫",
                visibility=MemoryVisibility.PUBLIC,
                memory_type="npc",
                title="酒馆老板汤姆",
                description="汤姆曾给过英雄们假情报。",
                body="汤姆在灰鸦酒馆经营吧台，公开记录中他给过一条会误导队伍的线索。",
                entities=["汤姆", "灰鸦酒馆"],
                tags=["npc", "tavern"],
            )
            store.write_topic_memory(
                "星匣迷宫",
                visibility=MemoryVisibility.PRIVATE,
                memory_type="gm_secret",
                title="汤姆的债主",
                description="汤姆其实被月神教派胁迫。",
                body="不要直接告诉玩家：汤姆提供假情报，是因为月神教派扣押了他的妹妹。",
                entities=["汤姆", "月神教派"],
                tags=["secret"],
                lock_level="draft",
            )

            public_only = store.recall("星匣迷宫", "我要质问汤姆", include_private=False)
            self.assertTrue(any("酒馆老板汤姆" in record.title for record in public_only))
            self.assertFalse(any(record.visibility == MemoryVisibility.PRIVATE for record in public_only))

            with_private = store.recall("星匣迷宫", "我要质问汤姆", include_private=True)
            self.assertTrue(any("汤姆的债主" in record.title for record in with_private))
            self.assertTrue(any("GM 私密draft记忆" in record.freshness_note for record in with_private))

            surfaced = {record.relative_path for record in with_private}
            recalled_again = store.recall(
                "星匣迷宫",
                "我要质问汤姆",
                include_private=True,
                already_surfaced=surfaced,
            )
            self.assertEqual(recalled_again, [])
            self.assertTrue((store.root / "星匣迷宫" / "memory" / "MEMORY.md").exists())

    def test_session_finalize_writes_topic_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            world = WorldState()
            manager = SessionLogManager(tmpdir)
            manager.append_message("星匣迷宫", "s1", speaker="阿凛", content="我们打开宝箱。")
            manager.append_message(
                "星匣迷宫",
                "s1",
                speaker="GM",
                content="宝箱底部藏着星尘钥匙。",
                role="assistant",
            )
            manager.append_message(
                "星匣迷宫",
                "s1",
                speaker="GM",
                content="星尘钥匙其实会唤醒月神教派。",
                role="gm_private",
            )

            summary = manager.finalize_session("星匣迷宫", "s1", world_state=world, title="星尘钥匙")
            store = TopicMemoryStore(tmpdir)

            public_records = store.recall("星匣迷宫", "星尘钥匙 宝箱", include_private=False)
            self.assertTrue(public_records)
            self.assertTrue(any(summary.title in record.title for record in public_records))
            self.assertFalse(any("月神教派" in record.format_for_prompt() for record in public_records))

            private_records = store.recall("星匣迷宫", "月神教派", include_private=True)
            self.assertTrue(any(record.visibility == MemoryVisibility.PRIVATE for record in private_records))


if __name__ == "__main__":
    unittest.main()
