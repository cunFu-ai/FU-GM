from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fu_gm.conversation import MessageEvent
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_agent import GMToolAgentOutcome, LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import (
    Character,
    Clock,
    HeroDraft,
    MemoryVisibility,
    RollOutcome,
    SceneType,
)


class ScriptedGMClient:
    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self.responses = [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in responses
        ]
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> str:
        if not self.responses:
            raise AssertionError("GM工具测试缺少脚本化模型响应。")
        request = json.loads(kwargs["messages"][1].content)
        available = {
            str(item.get("name") or "")
            for item in list(request.get("available_tools") or [])
            if isinstance(item, dict)
        }
        try:
            scripted = json.loads(self.responses[0])
        except (TypeError, ValueError):
            scripted = {}
        requested_tools: list[str] = []
        if scripted.get("decision") == "call_tool":
            requested_tools = [str(scripted.get("tool_name") or "")]
        elif scripted.get("decision") == "call_tools":
            requested_tools = [
                str(item.get("tool_name") or "")
                for item in list(scripted.get("calls") or [])
                if isinstance(item, dict)
            ]
        missing = {
            name
            for name in requested_tools
            if name and name not in available
        }
        if (
            missing
            and GMCapabilityBroker.DISCOVERY_TOOL in available
        ):
            domains = GMCapabilityBroker.domains_for_tools(missing)
            if domains:
                return json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": GMCapabilityBroker.DISCOVERY_TOOL,
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试模型按协议取得所需能力。",
                        },
                        "reason": "先发现当前消息需要的能力。",
                    },
                    ensure_ascii=False,
                )
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


class FUGMHttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def install_agent(
        self,
        responses: list[dict[str, object] | str],
    ) -> ScriptedGMClient:
        client = ScriptedGMClient(responses)
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
        )
        return client

    @staticmethod
    def payload(
        message: str,
        *,
        message_id: str = "m-1",
        speaker: str = "阿凛",
        addressed: bool = False,
    ) -> dict[str, object]:
        return {
            "campaign_id": "http-agent-test",
            "session_id": "s1",
            "channel_id": "group-1",
            "speaker": speaker,
            "message": message,
            "message_id": message_id,
            "is_at_bot": addressed,
        }

    def test_health_and_dashboard_are_available_without_loading_a_campaign(self) -> None:
        health_status, health = self.service.handle("GET", "/health", {})
        dashboard_status, dashboard = self.service.handle("GET", "/dashboard", {})

        self.assertEqual(health_status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "fu-gm")
        self.assertTrue(health["runtime"]["gm_persona"]["loaded"])
        self.assertIn("source", health["runtime"]["gm_persona"])
        self.assertIn("core_gm_provider", health["runtime"])
        self.assertIsInstance(health["runtime"]["core_gm_provider"], dict)
        self.assertEqual(dashboard_status, 200)
        self.assertIsInstance(dashboard, str)
        self.assertIn("FU-GM", dashboard)

    def test_dashboard_keeps_tool_receipts_when_provider_fails_after_tool(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="本轮失败关闭。",
            role="assistant",
            channel_id="group-1",
            metadata={
                "mode": "gm_agent_unavailable",
                "tool_receipts": [
                    {
                        "tool_name": "inspect_supervisor_state",
                        "ok": True,
                        "state_changed": False,
                    }
                ],
                "agent_trace": [
                    {
                        "iteration": 1,
                        "decision": "call_tool",
                        "tool_name": "inspect_supervisor_state",
                    }
                ],
                "agent_error": "LLM HTTP 503: temporarily unavailable",
            },
        )

        status, dashboard = self.service.handle(
            "GET",
            "/v1/audit/dashboard?campaign_id=http-agent-test&session_id=s1&channel_id=group-1&include_private=true",
        )

        self.assertEqual(status, 200)
        event = dashboard["gm_tools"]["recent_events"][-1]
        self.assertEqual(
            event["receipts"][0]["tool_name"],
            "inspect_supervisor_state",
        )
        self.assertIn("503", event["error"])

    def test_loading_legacy_rendered_map_persists_internal_map_classification(self) -> None:
        runtime = self.service._runtime("legacy-rendered-map")
        world = runtime.app.world_state.world_profile
        world.continent_name = "宁姆格福"
        runtime.app.world_state.record_memory_event(
            "世界地图原画已生成：legacy-map.png",
            kind="world_map_visual",
            payload={"output_path": "legacy-map.png"},
        )
        with patch.object(
            runtime.app.session_zero_manager,
            "ensure_custom_map_card",
            return_value=False,
        ):
            runtime.app.save_campaign_memory("legacy-rendered-map")

        reloaded = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )._runtime("legacy-rendered-map")

        self.assertEqual(
            reloaded.app.world_state.world_profile.map_card,
            "自定义地图",
        )
        with open(
            f"{self.tempdir.name}/legacy-rendered-map/snapshot.json",
            encoding="utf-8",
        ) as handle:
            persisted = json.load(handle)
        self.assertEqual(
            persisted["world_state"]["world_profile"]["map_card"],
            "自定义地图",
        )
        self.assertEqual(
            persisted["session_zero"]["world"]["map_card"],
            "自定义地图",
        )

    def test_unconfigured_agent_fails_closed_and_stays_silent_for_table_talk(self) -> None:
        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("谁方便盯外面，谁继续谈判？"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_unavailable")
        self.assertEqual(response["target"], "silent")
        self.assertFalse(response["send_reply"])
        self.assertIn("single_agent_path", response["decision"]["tags"])
        self.assertEqual(
            self.service._runtime("http-agent-test").app.world_state.memory_events,
            [],
        )

    def test_unconfigured_agent_answers_direct_call_without_mutating_state(self) -> None:
        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("@时悠，在吗？", addressed=True),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["target"], "fu_gm")
        self.assertTrue(response["send_reply"])
        self.assertIn("没有启动", response["reply"])
        self.assertEqual(response["tool_receipts"] if "tool_receipts" in response else [], [])

    def test_agent_final_reply_reads_the_raw_current_message(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "巡守还没有接牌，他正看着伊莉雅递出的手。",
                    "reason": "玩家只完成了递出与示意。",
                }
            ]
        )
        message = "伊莉雅递出路牌，示意巡守接过去。"

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(message, addressed=True),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_reply")
        self.assertEqual(
            response["reply"],
            "巡守还没有接牌，他正看着伊莉雅递出的手。",
        )
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["current_message"], message)
        self.assertNotIn("semantic_route_decision", request["request_context"])

    def test_agent_silence_is_authoritative(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "silent",
                    "audience": "players",
                    "reason": "玩家正在彼此商量分工。",
                }
            ]
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("谁盯门口？我可以照顾旅人。"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["target"], "silent")
        self.assertEqual(response["route"], "gm_agent_silent")
        self.assertFalse(response["send_reply"])

    def test_agent_can_start_pre_session_through_typed_tool(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "start_session",
                    "arguments": {
                        "phase": "pre_session",
                        "reason": "玩家明确请求开始开团前讨论",
                    },
                    "reason": "需要建立会话阶段。",
                },
                {
                    "decision": "final",
                    "reply": "先聊聊这次大家想要怎样的故事。",
                    "reason": "开团前讨论已经开始。",
                },
            ]
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("@时悠，开始准备跑团。", addressed=True),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_tool")
        self.assertEqual(response["gate"]["status"], "pre_session")
        start_receipt = next(
            item
            for item in response["tool_receipts"]
            if item["tool_name"] == "start_session"
        )
        self.assertTrue(start_receipt["state_changed"])
        self.assertEqual(response["reply"], "先聊聊这次大家想要怎样的故事。")

    def test_typed_tool_write_autosaves_and_restores_after_service_restart(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "start_session",
                    "arguments": {
                        "phase": "pre_session",
                        "reason": "隔离黄金路径验证",
                    },
                    "reason": "建立可持久化的会话阶段。",
                },
                {
                    "decision": "final",
                    "reply": "隔离战役的开团准备已经开始。",
                    "reason": "权威工具已经提交会话阶段。",
                },
            ]
        )
        payload = self.payload(
            "@时悠，开始隔离黄金路径验证。",
            message_id="typed-write-restart-1",
            addressed=True,
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_tool")
        self.assertEqual(response["gate"]["status"], "pre_session")
        self.assertEqual(len(client.calls), 2)
        start_receipt = next(
            item
            for item in response["tool_receipts"]
            if item["tool_name"] == "start_session"
        )
        self.assertTrue(start_receipt["state_changed"])
        snapshot_path = self.service._runtime("http-agent-test").last_saved_path
        self.assertTrue(snapshot_path)
        self.assertTrue(self.service._runtime("http-agent-test").app.memory_store._snapshot_path("http-agent-test").exists())

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored_runtime = restarted._runtime("http-agent-test")
        replay_status, replay = restarted.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertTrue(restored_runtime.loaded_from_disk)
        restored_gate = restarted.session_gates.get(
            "http-agent-test",
            "group-1",
            "s1",
        )
        self.assertEqual(restored_gate.status, "pre_session")
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["reply"], response["reply"])

    def test_audit_log_failure_does_not_hide_a_committed_reply(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "start_session",
                    "arguments": {
                        "phase": "pre_session",
                        "reason": "玩家明确请求开始开团前讨论",
                    },
                    "reason": "需要建立会话阶段。",
                },
                {
                    "decision": "final",
                    "reply": "先聊聊这次大家想要怎样的故事。",
                    "reason": "开团前讨论已经开始。",
                },
            ]
        )
        payload = self.payload(
            "@时悠，开始准备跑团。",
            message_id="audit-log-failure-1",
            addressed=True,
        )
        runtime = self.service._runtime("http-agent-test")

        with patch.object(
            runtime.log_manager,
            "append_turn",
            side_effect=OSError("transcript disk unavailable"),
        ):
            first_status, first = self.service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )
        second_status, second = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["gate"]["status"], "pre_session")
        self.assertTrue(first["send_reply"])
        self.assertIn("transcript disk unavailable", first["audit_log_error"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["reply"], second["reply"])
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(
            runtime.log_manager.last_append_diagnostics["ok"]
        )
        health_status, health = self.service.handle("GET", "/health", {})
        self.assertEqual(health_status, 200)
        self.assertIn(
            "transcript disk unavailable",
            health["active_runtime"]["session_audit_log"]["error"],
        )

    def test_reply_ledger_failure_degrades_without_repeating_a_committed_turn(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "start_session",
                    "arguments": {
                        "phase": "pre_session",
                        "reason": "玩家明确请求开始开团前讨论",
                    },
                    "reason": "需要建立会话阶段。",
                },
                {
                    "decision": "final",
                    "reply": "先聊聊这次大家想要怎样的故事。",
                    "reason": "开团前讨论已经开始。",
                },
            ]
        )
        payload = self.payload(
            "@时悠，开始准备跑团。",
            message_id="reply-ledger-failure-1",
            addressed=True,
        )
        original_append = self.service.reply_ledger._append_record

        def fail_reply_record(campaign_id, record):
            if record.get("record_type") == "reply_envelope":
                raise OSError("reply ledger disk unavailable")
            return original_append(campaign_id, record)

        with patch.object(
            self.service.reply_ledger,
            "_append_record",
            side_effect=fail_reply_record,
        ):
            first_status, first = self.service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )
            second_status, second = self.service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["gate"]["status"], "pre_session")
        self.assertTrue(first["send_reply"])
        self.assertIn(
            "reply ledger disk unavailable",
            first["reply_ledger_warning"]["error"],
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["reply"], second["reply"])
        self.assertEqual(len(client.calls), 2)

        recovery_event = MessageEvent.from_payload(
            self.payload(
                "恢复写盘探针",
                message_id="reply-ledger-recovery-2",
            )
        )
        self.service.reply_ledger.register_event(recovery_event)
        self.assertTrue(self.service.reply_ledger.persistence_status()["ok"])

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restarted.gm_tool_agent = None
        restart_status, replay = restarted.handle(
            "POST",
            "/v1/message/route",
            payload,
        )
        self.assertEqual(restart_status, 200)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["reply"], first["reply"])

    def test_game_turn_endpoint_uses_the_same_single_agent_authority(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "门外的脚步还在远处，没有抵达驿站。",
                    "reason": "回答当前可观察状态。",
                }
            ]
        )

        status, response = self.service.handle(
            "POST",
            "/v1/game/turn",
            self.payload("追兵已经到门口了吗？"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_reply")
        self.assertTrue(response["core_gm_authority"])
        self.assertTrue(response["single_agent_path"])
        self.assertEqual(response["reply"], "门外的脚步还在远处，没有抵达驿站。")

    def test_buffered_messages_keep_each_original_speaker(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "silent",
                    "audience": "players",
                    "reason": "白河只是在提出分工建议。",
                },
                {
                    "decision": "final",
                    "reply": "伊莉雅在门边看见两道尚未靠近驿站的灯影。",
                    "reason": "阿凛已经执行观察。",
                },
            ]
        )
        payload = self.payload("批次占位", message_id="batch-parent")
        payload.update(
            {
                "batch_id": "batch-1",
                "batch_messages": [
                    {
                        "speaker": "白河",
                        "message": "要不要让伊莉雅去看门外？",
                        "timestamp": 1.0,
                        "payload": {"message_id": "m-white"},
                    },
                    {
                        "speaker": "阿凛",
                        "message": "伊莉雅走到门边观察外面的灯影。",
                        "timestamp": 2.0,
                        "payload": {"message_id": "m-arin"},
                    },
                ],
            }
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(response["batch_results"]), 2)
        self.assertEqual(response["batch_results"][0]["target"], "silent")
        self.assertEqual(response["batch_results"][1]["target"], "fu_gm")
        first = json.loads(client.calls[0]["messages"][1].content)
        second = json.loads(client.calls[1]["messages"][1].content)
        self.assertEqual(first["session"]["speaker"], "白河")
        self.assertEqual(second["session"]["speaker"], "阿凛")
        self.assertEqual(first["current_message"], "要不要让伊莉雅去看门外？")
        self.assertEqual(
            second["current_message"],
            "伊莉雅走到门边观察外面的灯影。",
        )
        self.assertIn("speaker_preserved", response["decision"]["tags"])

    def test_duplicate_message_id_does_not_run_the_agent_twice(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "我在。",
                    "reason": "回应直接呼叫。",
                }
            ]
        )
        payload = self.payload("@时悠，在吗？", addressed=True)

        first_status, first = self.service.handle("POST", "/v1/message/route", payload)
        second_status, second = self.service.handle("POST", "/v1/message/route", payload)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["reply"], second["reply"])
        self.assertEqual(len(client.calls), 1)

    def test_concurrent_duplicate_message_is_one_agent_transaction(self) -> None:
        class BlockingClient:
            def __init__(self) -> None:
                self.calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()
                self.lock = threading.Lock()

            def create_chat_completion(self, **_kwargs: object) -> str:
                with self.lock:
                    self.calls += 1
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("并发判重测试等待释放超时。")
                return json.dumps(
                    {
                        "decision": "final",
                        "reply": "只处理一次。",
                        "reason": "并发平台重投测试。",
                    },
                    ensure_ascii=False,
                )

        client = BlockingClient()
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
        )
        payload = self.payload("@时悠，推进一次。", addressed=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                payload,
            )
            self.assertTrue(client.entered.wait(timeout=1))
            second_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                payload,
            )
            time.sleep(0.05)
            client.release.set()
            first_status, first = first_future.result(timeout=2)
            second_status, second = second_future.result(timeout=2)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(client.calls, 1)
        self.assertEqual(first["reply"], second["reply"])
        self.assertTrue(first.get("deduplicated") or second.get("deduplicated"))

    def test_direct_session_end_cannot_skip_blocking_player_choice(self) -> None:
        runtime = self.service._runtime("end-window-test")
        runtime.app.interceptor.decision_window_manager.create(
            kind="zero_hp",
            owner="伊莉雅",
            prompt="选择牺牲或放弃抵抗。",
            blocking=True,
            allowed_responders=["伊莉雅"],
        )
        self.service.session_gates.activate(
            "end-window-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1")

        status, response = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "end-window-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "BLOCKING_DECISION_PENDING")
        self.assertTrue(
            self.service.session_gates.get(
                "end-window-test",
                "group-1",
                "s1",
            ).active
        )

    def test_direct_session_end_preserves_active_conflict_for_next_session(self) -> None:
        runtime = self.service._runtime("end-conflict-test")
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
        runtime.app.conflict_manager.start_scene(
            "未结束的伏击",
            ["伊莉雅", "财团机兵"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        self.service.session_gates.activate(
            "end-conflict-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1")

        status, response = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "end-conflict-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertTrue(runtime.app.conflict_manager.state.active)
        self.assertFalse(
            self.service.session_gates.get(
                "end-conflict-test",
                "group-1",
                "s1",
            ).active
        )

    def test_session_end_preserves_active_scene_and_scene_clock(self) -> None:
        runtime = self.service._runtime("end-scene-continuity-test")
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
        scene = runtime.app.start_scene(
            "钢铁墓园",
            SceneType.DUNGEON,
            location="守墓人营火",
            participants=["伊莉雅"],
            objective="找到核心墓室",
        )
        runtime.app.clock_manager.add(
            Clock(
                name="墓园深处的警觉",
                max_segments=6,
                current=2,
                clock_type="threat",
                scope="scene",
                scene_id=scene.scene_id,
            )
        )
        self.service.session_gates.activate(
            "end-scene-continuity-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1", participating_pcs=["伊莉雅"])

        status, response = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "end-scene-continuity-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertIs(runtime.app.scene_manager.current_scene, scene)
        self.assertTrue(scene.active)
        self.assertTrue(runtime.app.clock_manager.exists("墓园深处的警觉"))
        self.assertEqual(
            runtime.app.clock_manager.get("墓园深处的警觉").current,
            2,
        )

    def test_session_end_snapshot_restores_exact_mid_conflict_position(self) -> None:
        runtime = self.service._runtime("mid-conflict-resume-test")
        for name, traits in (
            ("伊莉雅", ["pc"]),
            ("墓园机兵", ["enemy"]),
        ):
            runtime.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=45,
                    mp=45,
                    traits=traits,
                )
            )
        scene = runtime.app.start_scene(
            "钢铁墓园伏击",
            SceneType.CONFLICT,
            location="齿轮门厅",
            participants=["伊莉雅", "墓园机兵"],
            objective="守住通往核心墓室的门",
        )
        runtime.app.clock_manager.add(
            Clock(
                name="核心墓室坍塌",
                max_segments=6,
                current=3,
                clock_type="threat",
                scope="scene",
                scene_id=scene.scene_id,
            )
        )
        runtime.app.conflict_manager.start_scene(
            "钢铁墓园伏击",
            ["伊莉雅", "墓园机兵"],
            player_side=["伊莉雅"],
            enemy_side=["墓园机兵"],
        )
        runtime.app.conflict_manager.state.round_number = 3
        runtime.app.conflict_manager.state.current_turn_index = 1
        runtime.app.conflict_manager.state.turn_serial = 7
        self.service.session_gates.activate(
            "mid-conflict-resume-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1", participating_pcs=["伊莉雅"])

        status, response = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "mid-conflict-resume-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])

        self.service.runtimes.clear()
        restored = self.service._runtime("mid-conflict-resume-test")
        conflict = restored.app.conflict_manager.state
        restored_scene = restored.app.scene_manager.current_scene

        self.assertTrue(restored.loaded_from_disk)
        self.assertTrue(conflict.active)
        self.assertEqual(conflict.round_number, 3)
        self.assertEqual(conflict.current_turn_index, 1)
        self.assertEqual(conflict.current_actor(), "墓园机兵")
        self.assertEqual(conflict.turn_serial, 7)
        self.assertIsNotNone(restored_scene)
        self.assertEqual(restored_scene.scene_id, scene.scene_id)
        self.assertEqual(restored_scene.location, "齿轮门厅")
        self.assertEqual(
            restored.app.clock_manager.get("核心墓室坍塌").current,
            3,
        )

    def test_direct_session_end_is_idempotent(self) -> None:
        runtime = self.service._runtime("end-idempotency-test")
        self.service.session_gates.activate(
            "end-idempotency-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1")
        payload = {
            "campaign_id": "end-idempotency-test",
            "session_id": "s1",
            "channel_id": "group-1",
        }

        first_status, first = self.service.handle(
            "POST",
            "/v1/session/end",
            payload,
        )
        feedback_count = len(
            runtime.app.story_arc_manager.state.session_feedback_history
        )
        second_status, second = self.service.handle(
            "POST",
            "/v1/session/end",
            payload,
        )

        self.assertEqual(first_status, 200)
        self.assertTrue(first["ok"])
        self.assertEqual(second_status, 200)
        self.assertTrue(second["ok"])
        self.assertTrue(second["already_ended"])
        self.assertEqual(
            len(runtime.app.story_arc_manager.state.session_feedback_history),
            feedback_count,
        )

    def test_session_end_rolls_back_authoritative_state_when_snapshot_write_fails(self) -> None:
        runtime = self.service._runtime("end-rollback-test")
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
        runtime.app.clock_manager.add(
            Clock(
                name="本场余震",
                max_segments=4,
                current=1,
                clock_type="threat",
                scope="session",
            )
        )
        self.service.session_gates.activate(
            "end-rollback-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1", participating_pcs=["伊莉雅"])

        with patch.object(
            runtime.app,
            "save_campaign_memory",
            side_effect=OSError("disk full"),
        ):
            status, response = self.service.handle(
                "POST",
                "/v1/session/end",
                {
                    "campaign_id": "end-rollback-test",
                    "session_id": "s1",
                    "channel_id": "group-1",
                },
            )

        self.assertEqual(status, 500)
        self.assertFalse(response["ok"])
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").experience_points,
            0,
        )
        self.assertTrue(runtime.app.session_ledger.active)
        self.assertFalse(runtime.app.session_ledger.settled)
        self.assertTrue(runtime.app.clock_manager.exists("本场余震"))
        self.assertTrue(
            self.service.session_gates.get(
                "end-rollback-test",
                "group-1",
                "s1",
            ).active
        )
        self.assertFalse(
            runtime.log_manager.summary_path(
                "end-rollback-test",
                "s1",
            ).exists()
        )
        self.assertFalse(
            runtime.log_manager.memory_path(
                "end-rollback-test",
                "s1",
            ).exists()
        )
        topic_store = runtime.log_manager.topic_memory_store
        public_summary = (
            topic_store._memory_dir(
                "end-rollback-test",
                MemoryVisibility.PUBLIC,
            )
            / "session_s1.md"
        )
        self.assertFalse(public_summary.exists())

    def test_session_end_recovers_gate_left_active_after_committed_snapshot(self) -> None:
        runtime = self.service._runtime("end-gate-recovery-test")
        runtime.app.session_ledger.session_id = "s1"
        runtime.app.session_ledger.active = False
        runtime.app.session_ledger.settled = True
        self.service.session_gates.activate(
            "end-gate-recovery-test",
            "group-1",
            "s1",
            status="adventure",
        )

        status, response = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "end-gate-recovery-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertTrue(response["already_ended"])
        self.assertEqual(response["gate"]["status"], "inactive")

    def test_session_end_rolls_back_if_gate_persistence_fails(self) -> None:
        runtime = self.service._runtime("end-gate-write-failure-test")
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
        self.service.session_gates.activate(
            "end-gate-write-failure-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1", participating_pcs=["伊莉雅"])
        self.service._autosave_campaign(
            runtime,
            "end-gate-write-failure-test",
        )
        original_deactivate = self.service.session_gates.deactivate

        def fail_after_gate_write(*args, **kwargs):
            original_deactivate(*args, **kwargs)
            raise OSError("gate fsync failed")

        with patch.object(
            self.service.session_gates,
            "deactivate",
            side_effect=fail_after_gate_write,
        ):
            status, response = self.service.handle(
                "POST",
                "/v1/session/end",
                {
                    "campaign_id": "end-gate-write-failure-test",
                    "session_id": "s1",
                    "channel_id": "group-1",
                },
            )

        self.assertEqual(status, 500)
        self.assertFalse(response["ok"])
        self.assertTrue(runtime.app.session_ledger.active)
        self.assertFalse(runtime.app.session_ledger.settled)
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").experience_points,
            0,
        )
        self.assertTrue(
            self.service.session_gates.get(
                "end-gate-write-failure-test",
                "group-1",
                "s1",
            ).active
        )
        self.assertFalse(
            runtime.log_manager.summary_path(
                "end-gate-write-failure-test",
                "s1",
            ).exists()
        )
        self.assertFalse(
            runtime.log_manager.memory_path(
                "end-gate-write-failure-test",
                "s1",
            ).exists()
        )
        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored = restarted._runtime("end-gate-write-failure-test").app
        self.assertTrue(restored.session_ledger.active)
        self.assertFalse(restored.session_ledger.settled)
        self.assertEqual(
            restored.character_manager.get("伊莉雅").experience_points,
            0,
        )

    def test_adventure_start_waits_for_mandatory_level_up(self) -> None:
        runtime = self.service._runtime("level-up-blocker-test")
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                experience_points=10,
                traits=["pc"],
            )
        )

        status, response = self.service.handle(
            "POST",
            "/v1/session/gate",
            {
                "campaign_id": "level-up-blocker-test",
                "session_id": "s2",
                "channel_id": "group-1",
                "status": "adventure",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["blocked"])
        self.assertEqual(
            response["blockers"]["progression"]["pending_level_ups"],
            ["伊莉雅"],
        )
        self.assertFalse(
            self.service.session_gates.get(
                "level-up-blocker-test",
                "group-1",
                "s2",
            ).active
        )

    def test_pure_session_zero_does_not_award_adventure_experience(self) -> None:
        runtime = self.service._runtime("session-zero-xp-test")
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
        gate_status, gate_result = self.service.handle(
            "POST",
            "/v1/session/gate",
            {
                "campaign_id": "session-zero-xp-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "status": "session_zero",
            },
        )
        end_status, ended = self.service.handle(
            "POST",
            "/v1/session/end",
            {
                "campaign_id": "session-zero-xp-test",
                "session_id": "s1",
                "channel_id": "group-1",
            },
        )

        self.assertEqual(gate_status, 200)
        self.assertTrue(gate_result["ok"])
        self.assertFalse(runtime.app.session_ledger.active)
        self.assertEqual(end_status, 200)
        self.assertTrue(ended["ok"])
        self.assertIsNone(ended["experience"])
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").experience_points,
            0,
        )

    def test_new_player_message_registers_only_their_pc_for_session_xp(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        for name in ("伊莉雅", "洛岚"):
            runtime.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=45,
                    mp=45,
                    traits=["pc"],
                )
            )
        runtime.app.world_state.world_profile.hero_drafts = {
            "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
            "白河": HeroDraft(player_name="白河", hero_name="洛岚"),
        }
        runtime.app.start_session_tracking("s1", participating_pcs=[])
        self.install_agent(
            [
                {
                    "decision": "silent",
                    "audience": "players",
                    "reason": "玩家正在和同伴商量。",
                }
            ]
        )

        status, _response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("我先看看外面的路。", speaker="阿凛"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(runtime.app.session_ledger.participating_pcs, {"伊莉雅"})
        self.assertEqual(runtime.app.character_manager.get("伊莉雅").fabula_points, 1)
        self.assertEqual(runtime.app.character_manager.get("洛岚").fabula_points, 0)

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored = restarted._runtime("http-agent-test").app
        self.assertIn("阿凛", restored.world_state.present_players)
        self.assertEqual(restored.session_ledger.participating_pcs, {"伊莉雅"})
        self.assertEqual(
            restored.character_manager.get("伊莉雅").fabula_points,
            1,
        )

    def test_durable_speaker_touch_rolls_back_when_autosave_fails(self) -> None:
        runtime = self.service._runtime("touch-speaker-rollback-test")
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                fabula_points=0,
                traits=["pc"],
            )
        )
        runtime.app.world_state.world_profile.hero_drafts = {
            "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
        }
        runtime.app.start_session_tracking("s1", participating_pcs=[])

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                self.service._touch_speaker(
                    runtime,
                    "阿凛",
                    persist=True,
                )

        self.assertNotIn("阿凛", runtime.app.world_state.present_players)
        self.assertEqual(runtime.app.session_ledger.participating_pcs, set())
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").fabula_points,
            0,
        )

    def test_inactive_casual_message_does_not_create_table_attendance(self) -> None:
        runtime = self.service._runtime("http-agent-test")

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("大家晚上好。", speaker="阿凛"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "gm_agent_unavailable")
        self.assertNotIn("阿凛", runtime.app.world_state.present_players)

    def test_slash_command_does_not_register_late_session_participant(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                fabula_points=0,
                traits=["pc"],
            )
        )
        runtime.app.world_state.world_profile.hero_drafts = {
            "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
        }
        runtime.app.start_session_tracking("s1", participating_pcs=[])

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("/fugm_save", speaker="阿凛"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "command_protocol_required")
        self.assertNotIn("阿凛", runtime.app.world_state.present_players)
        self.assertEqual(runtime.app.session_ledger.participating_pcs, set())
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").fabula_points,
            0,
        )

    def test_session_back_registers_returning_players_pc_once(self) -> None:
        runtime = self.service._runtime("back-participant-test")
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                fabula_points=0,
                traits=["pc"],
            )
        )
        runtime.app.world_state.world_profile.hero_drafts = {
            "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
        }
        runtime.app.start_session_tracking("s1", participating_pcs=[])
        runtime.app.world_state.mark_player_absent("阿凛", "暂离")

        payload = {
            "campaign_id": "back-participant-test",
            "session_id": "s1",
            "channel_id": "group-1",
            "speaker": "阿凛",
        }
        first_status, first = self.service.handle(
            "POST",
            "/v1/session/back",
            payload,
        )
        second_status, second = self.service.handle(
            "POST",
            "/v1/session/back",
            payload,
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(runtime.app.session_ledger.participating_pcs, {"伊莉雅"})
        self.assertEqual(runtime.app.character_manager.get("伊莉雅").fabula_points, 1)
        self.assertNotIn("阿凛", second["attendance"]["absent_players"])

    def test_session_away_rolls_back_snapshot_when_audit_log_fails(self) -> None:
        runtime = self.service._runtime("away-rollback-test")
        runtime.app.world_state.mark_player_present("阿凛")
        self.service._save_campaign({"campaign_id": "away-rollback-test"})
        snapshot_path = self.service._memory_store()._snapshot_path(
            "away-rollback-test"
        )
        snapshot_before = snapshot_path.read_bytes()

        with patch.object(
            runtime.log_manager,
            "append_message",
            side_effect=RuntimeError("injected log failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected log failure",
            ):
                self.service._session_away(
                    {
                        "campaign_id": "away-rollback-test",
                        "session_id": "s1",
                        "channel_id": "group-1",
                        "speaker": "阿凛",
                        "reason": "暂离",
                    }
                )

        self.assertNotIn(
            "阿凛",
            runtime.app.world_state.absent_players,
        )
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)

    def test_session_back_rolls_back_award_when_audit_log_fails(self) -> None:
        runtime = self.service._runtime("back-rollback-test")
        runtime.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                fabula_points=0,
                traits=["pc"],
            )
        )
        runtime.app.world_state.world_profile.hero_drafts = {
            "阿凛": HeroDraft(player_name="阿凛", hero_name="伊莉雅"),
        }
        runtime.app.start_session_tracking("s1", participating_pcs=[])
        runtime.app.world_state.mark_player_absent("阿凛", "暂离")
        self.service._save_campaign({"campaign_id": "back-rollback-test"})
        snapshot_path = self.service._memory_store()._snapshot_path(
            "back-rollback-test"
        )
        snapshot_before = snapshot_path.read_bytes()

        with patch.object(
            runtime.log_manager,
            "append_message",
            side_effect=RuntimeError("injected log failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected log failure",
            ):
                self.service._session_back(
                    {
                        "campaign_id": "back-rollback-test",
                        "session_id": "s1",
                        "channel_id": "group-1",
                        "speaker": "阿凛",
                    }
                )

        self.assertIn("阿凛", runtime.app.world_state.absent_players)
        self.assertEqual(
            runtime.app.session_ledger.participating_pcs,
            set(),
        )
        self.assertEqual(
            runtime.app.character_manager.get("伊莉雅").fabula_points,
            0,
        )
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)

    def test_slash_command_never_enters_natural_language_agent(self) -> None:
        client = self.install_agent([])

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("/fugm_save"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["route"], "command_protocol_required")
        self.assertFalse(response["ok"])
        self.assertEqual(len(client.calls), 0)

    def test_scene_opening_and_gm_beat_fail_closed_without_agent(self) -> None:
        opening_status, opening = self.service.handle(
            "POST",
            "/v1/game/scene-opening",
            self.payload("继续当前场景"),
        )
        beat_status, beat = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("让局面向前一步"),
        )

        self.assertEqual(opening_status, 200)
        self.assertFalse(opening["ok"])
        self.assertTrue(opening["single_agent_path"])
        self.assertEqual(beat_status, 200)
        self.assertFalse(beat["ok"])
        self.assertTrue(beat["single_agent_path"])

    def test_manual_gm_beat_exposes_npc_turn_tool_during_enemy_turn(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "silent",
                    "reason": "仅检查本次能力边界。",
                }
            ]
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.app.character_manager.add(
            Character(
                name="王城卫兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                traits=["enemy", "humanoid"],
            )
        )
        runtime.app.conflict_manager.start_scene("宫门冲突", ["王城卫兵"])

        status, _response = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("让当前敌方完成回合。"),
        )

        self.assertEqual(status, 200)
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["request_context"]["heartbeat_action"], "npc_turn")
        self.assertIn(
            "run_current_npc_turn",
            {tool["name"] for tool in request["available_tools"]},
        )

    def test_manual_gm_beat_reports_failure_and_rolls_back_incomplete_npc_fumble(
        self,
    ) -> None:
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "run_current_npc_turn",
                    "arguments": {
                        "expected_actor": "财团机兵",
                        "npc_action_type": "Attack",
                        "target": "伊莉雅",
                        "action_description": "机兵抬斧劈向伊莉雅。",
                        "scene_brief": "机兵挡在旅人与出口之间。",
                    },
                }
            ]
        )
        runtime = self.service._runtime("http-agent-test")
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
        runtime.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                weapon_damage=14,
                traits=["enemy", "construct"],
            )
        )
        runtime.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["财团机兵", "伊莉雅"],
        )
        runtime.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        runtime.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="财团机兵",
                attributes=["DEX", "MIG"],
                dice=[(8, 1), (10, 1)],
                total=2,
                modifier=0,
                high_roll=1,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=True,
                opportunity_count=1,
                margin=-8,
            )
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )

        status, response = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("让当前敌方完成回合。"),
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["ok"])
        self.assertEqual(
            response["agent_mode"],
            "gm_agent_message_transaction_rolled_back",
        )
        self.assertTrue(response["agent_error"])
        self.assertFalse(response["send_reply"])
        self.assertEqual(
            runtime.app.conflict_manager.state.current_actor(),
            "财团机兵",
        )
        self.assertFalse(
            runtime.app.interceptor.decision_window_manager.pending(
                kind="fumble_opportunity",
                owner="__gm__",
            )
        )

    def test_deferred_heartbeat_counts_only_after_delivery_confirmation(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "地图名字可以先放一放。你们想先聊哪一处最重要的地点？",
                    "reason": "第零章讨论已经停顿。",
                }
            ]
        )
        self.service.handle(
            "POST",
            "/v1/session/gate",
            {
                **self.payload(""),
                "status": "session_zero",
            },
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="地图名字还没想好。",
            role="user",
            channel_id="group-1",
        )
        heartbeat_payload = {
            **self.payload(""),
            "auto_respond": True,
            "defer_delivery_log": True,
            "cooldown_seconds": 0,
            "session_zero_idle_seconds": 0,
            "setup_nudge_followup_seconds": 0,
            "setup_nudge_limit": 1,
        }

        status, generated = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )
        self.assertEqual(status, 200)
        self.assertTrue(generated["send_reply"])
        self.assertTrue(generated["delivery_deferred"])
        self.assertEqual(generated["delivery_status"], "pending")
        self.assertTrue(generated["delivery_id"])
        self.assertEqual(len(client.calls), 1)
        transcript = runtime.log_manager.load_transcript(
            "http-agent-test",
            "s1",
        )
        self.assertEqual(
            [
                entry
                for entry in transcript
                if (entry.metadata or {}).get("mode")
                == "heartbeat_agent_session_zero_nudge"
            ],
            [],
        )

        _status, retried = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )
        self.assertTrue(retried["delivery_retry"])
        self.assertEqual(retried["delivery_id"], generated["delivery_id"])
        self.assertEqual(retried["reply"], generated["reply"])
        self.assertEqual(len(client.calls), 1)

        _status, delivered = self.service.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": generated["delivery_id"],
            },
        )
        self.assertTrue(delivered["ok"])
        transcript = runtime.log_manager.load_transcript(
            "http-agent-test",
            "s1",
        )
        heartbeat_entries = [
            entry
            for entry in transcript
            if (entry.metadata or {}).get("mode")
            == "heartbeat_agent_session_zero_nudge"
        ]
        self.assertEqual(len(heartbeat_entries), 1)
        self.assertTrue(
            heartbeat_entries[0].metadata["delivery_confirmed"]
        )

        _status, duplicate_ack = self.service.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": generated["delivery_id"],
            },
        )
        self.assertTrue(duplicate_ack["ok"])
        self.assertTrue(duplicate_ack["already_confirmed"])
        self.assertEqual(
            len(
                [
                    entry
                    for entry in runtime.log_manager.load_transcript(
                        "http-agent-test",
                        "s1",
                    )
                    if (entry.metadata or {}).get("mode")
                    == "heartbeat_agent_session_zero_nudge"
                ]
            ),
            1,
        )

    def test_committed_heartbeat_is_not_hidden_by_later_activity(self) -> None:
        class CommittedBeatAgent:
            def __init__(self, service: FUGMHttpService) -> None:
                self.service = service

            def run(self, *_args, **_kwargs):
                self.service.channel_activity_versions[
                    ("http-agent-test", "s1", "group-1")
                ] = 2
                return GMToolAgentOutcome(
                    handled=True,
                    target="fu_gm",
                    mode="gm_agent_tool",
                    reply="守卫已经放下闸门。",
                    stop_astrbot=True,
                    receipts=[
                        GMToolReceipt.success(
                            "commit_scene_response",
                            state_changed=True,
                            public_reply="守卫已经放下闸门。",
                            lock_public_reply=True,
                        )
                    ],
                )

        self.service.gm_tool_agent = CommittedBeatAgent(self.service)
        runtime = self.service._runtime("http-agent-test")
        gate = self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        self.service.channel_activity_versions[
            ("http-agent-test", "s1", "group-1")
        ] = 1

        result = self.service._session_heartbeat_via_agent(
            payload={
                **self.payload(""),
                "activity_version": 1,
                "defer_delivery_log": True,
            },
            runtime=runtime,
            gate=gate,
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            decision={
                "action": "free_scene_beat",
                "instruction": "",
                "reason": "测试提交后的竞态。",
            },
            heartbeat_entries=[],
            heartbeat_revision=(0, "", "", ""),
            heartbeat_is_stale=lambda: (
                self.service.channel_activity_versions[
                    ("http-agent-test", "s1", "group-1")
                ]
                != 1
            ),
            force=False,
            world_map={},
        )

        self.assertTrue(result["send_reply"])
        self.assertTrue(result["stale_after_commit"])
        self.assertTrue(result["delivery_id"])
        self.service._record_channel_activity_version(
            {"activity_version": 3},
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
        )
        self.assertIn(
            result["delivery_id"],
            self.service.pending_heartbeat_deliveries,
        )

    def test_new_player_message_cancels_multi_step_heartbeat_without_losing_player_write(
        self,
    ) -> None:
        class InterleavedClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.followup_entered = threading.Event()
                self.release_followup = threading.Event()
                self.lock = threading.Lock()

            @staticmethod
            def _window_id(messages: object) -> str:
                def visit(value: object) -> str:
                    if isinstance(value, dict):
                        required = value.get("required_followup_calls")
                        if isinstance(required, list):
                            for call in required:
                                if not isinstance(call, dict):
                                    continue
                                arguments = call.get("arguments")
                                if isinstance(arguments, dict) and arguments.get(
                                    "window_id"
                                ):
                                    return str(arguments["window_id"])
                        for nested in value.values():
                            found = visit(nested)
                            if found:
                                return found
                    elif isinstance(value, list):
                        for nested in value:
                            found = visit(nested)
                            if found:
                                return found
                    return ""

                for message in list(messages or []):
                    content = getattr(message, "content", "")
                    try:
                        parsed = json.loads(str(content or ""))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    found = visit(parsed)
                    if found:
                        return found
                return ""

            def create_chat_completion(self, **kwargs: object) -> str:
                with self.lock:
                    call_number = len(self.calls) + 1
                    self.calls.append(dict(kwargs))
                if call_number == 1:
                    return json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "run_current_npc_turn",
                            "arguments": {
                                "expected_actor": "财团机兵",
                                "npc_action_type": "Attack",
                                "target": "伊莉雅",
                                "action_description": "机兵抬斧劈向伊莉雅。",
                                "scene_brief": "机兵挡在旅人与出口之间。",
                            },
                        },
                        ensure_ascii=False,
                    )
                if call_number == 2:
                    self.followup_entered.set()
                    if not self.release_followup.wait(timeout=3):
                        raise AssertionError("心跳并发测试等待释放超时。")
                    window_id = self._window_id(kwargs.get("messages"))
                    if not window_id:
                        raise AssertionError("没有从工具回执中找到GM机会窗口。")
                    return json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "resolve_gm_opportunity",
                            "arguments": {
                                "window_id": window_id,
                                "choice": "情报",
                                "details": {
                                    "information": "机兵左膝的传动轴已经锈蚀。"
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                if call_number == 3:
                    return json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "discover_capabilities",
                            "arguments": {
                                "domains": ["table"],
                                "reason": "玩家正在声明安全界限。",
                            },
                        },
                        ensure_ascii=False,
                    )
                if call_number == 4:
                    return json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "record_safety_boundary",
                            "arguments": {
                                "kind": "line",
                                "content": "蜘蛛",
                            },
                        },
                        ensure_ascii=False,
                    )
                if call_number == 5:
                    return json.dumps(
                        {
                            "decision": "final",
                            "reply": "ok，已记录这条界限。",
                            "reason": "界限已经写入。",
                        },
                        ensure_ascii=False,
                    )
                raise AssertionError(f"意外的模型调用次数：{call_number}")

        runtime = self.service._runtime("http-agent-test")
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
        runtime.app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                defenses={"physical": 11, "magic": 8},
                weapon_damage=14,
                traits=["enemy", "construct"],
            )
        )
        runtime.app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            participants=["财团机兵", "伊莉雅"],
        )
        runtime.app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        runtime.app.interceptor.rules_engine.force_next_check_outcome(
            RollOutcome(
                actor="财团机兵",
                attributes=["DEX", "MIG"],
                dice=[(8, 1), (10, 1)],
                total=2,
                modifier=0,
                high_roll=1,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=True,
                opportunity_count=1,
                margin=-8,
            )
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        key = ("http-agent-test", "s1", "group-1")
        self.service.channel_activity_versions[key] = 1
        client = InterleavedClient()
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
            timeout_seconds=5,
        )
        heartbeat_payload = {
            **self.payload(""),
            "activity_version": 1,
            "auto_respond": True,
            "force": True,
            "npc_turn_grace_seconds": 0,
            "defer_delivery_log": True,
        }
        player_payload = {
            **self.payload(
                "界限：不要在游戏里出现蜘蛛。",
                message_id="m-player-after-heartbeat",
                addressed=True,
            ),
            "activity_version": 2,
        }

        with ThreadPoolExecutor(max_workers=2) as executor:
            heartbeat_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/session/heartbeat",
                heartbeat_payload,
            )
            self.assertTrue(client.followup_entered.wait(timeout=2))
            player_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                player_payload,
            )
            deadline = time.monotonic() + 2
            while (
                self.service.channel_activity_versions.get(key) != 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(self.service.channel_activity_versions.get(key), 2)
            client.release_followup.set()
            heartbeat_status, heartbeat = heartbeat_future.result(timeout=4)
            player_status, player = player_future.result(timeout=4)

        self.assertEqual(heartbeat_status, 200)
        self.assertFalse(heartbeat["send_reply"])
        self.assertTrue(heartbeat["stale_discarded"])
        self.assertEqual(player_status, 200)
        self.assertEqual(player["reply"], "ok，已记录这条界限。")
        self.assertIn(
            "蜘蛛",
            runtime.app.world_state.world_profile.safety_lines,
        )
        self.assertEqual(
            runtime.app.conflict_manager.state.current_actor(),
            "财团机兵",
        )
        self.assertFalse(
            runtime.app.interceptor.decision_window_manager.pending(
                kind="fumble_opportunity",
                owner="__gm__",
            )
        )

    def test_pending_heartbeat_delivery_survives_service_restart(self) -> None:
        delivery_id = self.service._stage_heartbeat_delivery(
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            reply="守卫已经落下闸门。",
            action="free_scene_beat",
            saved_path="",
            metadata={
                "mode": "heartbeat_agent_free_scene_beat",
                "tool_receipts": [
                    {
                        "tool_name": "commit_scene_response",
                        "ok": True,
                        "state_changed": True,
                    }
                ],
            },
            envelope={
                "envelope_id": "reply:heartbeat-restart",
                "campaign_id": "http-agent-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "target_event_id": "",
                "target_message_id": "",
                "target_speaker": "",
                "target_speaker_id": "",
                "text": "守卫已经落下闸门。",
                "created_at": "2026-07-30T00:00:00+00:00",
                "quote": False,
                "kind": "heartbeat:free_scene_beat",
                "metadata": {},
            },
        )

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        pending = restarted._pending_heartbeat_delivery(
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
        )
        self.assertIsNotNone(pending)
        self.assertEqual(pending["delivery_id"], delivery_id)

        status, delivered = restarted.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": delivery_id,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(delivered["ok"])

        restarted_again = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        _status, duplicate = restarted_again.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": delivery_id,
            },
        )
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["already_confirmed"])

    def test_failed_heartbeat_log_confirmation_keeps_pending_delivery(self) -> None:
        delivery_id = self.service._stage_heartbeat_delivery(
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            reply="守卫已经落下闸门。",
            action="free_scene_beat",
            saved_path="",
            metadata={"mode": "heartbeat_agent_free_scene_beat"},
            envelope={},
        )
        runtime = self.service._runtime("http-agent-test")
        with patch.object(
            runtime.log_manager,
            "append_message",
            side_effect=OSError("disk busy"),
        ):
            _status, failed = self.service.handle(
                "POST",
                "/v1/session/heartbeat/delivered",
                {
                    **self.payload(""),
                    "delivery_id": delivery_id,
                },
            )

        self.assertFalse(failed["ok"])
        self.assertTrue(failed["retryable"])
        self.assertIn(delivery_id, self.service.pending_heartbeat_deliveries)

        _status, delivered = self.service.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": delivery_id,
            },
        )
        self.assertTrue(delivered["ok"])
        self.assertNotIn(delivery_id, self.service.pending_heartbeat_deliveries)

    def test_heartbeat_confirmation_persistence_retry_does_not_duplicate_log(
        self,
    ) -> None:
        delivery_id = self.service._stage_heartbeat_delivery(
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            reply="守卫已经落下闸门。",
            action="free_scene_beat",
            saved_path="",
            metadata={"mode": "heartbeat_agent_free_scene_beat"},
            envelope={},
        )
        runtime = self.service._runtime("http-agent-test")

        def fail_persistence() -> bool:
            self.service.heartbeat_delivery_persistence_error = "disk busy"
            return False

        with patch.object(
            self.service,
            "_persist_heartbeat_delivery_state",
            side_effect=fail_persistence,
        ):
            _status, failed = self.service.handle(
                "POST",
                "/v1/session/heartbeat/delivered",
                {
                    **self.payload(""),
                    "delivery_id": delivery_id,
                },
            )

        self.assertFalse(failed["ok"])
        self.assertIn(delivery_id, self.service.pending_heartbeat_deliveries)

        _status, delivered = self.service.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                **self.payload(""),
                "delivery_id": delivery_id,
            },
        )
        self.assertTrue(delivered["ok"])
        entries = [
            entry
            for entry in runtime.log_manager.load_transcript(
                "http-agent-test",
                "s1",
            )
            if entry.message_id == f"heartbeat:{delivery_id}"
        ]
        self.assertEqual(len(entries), 1)

        _status, health = self.service.handle("GET", "/health", {})
        self.assertIn("heartbeat_delivery_queue", health["runtime"])
        self.assertEqual(
            health["runtime"]["heartbeat_delivery_queue"]["persistence_error"],
            "",
        )

    def test_session_zero_heartbeat_targets_less_contributing_player(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "南星，你愿意给这个世界添一个国家或城邦吗？",
                    "reason": "邀请贡献较少的玩家补一笔。",
                }
            ]
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="session_zero",
        )
        active = runtime.app.session_zero_manager.find_participant("阿凛")
        active.answered_topics.extend(
            ["kingdom_contributions", "historical_event_contributions"]
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="我已经补完索朗战争的来龙去脉。",
            role="user",
            channel_id="group-1",
        )

        status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "session_zero_idle_seconds": 0,
                "setup_nudge_limit": 1,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            response["session_zero_nudge_target"]["player"],
            "南星",
        )
        self.assertEqual(
            response["speech_intent"]["target_speaker"],
            "南星",
        )
        request = json.loads(client.calls[0]["messages"][1].content)
        target = request["request_context"]["heartbeat_session_zero_target"]
        self.assertEqual(target["player"], "南星")
        self.assertEqual(target["topic_label"], "王国、国家或政治共同体")

    def test_session_zero_heartbeat_stays_silent_while_player_is_thinking(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "不应生成这条提醒。",
                    "reason": "测试占位。",
                }
            ]
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
        runtime.app.session_zero_manager.pause_proactive_nudges(
            "阿凛",
            topic="第一幕开端",
            evidence="让我想想。",
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="session_zero",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="让我想想。",
            role="user",
            channel_id="group-1",
        )

        status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "session_zero_idle_seconds": 0,
                "setup_nudge_limit": 2,
            },
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["should_respond"])
        self.assertEqual(response["action"], "none")
        self.assertEqual(
            response["session_zero_nudge_target"]["status"],
            "player_requested_time",
        )
        self.assertEqual(client.calls, [])

    def test_unrelated_player_message_does_not_reset_same_topic_nudge_budget(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        runtime.app.initialize_session_zero(participants=["村夫", "loading"])
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="session_zero",
        )
        active = runtime.app.session_zero_manager.find_participant("村夫")
        active.answered_topics.extend(
            [
                "kingdom_contributions",
                "historical_event_contributions",
                "mystery_contributions",
                "threat_contributions",
            ]
        )
        quiet = runtime.app.session_zero_manager.find_participant("loading")
        quiet.answered_topics.extend(
            ["kingdom_contributions", "historical_event_contributions"]
        )
        for index in range(2):
            runtime.log_manager.append_message(
                "http-agent-test",
                "s1",
                speaker="时悠",
                content=f"第{index + 1}次询问loading的世界奥秘。",
                role="assistant",
                channel_id="group-1",
                metadata={
                    "mode": "heartbeat_agent_session_zero_nudge",
                    "delivery_confirmed": True,
                    "session_zero_nudge_target": {
                        "status": "targeted",
                        "player": "loading",
                        "topic": "mystery_contributions",
                        "topic_key": "mystery",
                        "topic_label": "世界奥秘",
                    },
                },
            )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="村夫",
            content="我去看一下诺艾尔的装备。",
            role="user",
            channel_id="group-1",
        )

        status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": False,
                "cooldown_seconds": 0,
                "session_zero_idle_seconds": 0,
                "setup_nudge_followup_seconds": 0,
                "setup_nudge_limit": 2,
            },
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["should_respond"])
        self.assertEqual(response["action"], "none")
        self.assertEqual(response["idle_episode"]["status"], "exhausted")

    def test_direct_reply_envelope_targets_the_triggering_message(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "这句我听见了。",
                    "reason": "被明确艾特。",
                }
            ]
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "@时悠，记得看这句。",
                message_id="qq-42",
                addressed=True,
            ),
        )

        envelope = response["reply_envelopes"][0]
        self.assertEqual(envelope["target_message_id"], "qq-42")
        self.assertEqual(envelope["target_speaker"], "阿凛")
        self.assertTrue(envelope["quote"])


if __name__ == "__main__":
    unittest.main()
