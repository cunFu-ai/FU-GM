import tempfile
import unittest

from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, HeroDraft


def context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="旧团",
        session_id="s1",
        channel_id="group",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={"current_message": message},
    )


def focused_context(message: str) -> GMToolExecutionContext:
    result = context(message)
    result.metadata["inspection_focus"] = {
        "campaign_id": "目标团",
        "slot": "",
    }
    return result


class GMCampaignCreateToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.service._save_campaign({"campaign_id": "旧团"})

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_campaign_requires_literal_name_and_preserves_old_snapshot(self) -> None:
        message = "@时悠，新建战役白钟远航。"

        receipt = self.service.gm_campaign_tools.create_campaign(
            context(message),
            {"campaign_id": "白钟远航", "evidence": "新建战役白钟远航"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(self.service._memory_store().snapshot_exists("旧团"))
        self.assertTrue(self.service._memory_store().snapshot_exists("白钟远航"))
        self.assertEqual(self.service._current_campaign_id(), "白钟远航")

    def test_existing_campaign_is_not_overwritten_by_create(self) -> None:
        message = "@时悠，新建战役旧团。"

        receipt = self.service.gm_campaign_tools.create_campaign(
            context(message),
            {"campaign_id": "旧团", "evidence": "新建战役旧团"},
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CAMPAIGN_ALREADY_EXISTS")

    def test_create_autosaves_message_campaign_not_dashboard_focus(self) -> None:
        self.service._save_campaign({"campaign_id": "仪表盘焦点"})
        self.service._mark_current_campaign("仪表盘焦点")
        saved_campaigns: list[str] = []
        original_save = self.service._save_campaign

        def recording_save(payload):
            saved_campaigns.append(str(payload.get("campaign_id") or ""))
            return original_save(payload)

        self.service._save_campaign = recording_save
        receipt = self.service.gm_campaign_tools.create_campaign(
            context("@时悠，新建战役白钟远航。"),
            {
                "campaign_id": "白钟远航",
                "evidence": "新建战役白钟远航",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(saved_campaigns, ["旧团"])
        self.assertEqual(receipt.result["previous_campaign_id"], "旧团")

    def test_delete_named_slot_requires_literal_target_and_deletes_only_that_slot(self) -> None:
        self.service._save_campaign({"campaign_id": "旧团", "slot": "钟楼前"})
        self.service._save_campaign({"campaign_id": "旧团", "slot": "钟楼后"})

        receipt = self.service.gm_campaign_tools.delete_save(
            context("@时悠，删除旧团的存档钟楼前。"),
            {
                "scope": "slot",
                "campaign_id": "旧团",
                "slot": "钟楼前",
                "evidence": "删除旧团的存档钟楼前",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        slots = {item["slot"] for item in self.service._memory_store().list_save_slots("旧团")}
        self.assertNotIn("钟楼前", slots)
        self.assertIn("钟楼后", slots)
        self.assertTrue(self.service._memory_store().snapshot_exists("旧团"))

    def test_save_tool_cannot_mutate_a_campaign_other_than_message_binding(self) -> None:
        self.service._save_campaign({"campaign_id": "目标团"})

        receipt = self.service.gm_campaign_tools.save_campaign(
            context("@时悠，把目标团存到钟楼前。"),
            {
                "campaign_id": "目标团",
                "slot": "钟楼前",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "CROSS_CAMPAIGN_SAVE_NOT_ALLOWED",
        )
        self.assertFalse(
            self.service._memory_store().snapshot_exists(
                "目标团",
                slot="钟楼前",
            )
        )

    def test_failed_named_save_removes_partially_written_slot(self) -> None:
        original = self.service._save_campaign

        def fail_after_save(payload):
            original(payload)
            raise RuntimeError("injected save failure")

        self.service._save_campaign = fail_after_save
        receipt = self.service.gm_tool_registry.execute(
            "save_campaign",
            {"slot": "半成品"},
            context("@时悠，存到半成品。"),
        )

        self.assertFalse(receipt.ok)
        self.assertFalse(
            self.service._memory_store().snapshot_exists(
                "旧团",
                slot="半成品",
            )
        )

    def test_registry_rolls_back_partial_campaign_creation(self) -> None:
        previous_snapshot = self.service._memory_store()._snapshot_path("旧团").read_bytes()
        previous_runtimes = dict(self.service.runtimes)
        original = self.service._new_campaign

        def fail_after_create(payload):
            original(payload)
            raise RuntimeError("injected create failure")

        self.service._new_campaign = fail_after_create
        receipt = self.service.gm_tool_registry.execute(
            "create_campaign",
            {"campaign_id": "半成品新团"},
            context("@时悠，新建战役半成品新团。"),
        )

        self.assertFalse(receipt.ok)
        self.assertFalse(self.service._memory_store().snapshot_exists("半成品新团"))
        self.assertEqual(self.service._current_campaign_id(), "旧团")
        self.assertEqual(self.service.runtimes, previous_runtimes)
        self.assertEqual(
            self.service._memory_store()._snapshot_path("旧团").read_bytes(),
            previous_snapshot,
        )

    def test_registry_rolls_back_failed_load_after_target_was_applied(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        target.app.world_state.world_profile.campaign_title = "磁盘里的标题"
        self.service._save_campaign({"campaign_id": "目标团"})
        target.app.world_state.world_profile.campaign_title = "读档前的内存标题"
        self.service._mark_current_campaign("旧团")
        original = self.service._load_campaign

        def fail_after_load(payload):
            status, result = original(payload)
            self.assertEqual(status, 200)
            return 500, {**result, "ok": False, "error": "injected load failure"}

        self.service._load_campaign = fail_after_load
        receipt = self.service.gm_tool_registry.execute(
            "load_campaign",
            {"campaign_id": "目标团"},
            context("@时悠，读取战役目标团。"),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(self.service._current_campaign_id(), "旧团")
        self.assertIs(self.service.runtimes["目标团"], target)
        self.assertEqual(
            target.app.world_state.world_profile.campaign_title,
            "读档前的内存标题",
        )

    def test_registry_restores_campaign_deleted_before_failure_receipt(self) -> None:
        original_runtime = self.service.runtimes["旧团"]
        original_snapshot = self.service._memory_store()._snapshot_path("旧团").read_bytes()
        original = self.service._delete_campaign

        def fail_after_delete(payload):
            status, result = original(payload)
            self.assertEqual(status, 200)
            return 500, {**result, "ok": False, "error": "injected delete failure"}

        self.service._delete_campaign = fail_after_delete
        receipt = self.service.gm_tool_registry.execute(
            "delete_save",
            {"scope": "campaign", "campaign_id": "旧团"},
            context("@时悠，删除整个战役旧团。"),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(self.service._current_campaign_id(), "旧团")
        self.assertIs(self.service.runtimes["旧团"], original_runtime)
        self.assertEqual(
            self.service._memory_store()._snapshot_path("旧团").read_bytes(),
            original_snapshot,
        )

    def test_attendance_tool_records_player_account_not_character_name(self) -> None:
        runtime = self.service._runtime("旧团")
        runtime.app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
        )
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                traits=["pc"],
            )
        )

        receipt = self.service.gm_campaign_tools.set_player_attendance(
            context("@时悠，我先离席一会。"),
            {
                "mode": "away",
                "player": "伊莉雅",
                "reason": "短暂离席",
                "evidence": "我先离席一会",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertIn("阿凛", runtime.app.world_state.absent_players)
        self.assertNotIn("伊莉雅", runtime.app.world_state.absent_players)
        self.assertTrue(self.service._memory_store().snapshot_exists("旧团"))

    def test_attendance_tool_does_not_infer_absence_from_silence(self) -> None:
        receipt = self.service.gm_campaign_tools.set_player_attendance(
            context("loading一直没有回答。"),
            {
                "mode": "away",
                "player": "loading",
                "evidence": "",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ATTENDANCE_EVIDENCE_REQUIRED")
        self.assertNotIn(
            "loading",
            self.service._runtime("旧团").app.world_state.absent_players,
        )

    def test_inspect_campaign_reads_public_content_without_switching_current_campaign(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        profile = target.app.world_state.world_profile
        profile.world_style = "藤蔓与钢铁共生的幻想世界"
        profile.group_concept = "追寻失落传说的旅行英雄团"
        profile.major_locations["星落尖塔"] = "沉入地下的失陷学院"
        profile.gm_secret_notes.append("不可公开的幕后真相")
        profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
            theme="归属",
            origin="星落尖塔",
        )
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._mark_current_campaign("旧团")

        receipt = self.service.gm_campaign_tools.inspect_campaign(
            context("@时悠，看看目标团里面有什么。"),
            {"campaign_id": "目标团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.service._current_campaign_id(), "旧团")
        self.assertEqual(receipt.result["hero_drafts"][0]["hero_name"], "艾丽妮")
        self.assertEqual(
            receipt.result["world"]["profile"]["major_locations"]["星落尖塔"],
            "沉入地下的失陷学院",
        )
        self.assertNotIn("gm_secret_notes", receipt.result["world"]["profile"])
        self.assertNotIn("不可公开的幕后真相", receipt.public_fallback_reply)

    def test_hero_drafts_can_be_read_from_named_campaign_without_loading_it(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        target.app.world_state.world_profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
            identity="被放逐的学徒",
        )
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._mark_current_campaign("旧团")

        receipt = self.service.gm_campaign_tools.get_hero_drafts(
            context("@时悠，目标团有哪些角色？"),
            {"scope": "all", "campaign_id": "目标团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["campaign_id"], "目标团")
        self.assertEqual(receipt.result["source"], "persisted_snapshot")
        self.assertEqual(receipt.result["drafts"][0]["hero_name"], "艾丽妮")
        self.assertEqual(self.service._current_campaign_id(), "旧团")

    def test_hero_state_returns_authoritative_draft_budget_and_formal_resources(self) -> None:
        runtime = self.service._runtime("旧团")
        runtime.app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="诺艾尔",
            equipment=["匕首（钢匕首模板）", "细剑"],
            equipment_slots={"main_hand": "细剑", "off_hand": "匕首"},
        )

        draft_receipt = self.service.gm_campaign_tools.get_hero_state(
            context("@时悠，我现在还有多少初始装备预算？"),
            {"scope": "mine"},
        )

        self.assertTrue(draft_receipt.ok, draft_receipt.message)
        ledger = draft_receipt.result["drafts"][0]["equipment_ledger"]
        self.assertEqual(ledger["spent"], 350)
        self.assertEqual(ledger["budget_remaining"], 150)
        self.assertEqual(
            ledger["items"][0],
            {
                "display_name": "匕首",
                "template_name": "钢匕首",
                "price": 150,
                "required_ability": "",
            },
        )
        self.assertIn("还剩150Z", draft_receipt.public_fallback_reply)

        runtime.app.character_manager.add(
            Character(
                name="诺艾尔",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=32,
                max_mp=45,
                mp=20,
                traits=["pc"],
                inventory_points=4,
                max_inventory_points=6,
                fabula_points=2,
                zenit=230,
                equipment=["匕首", "细剑"],
                equipment_templates={"匕首": "钢匕首"},
                equipped_main_hand="细剑",
                equipped_off_hand="匕首",
            )
        )
        formal_receipt = self.service.gm_campaign_tools.get_hero_state(
            context("@时悠，诺艾尔现在有多少钱和多少物资点？"),
            {"scope": "named", "subjects": ["诺艾尔"]},
        )

        self.assertTrue(formal_receipt.ok, formal_receipt.message)
        formal = formal_receipt.result["characters"][0]
        self.assertEqual(formal["zenit"], 230)
        self.assertEqual(formal["inventory_points"], 4)
        self.assertEqual(formal["equipped"]["off_hand"], "匕首")

    def test_world_state_can_be_read_from_named_campaign_without_exposing_secrets(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        profile = target.app.world_state.world_profile
        profile.core_themes.append("钢铁生命拥有独立灵魂")
        profile.mysteries.append("星落尖塔为何沉没？")
        profile.gm_secret_notes.append("塔底其实封印着反派")
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._mark_current_campaign("旧团")

        receipt = self.service.gm_campaign_tools.get_world_state(
            context("@时悠，看看目标团的世界状态。"),
            {"campaign_id": "目标团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        profile_result = receipt.result["world"]["profile"]
        self.assertEqual(profile_result["core_themes"], ["钢铁生命拥有独立灵魂"])
        self.assertEqual(profile_result["mysteries"], ["星落尖塔为何沉没？"])
        self.assertNotIn("gm_secret_notes", profile_result)
        self.assertNotIn("塔底其实封印着反派", receipt.public_fallback_reply)
        self.assertEqual(self.service._current_campaign_id(), "旧团")

    def test_unqualified_followups_read_the_inspection_focus(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        profile = target.app.world_state.world_profile
        profile.world_style = "群岛酒馆幻想"
        profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
        )
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._mark_current_campaign("旧团")

        heroes = self.service.gm_campaign_tools.get_hero_drafts(
            focused_context("@时悠，有哪些角色？"),
            {"scope": "all"},
        )
        world = self.service.gm_campaign_tools.get_world_state(
            focused_context("@时悠，看看世界状态。"),
            {},
        )

        self.assertTrue(heroes.ok, heroes.message)
        self.assertEqual(heroes.result["campaign_id"], "目标团")
        self.assertEqual(heroes.result["source"], "persisted_snapshot")
        self.assertEqual(heroes.result["drafts"][0]["hero_name"], "艾丽妮")
        self.assertTrue(world.ok, world.message)
        self.assertEqual(world.result["campaign_id"], "目标团")
        self.assertEqual(world.result["source"], "persisted_snapshot")
        self.assertEqual(world.result["world"]["profile"]["world_style"], "群岛酒馆幻想")
        self.assertEqual(self.service._current_campaign_id(), "旧团")

    def test_explicit_current_campaign_overrides_the_inspection_focus(self) -> None:
        current = self.service._runtime("旧团")
        current.app.world_state.world_profile.world_style = "当前团的实时设定"
        target = self.service._runtime("目标团", auto_load=False)
        target.app.world_state.world_profile.world_style = "另一个存档"
        self.service._save_campaign({"campaign_id": "目标团"})

        receipt = self.service.gm_campaign_tools.get_world_state(
            focused_context("@时悠，看看当前团的世界状态。"),
            {"campaign_id": "旧团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["campaign_id"], "旧团")
        self.assertEqual(receipt.result["source"], "live_runtime")
        self.assertEqual(
            receipt.result["world"]["profile"]["world_style"],
            "当前团的实时设定",
        )

    def test_load_receipt_preserves_section_counts_instead_of_only_key_names(self) -> None:
        target = self.service._runtime("目标团", auto_load=False)
        target.app.world_state.world_profile.hero_drafts["loading"] = HeroDraft(
            player_name="loading",
            hero_name="艾丽妮",
        )
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._mark_current_campaign("旧团")

        receipt = self.service.gm_campaign_tools.load_campaign(
            context("@时悠，读取目标团。"),
            {"campaign_id": "目标团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        sections = receipt.result["loaded_sections"]
        self.assertIsInstance(sections, dict)
        self.assertEqual(sections["characters"], 0)
        self.assertIn("world_profile", sections["world_state_keys"])

    def test_load_autosaves_the_message_campaign_not_global_dashboard_focus(self) -> None:
        self.service._save_campaign({"campaign_id": "目标团"})
        self.service._save_campaign({"campaign_id": "仪表盘焦点"})
        self.service._mark_current_campaign("仪表盘焦点")
        saved_campaigns: list[str] = []
        original_save = self.service._save_campaign

        def recording_save(payload):
            saved_campaigns.append(str(payload.get("campaign_id") or ""))
            return original_save(payload)

        self.service._save_campaign = recording_save
        receipt = self.service.gm_campaign_tools.load_campaign(
            context("@时悠，读取目标团。"),
            {"campaign_id": "目标团"},
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(saved_campaigns, ["旧团"])


if __name__ == "__main__":
    unittest.main()
