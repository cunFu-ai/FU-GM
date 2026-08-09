import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fu_gm.components.gm_tool_pacing_observer import GMToolPacingObserver
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Clock, SceneType, SessionEpisodeProgress


def tool_context(
    message: str,
    *,
    campaign_id: str = "scene-tool-test",
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": "白花碑驿站外传来金属脚步声。",
        },
    )


class GMSceneToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.runtime = self.service._runtime("scene-tool-test")
        self.runtime.app.scene_manager.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="风铃廊",
            participants=["伊莉雅", "白花守望会会长"],
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_scene_response_commits_only_facts_spoken_in_locked_reply(self) -> None:
        message = "伊莉雅把通行牌递向会长，请她判断真假。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            tool_context(message),
            {
                "public_reply": "会长接过通行牌，翻到背面看了一眼。她没有把牌子交给巡守。",
                "public_facts": ["她没有把牌子交给巡守。"],
                "evidence": "把通行牌递向会长",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.lock_public_reply)
        self.assertEqual(
            receipt.public_fallback_reply,
            "会长接过通行牌，翻到背面看了一眼。她没有把牌子交给巡守。",
        )
        frame = self.runtime.app.scene_frame_manager.current_frame
        self.assertIn("她没有把牌子交给巡守。", frame.public_facts)

    def test_scene_response_discards_unspoken_fact_without_losing_public_beat(self) -> None:
        message = "伊莉雅示意巡守接过牌子。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            tool_context(message),
            {
                "public_reply": "巡守看了看会长，没有伸手。",
                "public_facts": ["巡守已经接过牌子。"],
                "evidence": "示意巡守接过牌子",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["public_facts"], [])
        self.assertEqual(
            receipt.result["discarded_public_facts"],
            ["巡守已经接过牌子。"],
        )
        frame = self.runtime.app.scene_frame_manager.current_frame
        self.assertNotIn("巡守已经接过牌子。", frame.public_facts)
        self.assertIn("巡守看了看会长，没有伸手。", frame.recent_beats)

    def test_scene_response_can_commit_a_beat_without_fact_index_entries(self) -> None:
        message = "远处的骑手已经抵达闸门。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            tool_context(message),
            {
                "public_reply": "闸门外传来勒缰声，三盏遮光提灯停在门缝后。",
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["public_facts"], [])
        frame = self.runtime.app.scene_frame_manager.current_frame
        self.assertIn(receipt.public_fallback_reply, frame.recent_beats)

    def test_scene_state_carries_recent_history_world_facts_and_story_items(self) -> None:
        app = self.runtime.app
        app.scene_frame_manager.history.append(
            SimpleNamespace(
                scene_name="旧路入口",
                location="旧路入口",
                public_facts=["弥莎将旧路闸门推开一人宽。"],
                established_facts=[],
                committed_consequences=[],
            )
        )
        app.world_state.remember_subject_fact(
            "旧路闸门",
            "旧路闸门已经由弥莎打开过。",
        )
        app.world_state.commit_story_item_action(
            operation="acquire",
            item_name="白蜡路封",
            actor="伊莉雅",
            scene_location="风铃廊",
            public_fact="白蜡路封现由伊莉雅持有。",
            source="test",
        )

        state = self.service.gm_scene_tools.state_summary(tool_context("查看场景"))

        self.assertEqual(
            state["recent_scene_history"][0]["public_facts"],
            ["弥莎将旧路闸门推开一人宽。"],
        )
        self.assertEqual(state["world_public_facts"][0]["subject"], "旧路闸门")
        self.assertEqual(state["story_items"][0]["holder"], "伊莉雅")

    def test_scene_state_exposes_delivered_committed_consequences(self) -> None:
        app = self.runtime.app
        frame = app.scene_frame_manager.ensure_frame(
            scene=app.scene_manager.current_scene,
            recent_chat="",
            world_state=app.world_state,
            character_manager=app.character_manager,
        )
        consequence = "牢门与铁栏上的封印提前重新亮起。"
        frame.committed_consequences.append(consequence)

        state = self.service.gm_scene_tools.state_summary(tool_context("查看场景"))

        self.assertEqual(state["committed_consequences"], [consequence])

    def test_system_material_beat_rejects_semantic_restatement_of_consequence(self) -> None:
        app = self.runtime.app
        app.scene_manager.current_scene.location = "卡里巴村监狱"
        frame = app.scene_frame_manager.ensure_frame(
            scene=app.scene_manager.current_scene,
            recent_chat="",
            world_state=app.world_state,
            character_manager=app.character_manager,
        )
        frame.current_pressure = "值班狱卒正试图恢复牢区封印并封锁走廊。"
        frame.committed_consequences.append(
            "回流的蓝光骤然反噬，牢门与铁栏上的封印提前重新亮起；"
            "牢区的动静也会更容易被值班室外的人察觉。"
        )
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )
        reply = (
            "走廊尽头的地面符文一盏盏亮起，蓝光沿着湿漉漉的石缝朝牢门蔓延；"
            "牢区的通路正在被重新封死。"
        )

        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": reply,
                "public_facts": [],
                "evidence": "系统主动节拍",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NO_NEW_MATERIAL_CHANGE")
        self.assertNotIn(reply, frame.recent_beats)

    def test_system_beat_records_directive_purpose_in_episode_progress(self) -> None:
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_beat_purpose": "escalation",
                "heartbeat_require_material_change": True,
            }
        )
        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": "值班室的门被撞开，两名狱卒冲进走廊。",
                "public_facts": ["两名狱卒冲进走廊。"],
                "evidence": "系统主动节拍",
            },
        )
        state = self.runtime.app.story_arc_manager.state
        state.current_pacing_plan.session_number = 1
        state.current_session_progress = SessionEpisodeProgress(session_number=1)

        GMToolPacingObserver().observe(self.runtime, context, [receipt])

        self.assertTrue(receipt.ok)
        self.assertEqual(
            state.current_session_progress.gm_beat_purposes,
            ["escalation"],
        )

    def test_scene_state_exposes_persisted_npc_commitments_to_agent(
        self,
    ) -> None:
        app = self.runtime.app
        frame = app.scene_frame_manager.ensure_frame(
            scene=app.scene_manager.current_scene,
            recent_chat="",
            world_state=app.world_state,
            character_manager=app.character_manager,
        )
        commitment = {
            "commitment_id": "scene-1|promise-1",
            "npc": "白花守望会会长",
            "public_statement": "我派一名白花守望者在旧路入口为你们带路。",
            "action": "白花守望者前往旧路入口并在那里带路",
            "promised_result": "在旧路入口为队伍带路",
            "trigger": "队伍抵达旧路入口",
            "status": "pending",
        }
        frame.deferred_npc_commitments.append(commitment)

        state = self.service.gm_scene_tools.state_summary(tool_context("继续前往旧路"))

        self.assertEqual(state["pending_npc_commitments"], [commitment])

class GMClockToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.runtime = self.service._runtime("scene-tool-test")
        self.runtime.app.scene_manager.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="风铃廊",
            participants=["伊莉雅", "洛岚"],
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _create_pressure(self, *, name: str = "财团巡逻队逼近", visibility: str = "foreground"):
        message = "远处的铁靴声正在逼近驿站。"
        return self.service.gm_clock_tools.create_clock(
            tool_context(message),
            {
                "name": name,
                "segments": 6,
                "clock_type": "threat",
                "scope": "scene",
                "stakes": "巡逻队包围驿站",
                "completion_consequence": "财团巡逻队抵达并封住出口",
                "auto_advance": True,
                "auto_advance_every": 1,
                "visibility": visibility,
                "public_reply": f"远处传来整齐的金属脚步声。\n【{name}】0/6",
                "evidence": "铁靴声正在逼近驿站",
            },
        )

    def test_create_clock_uses_full_action_round_cadence(self) -> None:
        receipt = self._create_pressure()

        self.assertTrue(receipt.ok)
        clock = self.runtime.app.clock_manager.get("财团巡逻队逼近")
        self.assertEqual(clock.auto_advance_timing, "action_round_end")
        self.assertIn("完整行动轮", clock.auto_advance)
        self.assertTrue(receipt.lock_public_reply)

    def test_generic_clock_tool_cannot_create_a_ritual_clock(self) -> None:
        message = "伊莉雅开始准备封住裂隙的仪式。"
        receipt = self.service.gm_clock_tools.create_clock(
            tool_context(message),
            {
                "name": "仪式：封住裂隙",
                "segments": 4,
                "clock_type": "ritual",
                "scope": "scene",
                "stakes": "准备仪式",
                "completion_consequence": "仪式可以进行最终施法检定",
                "auto_advance": False,
                "visibility": "foreground",
                "public_reply": "伊莉雅画下第一道术式。\n【仪式：封住裂隙】0/4",
                "evidence": "开始准备封住裂隙的仪式",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "RITUAL_REQUIRES_RITUAL_TOOL")
        self.assertFalse(self.runtime.app.clock_manager.exists("仪式：封住裂隙"))

    def test_generic_clock_tools_cannot_advance_or_close_a_ritual(self) -> None:
        self.runtime.app.clock_manager.add(
            Clock(
                name="仪式：封住裂隙",
                max_segments=4,
                clock_type="ritual",
                scope="scene",
            )
        )
        message = "伊莉雅继续描画封印术式。"
        changed = self.service.gm_clock_tools.change_clock(
            tool_context(message),
            {
                "name": "仪式：封住裂隙",
                "delta": 1,
                "cause": "direct_action_success",
                "reason": "继续准备仪式",
                "public_reply": "术式继续成形。\n【仪式：封住裂隙】1/4",
                "completion_facts": [],
                "evidence": "继续描画封印术式",
            },
        )
        closed = self.service.gm_clock_tools.close_clock(
            tool_context(message),
            {
                "name": "仪式：封住裂隙",
                "mode": "resolved",
                "reason": "仪式完成",
                "public_reply": "术式完成。\n【仪式：封住裂隙】0/4",
                "public_facts": ["术式完成。"],
                "evidence": "继续描画封印术式",
            },
        )

        self.assertFalse(changed.ok)
        self.assertEqual(changed.error_code, "RITUAL_REQUIRES_RITUAL_TOOL")
        self.assertFalse(closed.ok)
        self.assertEqual(closed.error_code, "RITUAL_REQUIRES_RITUAL_TOOL")
        self.assertEqual(
            self.runtime.app.clock_manager.get("仪式：封住裂隙").current,
            0,
        )

    def test_foreground_objective_cannot_complete_without_public_result_fact(self) -> None:
        manager = self.runtime.app.clock_manager
        manager.add(
            Clock(
                name="打开旧路",
                max_segments=4,
                current=3,
                clock_type="objective",
                scope="scene",
                completion_consequence="旧路闸门打开",
            )
        )
        message = "伊莉雅转动最后一道锁舌。"
        receipt = self.service.gm_clock_tools.change_clock(
            tool_context(message),
            {
                "name": "打开旧路",
                "delta": 1,
                "cause": "direct_action_success",
                "reason": "最后一道锁舌已经解除",
                "public_reply": "【打开旧路】4/4",
                "completion_facts": [],
                "evidence": "转动最后一道锁舌",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CLOCK_COMPLETION_FACT_REQUIRED")
        self.assertEqual(manager.get("打开旧路").current, 3)

    def test_registry_transaction_rolls_back_clock_when_autosave_fails(self) -> None:
        message = "远处的铁靴声正在逼近驿站。"
        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "create_clock",
                {
                    "name": "财团巡逻队逼近",
                    "segments": 6,
                    "clock_type": "threat",
                    "scope": "scene",
                    "stakes": "巡逻队包围驿站",
                    "completion_consequence": "财团巡逻队抵达并封住出口",
                    "auto_advance": True,
                    "auto_advance_every": 1,
                    "visibility": "foreground",
                    "public_reply": "远处传来整齐的金属脚步声。\n【财团巡逻队逼近】0/6",
                },
                tool_context(message),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertFalse(self.runtime.app.clock_manager.exists("财团巡逻队逼近"))
        self.assertIsNone(self.runtime.app.scene_frame_manager.current_frame)

    def test_opening_pressure_budget_rejects_second_foreground_threat(self) -> None:
        self.assertTrue(self._create_pressure().ok)
        message = "潮水也开始漫过旧路。"
        receipt = self.service.gm_clock_tools.create_clock(
            tool_context(message),
            {
                "name": "潮水没顶",
                "segments": 6,
                "clock_type": "threat",
                "scope": "scene",
                "stakes": "潮水封死旧路",
                "completion_consequence": "退路被潮水淹没",
                "auto_advance": False,
                "visibility": "foreground",
                "public_reply": "冷水漫上石阶。\n【潮水没顶】0/6",
                "evidence": "潮水也开始漫过旧路",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "FOREGROUND_PRESSURE_BUDGET_EXCEEDED")
        self.assertFalse(self.runtime.app.clock_manager.exists("潮水没顶"))

    def test_pressure_clock_cannot_fill_without_public_consequence(self) -> None:
        self.assertTrue(self._create_pressure().ok)
        self.runtime.app.clock_manager.advance("财团巡逻队逼近", 3)
        self.runtime.app.clock_manager.advance("财团巡逻队逼近", 2)
        message = "众人继续争论，没人去封住入口。"
        receipt = self.service.gm_clock_tools.change_clock(
            tool_context(message),
            {
                "name": "财团巡逻队逼近",
                "delta": 1,
                "cause": "gm_fictional_consequence",
                "reason": "完整行动轮结束且无人延缓巡逻队",
                "public_reply": "铁靴声已经压到门外。\n【财团巡逻队逼近】6/6",
                "completion_facts": [],
                "evidence": "没人去封住入口",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CLOCK_COMPLETION_FACT_REQUIRED")
        self.assertEqual(self.runtime.app.clock_manager.get("财团巡逻队逼近").current, 5)

    def test_pressure_clock_resolves_only_when_full_consequence_is_spoken(self) -> None:
        self.assertTrue(self._create_pressure().ok)
        self.runtime.app.clock_manager.advance("财团巡逻队逼近", 3)
        self.runtime.app.clock_manager.advance("财团巡逻队逼近", 2)
        message = "众人继续争论，没人去封住入口。"
        reply = (
            "最后一名财团机兵踏进风铃廊，巡逻队从两侧封住了出口。\n"
            "【财团巡逻队逼近】6/6"
        )
        receipt = self.service.gm_clock_tools.change_clock(
            tool_context(message),
            {
                "name": "财团巡逻队逼近",
                "delta": 1,
                "cause": "gm_fictional_consequence",
                "reason": "完整行动轮结束且无人延缓巡逻队",
                "public_reply": reply,
                "completion_facts": ["巡逻队从两侧封住了出口。"],
                "evidence": "没人去封住入口",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.lock_public_reply)
        self.assertFalse(self.runtime.app.clock_manager.exists("财团巡逻队逼近"))
        archived = self.runtime.app.clock_manager.archived_match("财团巡逻队逼近")
        self.assertIsNotNone(archived)
        self.assertEqual(archived.status, "resolved")

    def test_public_clock_reply_cannot_leak_backstage_labels(self) -> None:
        message = "远处的铁靴声正在逼近驿站。"
        receipt = self.service.gm_clock_tools.create_clock(
            tool_context(message),
            {
                "name": "财团巡逻队逼近",
                "segments": 6,
                "clock_type": "threat",
                "scope": "scene",
                "stakes": "巡逻队包围驿站",
                "completion_consequence": "财团巡逻队抵达并封住出口",
                "auto_advance": True,
                "visibility": "foreground",
                "public_reply": "威胁命刻【财团巡逻队逼近】0/6；赌注：包围现场。",
                "evidence": "铁靴声正在逼近驿站",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "CLOCK_BACKSTAGE_FIELD_LEAK")


if __name__ == "__main__":
    unittest.main()
