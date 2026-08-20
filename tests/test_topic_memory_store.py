from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from os import utime

from fu_gm.components.session_log_manager import SessionLogManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.components.world_state import WorldState
from fu_gm.models import MemoryVisibility


class TopicMemoryStoreTests(unittest.TestCase):
    def test_memory_lifecycle_metadata_and_supersession(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TopicMemoryStore(tmpdir)
            first = store.write_topic_memory(
                "长团",
                visibility=MemoryVisibility.PUBLIC,
                memory_type="npc",
                title="旧约定",
                body="守门人允许队伍从东门通过。",
                filename="old",
                snapshot_version_at_write=7,
            )
            replacement = store.write_topic_memory(
                "长团",
                visibility=MemoryVisibility.PUBLIC,
                memory_type="npc",
                title="新约定",
                body="守门人改为允许队伍从北门通过。",
                filename="new",
                snapshot_version_at_write=8,
            )

            relative_old = str(first.relative_to(store.root / "长团" / "memory"))
            relative_new = str(replacement.relative_to(store.root / "长团" / "memory"))
            self.assertTrue(
                store.supersede_memory(
                    "长团",
                    relative_old,
                    superseded_by=relative_new,
                )
            )
            active = store.scan_frontmatter("长团")
            self.assertEqual([record.title for record in active], ["新约定"])
            all_records = store.scan_frontmatter(
                "长团",
                include_superseded=True,
            )
            old_record = next(record for record in all_records if record.title == "旧约定")
            self.assertEqual(old_record.snapshot_version_at_write, 7)
            self.assertEqual(old_record.superseded_by, relative_new)
            self.assertFalse(old_record.verified_at)
            self.assertTrue(
                store.verify_memory(
                    "长团",
                    relative_old,
                    snapshot_version=9,
                )
            )
            verified = next(
                record
                for record in store.scan_frontmatter(
                    "长团",
                    include_superseded=True,
                )
                if record.title == "旧约定"
            )
            self.assertTrue(verified.verified_at)
            self.assertEqual(verified.snapshot_version_at_write, 9)

    def test_consolidation_only_supersedes_exact_duplicate_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TopicMemoryStore(tmpdir)
            for filename in ("one", "two"):
                store.write_topic_memory(
                    "长团",
                    visibility=MemoryVisibility.PUBLIC,
                    memory_type="note",
                    title="同一条记忆",
                    body="完全相同的记忆正文。",
                    filename=filename,
                )
            store.write_topic_memory(
                "长团",
                visibility=MemoryVisibility.PUBLIC,
                memory_type="note",
                title="different",
                body="这条内容不同，不能被词法整理器猜成旧事实。",
                filename="different",
            )

            report = store.consolidate_if_due(
                "长团",
                completed_session_count=5,
                force=True,
            )
            self.assertTrue(report["ran"])
            self.assertEqual(report["superseded"], 1)
            active = store.scan_frontmatter("长团")
            self.assertEqual(len(active), 2)

    def test_consolidation_marks_conflicts_without_replacing_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TopicMemoryStore(tmpdir)
            for filename, body in (
                ("old", "守门人答应开放东门。"),
                ("new", "守门人后来只肯开放北门。"),
            ):
                store.write_topic_memory(
                    "长团",
                    visibility=MemoryVisibility.PUBLIC,
                    memory_type="npc",
                    title="守门人的约定",
                    body=body,
                    entities=["守门人"],
                    filename=filename,
                )

            report = store.consolidate_if_due(
                "长团",
                completed_session_count=5,
                force=True,
            )

            self.assertEqual(report["superseded"], 0)
            self.assertEqual(report["conflict_candidate_count"], 1)
            self.assertEqual(len(store.scan_frontmatter("长团")), 2)

    def test_stale_unmaintained_memory_triggers_time_based_consolidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TopicMemoryStore(tmpdir)
            path = store.write_topic_memory(
                "长团",
                visibility=MemoryVisibility.PUBLIC,
                memory_type="note",
                title="旧记忆",
                body="这条记录已经等待整理很久。",
                filename="old",
            )
            stale = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
            utime(path, (stale, stale))

            report = store.consolidate_if_due(
                "长团",
                completed_session_count=1,
            )

            self.assertTrue(report["ran"])
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
