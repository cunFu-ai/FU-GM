from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fu_gm.http_server import FUGMHttpService


class CampaignAdminTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def import_payload(title: str) -> dict[str, object]:
        return {
            "summary": "迁移预览",
            "world_updates": {
                "campaign_title": title,
            },
        }

    def test_dry_run_with_base_slot_does_not_replace_live_runtime(self) -> None:
        runtime = self.service._runtime("长团", auto_load=False)
        profile = runtime.app.world_state.world_profile
        profile.campaign_title = "基底标题"
        self.service._save_campaign(
            {
                "campaign_id": "长团",
                "slot": "旧基底",
            }
        )
        profile.campaign_title = "正在游玩的标题"
        runtime.last_loaded_slot = "当前现场"
        self.service._mark_current_campaign("另一个团")

        status, result = self.service._import_chat_log(
            {
                "campaign_id": "长团",
                "session_id": "s1",
                "base_slot": "旧基底",
                "dry_run": True,
                "import_payload": self.import_payload("预览里的标题"),
            }
        )

        self.assertEqual(status, 200, result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(profile.campaign_title, "正在游玩的标题")
        self.assertEqual(runtime.last_loaded_slot, "当前现场")
        self.assertEqual(self.service.current_campaign_id, "另一个团")

    def test_import_failure_after_snapshot_write_rolls_back_state_and_files(self) -> None:
        runtime = self.service._runtime("长团", auto_load=False)
        runtime.app.world_state.world_profile.campaign_title = "导入前"
        self.service._save_campaign({"campaign_id": "长团"})
        snapshot_path = self.service._memory_store()._snapshot_path("长团")
        snapshot_before = snapshot_path.read_bytes()

        with patch.object(
            self.service,
            "_write_import_artifact",
            side_effect=RuntimeError("injected artifact failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected artifact failure",
            ):
                self.service._import_chat_log(
                    {
                        "campaign_id": "长团",
                        "session_id": "s1",
                        "import_payload": self.import_payload("不应留下"),
                    }
                )

        self.assertEqual(
            runtime.app.world_state.world_profile.campaign_title,
            "导入前",
        )
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
        import_dir = Path(self.tempdir.name) / "长团" / "imports"
        self.assertFalse(list(import_dir.glob("*.json")))

    def test_failed_load_restores_live_state_and_runtime_metadata(self) -> None:
        runtime = self.service._runtime("长团", auto_load=False)
        runtime.app.world_state.world_profile.campaign_title = "磁盘标题"
        self.service._save_campaign({"campaign_id": "长团"})
        runtime.app.world_state.world_profile.campaign_title = "现场标题"
        runtime.loaded_from_disk = False
        runtime.last_loaded_slot = "现场槽"
        previous_path = runtime.last_saved_path
        self.service._mark_current_campaign("当前团")

        def fail_after_mutation(*_args: object, **_kwargs: object) -> dict:
            runtime.app.world_state.world_profile.campaign_title = "半读档状态"
            raise RuntimeError("injected load failure")

        with patch.object(
            runtime.app,
            "load_campaign_memory",
            side_effect=fail_after_mutation,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected load failure"):
                self.service._load_campaign({"campaign_id": "长团"})

        self.assertEqual(
            runtime.app.world_state.world_profile.campaign_title,
            "现场标题",
        )
        self.assertFalse(runtime.loaded_from_disk)
        self.assertEqual(runtime.last_loaded_slot, "现场槽")
        self.assertEqual(runtime.last_saved_path, previous_path)
        self.assertEqual(self.service.current_campaign_id, "当前团")

    def test_cross_campaign_loads_time_out_instead_of_deadlocking(self) -> None:
        for campaign_id in ("甲团", "乙团"):
            runtime = self.service._runtime(campaign_id, auto_load=False)
            runtime.app.world_state.world_profile.campaign_title = campaign_id
            self.service._save_campaign({"campaign_id": campaign_id})
        self.service.campaign_lock_timeout_seconds = 0.1
        barrier = threading.Barrier(2)

        def cross_load(source_id: str, target_id: str):
            source = self.service._runtime(source_id)
            with source.transaction_lock:
                barrier.wait(timeout=1)
                return self.service._load_campaign(
                    {"campaign_id": target_id}
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cross_load, "甲团", "乙团")
            second = pool.submit(cross_load, "乙团", "甲团")
            results = [
                first.result(timeout=2),
                second.result(timeout=2),
            ]

        self.assertTrue(any(status == 409 for status, _body in results))
        self.assertTrue(
            all(
                status in {200, 409}
                for status, _body in results
            )
        )

    def test_deleted_runtime_rejects_late_autosave(self) -> None:
        runtime = self.service._runtime("待删除团", auto_load=False)
        self.service._save_campaign({"campaign_id": "待删除团"})

        status, result = self.service._delete_campaign(
            {
                "campaign_id": "待删除团",
                "delete_all": True,
                "confirm": "确认删除",
            }
        )

        self.assertEqual(status, 200, result)
        self.assertTrue(runtime.retired)
        with self.assertRaisesRegex(RuntimeError, "拒绝迟到"):
            self.service._autosave_campaign(runtime, "待删除团")
        self.assertFalse(
            self.service._memory_store()._campaign_dir("待删除团").exists()
        )

    def test_two_imports_in_same_second_keep_separate_audit_artifacts(self) -> None:
        for title in ("第一次导入", "第二次导入"):
            status, result = self.service._import_chat_log(
                {
                    "campaign_id": "长团",
                    "session_id": "s1",
                    "import_payload": self.import_payload(title),
                }
            )
            self.assertEqual(status, 200, result)

        import_dir = Path(self.tempdir.name) / "长团" / "imports"
        self.assertEqual(len(list(import_dir.glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
