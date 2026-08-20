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


def scene_response_context(
    message: str,
    public_reply: str,
    public_facts=None,
) -> GMToolExecutionContext:
    context = tool_context(message)
    context.metadata["_gm_agent_required_followup_context"] = {
        "source_tool": "perform_scene_action",
        "required_tools": ["commit_scene_response"],
        "scene_response_followup": {
            "public_reply": public_reply,
            "public_facts": list(public_facts or []),
        },
    }
    return context


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

    def test_scene_tool_schema_states_positive_capability_boundary(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }
        schema = schemas["commit_scene_response"]
        facts = schema["parameters"]["properties"]["public_facts"]["description"]

        self.assertIn("行动主体范围为非人格化环境", schema["description"])
        self.assertIn("分别使用对应专用工具", schema["description"])
        self.assertNotIn("不能让NPC", schema["description"])
        self.assertIn("从public_reply逐字复制", facts)
        self.assertNotIn("不能概括", facts)

    def test_scene_response_commits_only_facts_spoken_in_locked_reply(self) -> None:
        message = "伊莉雅把通行牌递向会长，请她判断真假。"
        public_reply = "会长接过通行牌，翻到背面看了一眼。她没有把牌子交给巡守。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            scene_response_context(
                message,
                public_reply,
                ["她没有把牌子交给巡守。"],
            ),
            {
                "public_reply": public_reply,
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
        public_reply = "巡守看了看会长，没有伸手。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            scene_response_context(message, public_reply),
            {
                "public_reply": public_reply,
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
        public_reply = "闸门外传来勒缰声，三盏遮光提灯停在门缝后。"
        receipt = self.service.gm_scene_tools.commit_scene_response(
            scene_response_context(message, public_reply),
            {
                "public_reply": public_reply,
                "evidence": message,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["public_facts"], [])
        frame = self.runtime.app.scene_frame_manager.current_frame
        self.assertIn(receipt.public_fallback_reply, frame.recent_beats)

    def test_ordinary_message_cannot_open_freeform_scene_write_directly(self) -> None:
        receipt = self.service.gm_scene_tools.commit_scene_response(
            tool_context("我看看窗外。"),
            {
                "public_reply": "整片森林突然被永久风墙包围。",
                "public_facts": ["整片森林突然被永久风墙包围。"],
                "evidence": "我看看窗外。",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SCENE_RESPONSE_FOLLOWUP_REQUIRED")

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
        consequence = "仓库南侧的横梁已经坠落。"
        frame.committed_consequences.append(consequence)

        state = self.service.gm_scene_tools.state_summary(tool_context("查看场景"))

        self.assertEqual(state["committed_consequences"], [consequence])

    def test_system_material_beat_rejects_semantic_restatement_of_consequence(self) -> None:
        app = self.runtime.app
        app.scene_manager.current_scene.location = "白花碑驿站·仓库"
        frame = app.scene_frame_manager.ensure_frame(
            scene=app.scene_manager.current_scene,
            recent_chat="",
            world_state=app.world_state,
            character_manager=app.character_manager,
        )
        frame.current_pressure = "仓库梁柱正在火势中失去支撑。"
        frame.committed_consequences.append(
            "南侧梁柱被火焰烧裂，碎木坠落堵住了靠窗通道；"
            "仓库里只剩北侧通路可走。"
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
            "燃烧的木梁继续坠下，靠窗一侧已经无法通行；"
            "众人只能从北侧通路移动。"
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
        scene = self.runtime.app.scene_manager.current_scene
        self.runtime.app.clock_manager.add(
            Clock(
                name="外院火势蔓延",
                max_segments=6,
                current=2,
                clock_type="threat",
                scope="scene",
                scene_id=scene.scene_id,
                stakes="火势继续侵入有人活动的区域",
                completion_consequence="外院通道被火焰截断",
            )
        )
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_beat_purpose": "escalation",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "authority_id": "外院火势蔓延",
                        "source_kind": "active_clock",
                        "status": "triggered",
                        "scene_id": scene.scene_id,
                        "public_reply": "外院火线推进到石阶，东侧通道已经被火焰截断。",
                        "public_facts": ["东侧通道已经被火焰截断。"],
                    }
                ],
            }
        )
        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": "外院火线推进到石阶，东侧通道已经被火焰截断。",
                "public_facts": ["东侧通道已经被火焰截断。"],
                "change_authority": {
                    "kind": "active_clock",
                    "authority_ref": "外院火势蔓延",
                },
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

    def test_material_beat_cannot_promote_pressure_or_public_fact_into_new_hazard_power(
        self,
    ) -> None:
        frame = self.runtime.app.scene_frame_manager.ensure_frame(
            scene=self.runtime.app.scene_manager.current_scene,
            recent_chat="",
            world_state=self.runtime.app.world_state,
            character_manager=self.runtime.app.character_manager,
        )
        frame.current_pressure = "暴雨持续敲打屋顶。"
        frame.public_facts.append("排水沟里已经积起浅水。")
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )

        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": "积水突然封死所有出口，整座驿站已经无法离开。",
                "public_facts": ["整座驿站已经无法离开。"],
                "evidence": "系统主动节拍",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SCENE_CHANGE_AUTHORITY_REQUIRED")
        self.assertNotIn(receipt.public_fallback_reply, frame.recent_beats)

    def test_material_beat_rejects_active_clock_without_triggered_exact_effect(self) -> None:
        scene = self.runtime.app.scene_manager.current_scene
        self.runtime.app.clock_manager.add(
            Clock(
                name="仓库火势",
                max_segments=6,
                current=3,
                clock_type="threat",
                scope="scene",
                scene_id=scene.scene_id,
                stakes="仓库内可用空间持续缩小",
                completion_consequence="仓库结构失去支撑",
            )
        )
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )

        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": "一根燃烧的横梁落进仓库中央，原有通路只剩靠墙一侧。",
                "public_facts": ["原有通路只剩靠墙一侧。"],
                "change_authority": {
                    "kind": "active_clock",
                    "authority_ref": "仓库火势",
                },
                "evidence": "系统主动节拍",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SCENE_CHANGE_AUTHORITY_NOT_FOUND")

    def test_material_beat_rejects_unregistered_mechanical_hazard(self) -> None:
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
            }
        )

        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "public_reply": "齿轮机关合拢，北侧平台被切断。",
                "public_facts": ["北侧平台被切断。"],
                "change_authority": {
                    "kind": "structured_hazard",
                    "authority_ref": "gear-floor-2",
                },
                "evidence": "系统主动节拍",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "SCENE_CHANGE_AUTHORITY_NOT_FOUND")

    def test_material_beat_accepts_due_structured_hazard_from_trusted_context(self) -> None:
        scene = self.runtime.app.scene_manager.current_scene
        writer = SimpleNamespace(
            available=True,
            compose_public_scene_text=lambda **_kwargs: self.fail(
                "精确到期结果不应经过创作器改写"
            ),
        )
        self.runtime.app.scene_creative_writer = writer
        context = tool_context("系统主动节拍")
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "hazard_id": "gear-floor-2",
                        "source_kind": "structured_hazard",
                        "status": "triggered",
                        "scene_id": scene.scene_id,
                        "public_reply": "齿轮机关完成这一轮转动，北侧平台降到下层。",
                        "public_facts": ["北侧平台降到下层。"],
                    }
                ],
            }
        )

        receipt = self.service.gm_scene_tools.commit_scene_response(
            context,
            {
                "change_authority": {
                    "kind": "structured_hazard",
                    "authority_ref": "gear-floor-2",
                },
                "evidence": "系统主动节拍",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            receipt.public_fallback_reply,
            "齿轮机关完成这一轮转动，北侧平台降到下层。",
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

    def test_registry_exposes_explicit_clock_directions_and_removes_legacy_tool(self) -> None:
        names = {
            item["name"]
            for item in self.service.gm_tool_registry.schemas()
        }

        self.assertIn("fill_clock", names)
        self.assertIn("erase_clock", names)
        self.assertNotIn("change_clock", names)

    def test_erase_clock_applies_only_the_requested_direction(self) -> None:
        self.runtime.app.clock_manager.add(
            Clock(
                name="财团封锁协议",
                max_segments=6,
                current=3,
                clock_type="villain",
                scope="scene",
            )
        )
        message = "伊莉雅切断了协议的一条信号回路。"

        receipt = self.service.gm_clock_tools.erase_clock(
            tool_context(message),
            {
                "name": "财团封锁协议",
                "amount": 1,
                "cause": "skill_effect",
                "reason": "信号回路被明确切断",
                "public_reply": "协议上的一枚指示灯熄灭。\n【财团封锁协议】2/6",
                "evidence": "切断了协议的一条信号回路",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            self.runtime.app.clock_manager.get("财团封锁协议").current,
            2,
        )
        self.assertEqual(receipt.result["delta"], -1)

    def test_create_clock_uses_full_action_round_cadence(self) -> None:
        receipt = self._create_pressure()

        self.assertTrue(receipt.ok)
        clock = self.runtime.app.clock_manager.get("财团巡逻队逼近")
        self.assertEqual(clock.auto_advance_timing, "action_round_end")
        self.assertIn("完整行动轮", clock.auto_advance)
        self.assertTrue(receipt.lock_public_reply)

    def test_create_clock_accepts_named_owner_turn_timing(self) -> None:
        message = "安吉拉开始向实验室核心引导毁灭魔力。"
        receipt = self.service.gm_clock_tools.create_clock(
            tool_context(message),
            {
                "name": "灰飞烟灭",
                "segments": 6,
                "clock_type": "boss",
                "scope": "scene",
                "stakes": "安吉拉完成毁灭实验室的术式",
                "completion_consequence": "实验室与证据被魔力摧毁",
                "auto_advance": True,
                "auto_advance_timing": "owner_turn_start",
                "auto_advance_owner": "安吉拉",
                "auto_advance_every": 1,
                "visibility": "foreground",
                "public_reply": "魔力沿墙上的术式向核心汇聚。\n【灰飞烟灭】0/6",
                "evidence": "安吉拉开始向实验室核心引导毁灭魔力",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        clock = self.runtime.app.clock_manager.get("灰飞烟灭")
        self.assertEqual(clock.auto_advance_timing, "owner_turn_start")
        self.assertEqual(clock.auto_advance_owner, "安吉拉")
        self.assertIn("安吉拉", clock.auto_advance)

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
        changed = self.service.gm_clock_tools.fill_clock(
            tool_context(message),
            {
                "name": "仪式：封住裂隙",
                "amount": 1,
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
        receipt = self.service.gm_clock_tools.fill_clock(
            tool_context(message),
            {
                "name": "打开旧路",
                "amount": 1,
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
        receipt = self.service.gm_clock_tools.fill_clock(
            tool_context(message),
            {
                "name": "财团巡逻队逼近",
                "amount": 1,
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
        receipt = self.service.gm_clock_tools.fill_clock(
            tool_context(message),
            {
                "name": "财团巡逻队逼近",
                "amount": 1,
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
