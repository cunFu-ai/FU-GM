from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fu_gm.conversation import MessageEvent
from fu_gm.config import LLMConfig
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
)
from fu_gm.gm_tool_agent import GMToolAgentOutcome, LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.http_server import FUGMHttpService, make_server
from fu_gm.llm_client_bundle import TestLLMClientBundle
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
        self.config = LLMConfig.for_test_client("test-model")
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
                discovery_decision = {
                        "decision": "call_tool",
                        "tool_name": GMCapabilityBroker.DISCOVERY_TOOL,
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试模型按协议取得所需能力。",
                        },
                        "reason": "先发现当前消息需要的能力。",
                    }
                return json.dumps(
                    discovery_decision,
                    ensure_ascii=False,
                )
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


class BlockingSummaryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("summary test client was not released")
        return json.dumps(
            {
                "public_evidence_entry_ids": [0],
                "private_evidence_entry_ids": [],
                "location_entry_ids": [],
                "reward_entry_ids": [],
                "unresolved_entry_ids": [],
            },
            ensure_ascii=False,
        )


class FUGMHttpRequestHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        service = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)
        self.server = make_server("127.0.0.1", 0, service=service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, payload, response_headers

    def test_post_requires_json_and_object_top_level(self) -> None:
        status, payload, _headers = self.request(
            "POST",
            "/v1/chat",
            body=b'{"message":"hello"}',
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertFalse(payload["ok"])

        status, payload, _headers = self.request(
            "POST",
            "/v1/chat",
            body=b"[]",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("顶层", payload["error"])

    def test_post_rejects_oversized_body_and_sets_security_headers(self) -> None:
        with patch.dict("os.environ", {"FU_GM_HTTP_MAX_BODY_BYTES": "1024"}):
            status, payload, headers = self.request(
                "POST",
                "/v1/chat",
                body=b"{" + (b"x" * 1024) + b"}",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 413)
        self.assertFalse(payload["ok"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_dashboard_uses_text_rows_for_untrusted_identifiers(self) -> None:
        status, page = self.server.RequestHandlerClass.service.handle("GET", "/gm")

        self.assertEqual(status, 200)
        self.assertIn('function rowText(title, body = "")', page)
        self.assertIn('rowText("战役", data.campaign_id)', page)
        self.assertIn('rowText("场次", data.session_id)', page)
        self.assertIn('rowText("最近保存", runtime.last_saved_path', page)


class FUGMHttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_missing_gate_recovers_from_active_session_zero_scene(self) -> None:
        runtime = self.service._runtime("recovered-session-zero", auto_load=False)
        runtime.app.initialize_session_zero(participants=["阿凛"])

        gate = self.service._effective_session_gate(
            runtime,
            "recovered-session-zero",
            "private-1",
            "solo",
        )

        self.assertEqual(gate.status, "session_zero")
        self.assertIn("恢复", gate.reason)
        restored = self.service.session_gates.get(
            "recovered-session-zero",
            "private-1",
            "solo",
        )
        self.assertEqual(restored.status, "session_zero")

    def test_explicitly_inactive_gate_is_not_reactivated_from_scene(self) -> None:
        runtime = self.service._runtime("ended-session-zero", auto_load=False)
        runtime.app.initialize_session_zero(participants=["阿凛"])
        self.service.session_gates.deactivate(
            "ended-session-zero",
            "private-1",
            "solo",
            reason="玩家明确收团",
        )

        gate = self.service._effective_session_gate(
            runtime,
            "ended-session-zero",
            "private-1",
            "solo",
        )

        self.assertEqual(gate.status, "inactive")
        self.assertEqual(gate.reason, "玩家明确收团")

    def install_agent(
        self,
        responses: list[dict[str, object] | str],
    ) -> ScriptedGMClient:
        client = ScriptedGMClient(responses)
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
            gm_personality_prompt=self.service.gm_style_prompt,
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

    @staticmethod
    def make_session_zero_adventure_ready(runtime) -> None:
        runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
        manager = runtime.app.session_zero_manager
        world = manager.state.world
        world.map_card = "自定义地图"
        world.magic_tech_role = "魔法与科技彼此对立。"
        world.kingdoms = {"索朗帝国": "旧蒸汽帝国。"}
        world.historical_events = ["两百年前的机械战争。"]
        world.mysteries = ["重叠日。"]
        world.world_threats = ["失控的钢铁生命正在扩散。"]
        world.group_concept = "调查重叠日的同行者"
        world.safety_lines = ["不出现性暴力"]
        world.selected_first_act_summary = "从卡里巴村监狱越狱。"
        for participant in manager.state.participants:
            participant.answered_topics.extend(
                [
                    "kingdom_contributions",
                    "historical_event_contributions",
                    "mystery_contributions",
                    "threat_contributions",
                ]
            )
        for player, hero in (("阿凛", "伊莉雅"), ("南星", "赛璃")):
            world.hero_drafts[player] = HeroDraft(
                player_name=player,
                hero_name=hero,
                identity="出逃的魔导工匠",
                theme="希望",
                origin="第七采掘城",
                classes={"造物使": 3, "武器大师": 2},
                attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
                skills={
                    "便携装置": 1,
                    "秘密配方": 1,
                    "先见之明": 1,
                    "碎骨": 1,
                    "破防打击": 1,
                },
                skill_options={"便携装置": ["魔导装置"]},
                equipment=["铁锤", "旅行装束"],
                confirmed=True,
            )
        manager.refresh_stage_from_state()

    def test_health_and_dashboard_are_available_without_loading_a_campaign(self) -> None:
        health_status, health = self.service.handle("GET", "/health", {})
        dashboard_status, dashboard = self.service.handle("GET", "/dashboard", {})

        self.assertEqual(health_status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "fu-gm")
        self.assertTrue(health["runtime"]["gm_persona"]["loaded"])
        self.assertIn("source", health["runtime"]["gm_persona"])
        self.assertEqual(
            health["runtime"]["gm_persona"]["core_agent_persona_scope"],
            "all_core_decisions",
        )
        self.assertTrue(
            health["runtime"]["gm_persona"]["ordinary_core_agent_receives_persona"]
        )
        self.assertEqual(health["runtime"]["public_expression_mode"], "core")
        self.assertIn("core_gm_provider", health["runtime"])
        self.assertIsInstance(health["runtime"]["core_gm_provider"], dict)
        self.assertEqual(dashboard_status, 200)
        self.assertIsInstance(dashboard, str)
        self.assertIn("FU-GM", dashboard)
        self.assertIn("审计面板快速跳转", dashboard)
        self.assertIn('id="providerStatus"', dashboard)
        self.assertIn('id="liveRuns"', dashboard)
        self.assertIn("模型供应商状态", dashboard)
        self.assertIn("实时执行观察器", dashboard)
        self.assertIn("/v1/audit/live-runs", dashboard)
        self.assertIn("setInterval(refreshLiveRuns, 750)", dashboard)
        self.assertIn("供应商尚未返回文本", dashboard)
        self.assertIn("usage 未上报，命中率未知", dashboard)
        self.assertIn("已知未命中", dashboard)
        self.assertIn("prompt/cached/miss", dashboard)
        self.assertIn("if (liveActiveCount > 0) return;", dashboard)
        self.assertIn('${esc(text)}</div></div>`', dashboard)
        self.assertIn("模型不可用，GM 当前无法生成回复", dashboard)
        self.assertIn("玩家角色卡", dashboard)
        self.assertIn("物语点", dashboard)
        self.assertIn("物资点", dashboard)
        self.assertIn("尚未转化的角色草稿", dashboard)
        self.assertIn("当前地图", dashboard)
        self.assertLess(
            dashboard.index('id="providerStatus"'),
            dashboard.index('id="liveRuns"'),
        )
        self.assertLess(
            dashboard.index('id="liveRuns"'),
            dashboard.index('id="mapArtifacts"'),
        )
        self.assertLess(
            dashboard.index('id="mapArtifacts"'),
            dashboard.index('id="gmTools"'),
        )
        self.assertLess(
            dashboard.index('id="characters"'),
            dashboard.index('id="gmTools"'),
        )

    def test_health_cache_usage_status_distinguishes_unknown_from_known_miss(
        self,
    ) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.known = False

            def telemetry_payload(self):
                return {
                    "total_calls": 1,
                    "failed_calls": 0,
                    "prompt_cache": {
                        "usage_status": "reported" if self.known else "unknown",
                        "usage_reported_calls": 1 if self.known else 0,
                        "unknown_calls": 0 if self.known else 1,
                        "hit_calls": 0,
                        "known_miss_calls": 1 if self.known else 0,
                        "prompt_tokens": 800 if self.known else 0,
                        "cached_tokens": 0,
                        "cache_miss_tokens": 800 if self.known else 0,
                        "by_operation": [],
                    },
                }

        provider = FakeProvider()
        object.__setattr__(
            self.service.gm_agent_runtime,
            "llm_client",
            provider,
        )

        _status, health = self.service.handle("GET", "/health", {})
        cache = health["runtime"]["core_gm_provider"]["prompt_cache"]
        self.assertEqual(cache["usage_status"], "unknown")
        self.assertEqual(cache["unknown_calls"], 1)
        self.assertEqual(cache["known_miss_calls"], 0)

        provider.known = True
        _status, health = self.service.handle("GET", "/health", {})
        cache = health["runtime"]["core_gm_provider"]["prompt_cache"]
        self.assertEqual(cache["usage_status"], "reported")
        self.assertEqual(cache["unknown_calls"], 0)
        self.assertEqual(cache["known_miss_calls"], 1)
        self.assertEqual(cache["cache_miss_tokens"], 800)

    def test_live_runs_endpoint_filters_scope_and_private_diagnostics(self) -> None:
        run_id = self.service.gm_live_run_monitor.start_run(
            campaign_id="实时团",
            session_id="s1",
            channel_id="group-1",
            conversation_turn_id="turn-1",
            message_id="message-1",
            speaker="阿凛<script>",
            model="fake-model",
            timeout_seconds=120,
            max_iterations=8,
            message="调查钟楼<script>alert(1)</script>",
        )
        self.service.gm_live_run_monitor.event(
            run_id,
            kind="model_output",
            phase="validating_model_output",
            iteration=1,
            summary="模型已返回完整正文。",
            public_details={"output_chars": 28},
            private_details={
                "raw_output": '<script>alert("raw")</script>',
                "parsed_decision": {"decision": "call_tool"},
                "tool_arguments": {"secret": "隐秘参数"},
                "receipt": {"ok": True, "message": "权威回执"},
            },
        )
        self.service.gm_live_run_monitor.start_run(
            campaign_id="别的团",
            session_id="s1",
            channel_id="group-1",
            message_id="other",
        )

        public_status, public = self.service.handle(
            "GET",
            "/v1/audit/live-runs?campaign_id=实时团&session_id=s1"
            "&channel_id=group-1&include_private=false&limit=1",
        )
        private_status, private = self.service.handle(
            "GET",
            "/v1/audit/live-runs?campaign_id=实时团&session_id=s1"
            "&channel_id=group-1&include_private=true&limit=1",
        )

        self.assertEqual(public_status, 200)
        self.assertEqual(private_status, 200)
        self.assertEqual(public["active_count"], 1)
        self.assertEqual(public["active_runs"][0]["run_id"], run_id)
        self.assertNotIn("raw_output", str(public))
        self.assertNotIn("message_id", public["active_runs"][0])
        private_run = private["active_runs"][0]
        self.assertEqual(private_run["message_id"], "message-1")
        self.assertEqual(private_run["speaker"], "阿凛<script>")
        private_event = private_run["events"][-1]
        self.assertEqual(
            private_event["details"]["raw_output"],
            '<script>alert("raw")</script>',
        )
        self.assertEqual(
            private_event["details"]["parsed_decision"],
            {"decision": "call_tool"},
        )
        self.assertEqual(
            private_event["details"]["tool_arguments"],
            {"secret": "隐秘参数"},
        )
        self.assertEqual(
            private_event["details"]["receipt"],
            {"ok": True, "message": "权威回执"},
        )
        self.assertFalse(private["streaming"])
        self.assertIn("非流式", private["streaming_note"])

    def test_live_runs_endpoint_limits_completed_history(self) -> None:
        completed_ids: list[str] = []
        for index in range(3):
            run_id = self.service.gm_live_run_monitor.start_run(
                campaign_id="历史团",
                session_id="s1",
                channel_id="group-1",
                message_id=f"message-{index}",
            )
            completed_ids.append(run_id)
            self.service.gm_live_run_monitor.finish_run(
                run_id,
                terminal_reason="completed",
            )

        status, result = self.service.handle(
            "GET",
            "/v1/audit/live-runs?campaign_id=历史团&limit=2",
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["active_count"], 0)
        self.assertEqual(
            [item["run_id"] for item in result["recent_runs"]],
            [completed_ids[2], completed_ids[1]],
        )

    def test_live_runs_endpoint_does_not_wait_for_campaign_transaction_lock(self) -> None:
        runtime = self.service._runtime("锁内实时团", auto_load=False)
        run_id = self.service.gm_live_run_monitor.start_run(
            campaign_id="锁内实时团",
            session_id="s1",
            channel_id="group-1",
            timeout_seconds=120,
        )
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_campaign_lock() -> None:
            with runtime.transaction_lock:
                lock_acquired.set()
                release_lock.wait(timeout=2)

        worker = threading.Thread(target=hold_campaign_lock)
        worker.start()
        self.assertTrue(lock_acquired.wait(timeout=1))
        started = time.monotonic()
        try:
            status, result = self.service.handle(
                "GET",
                "/v1/audit/live-runs?campaign_id=锁内实时团&session_id=s1",
            )
        finally:
            release_lock.set()
            worker.join(timeout=2)

        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(result["active_runs"][0]["run_id"], run_id)

    def test_new_activity_marks_matching_live_run_superseded(self) -> None:
        run_id = self.service.gm_live_run_monitor.start_run(
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            message_id="old-message",
            timeout_seconds=120,
        )
        self.service._record_channel_activity_version(
            {"activity_version": 1, "message_id": "old-message"},
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
        )

        status, activity = self.service.handle(
            "POST",
            "/v1/message/activity",
            {
                **self.payload("后续消息", message_id="new-message"),
                "activity_version": 2,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(activity["tracked"])
        active = self.service.gm_live_run_monitor.snapshot(
            campaign_id="http-agent-test",
            include_private=True,
        )["active_runs"][0]
        self.assertEqual(active["run_id"], run_id)
        self.assertTrue(active["superseded"])
        self.assertEqual(active["superseded_by"], "new-message")
        self.assertEqual(active["health"], "superseded")

    def test_dashboard_marks_materialized_draft_and_exposes_full_pc_resources(self) -> None:
        runtime = self.service._runtime("角色卡审计团", auto_load=False)
        runtime.app.character_manager.add(
            Character(
                name="诺艾尔",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=41,
                max_mp=35,
                mp=30,
                crisis_threshold=22,
                inventory_points=7,
                max_inventory_points=8,
                fabula_points=3,
                experience_points=4,
                zenit=120,
                initiative=-1,
                identity="离家出走的秘宝猎人",
                theme="野心",
                origin="托伦",
                classes={"旅人": 2, "武器大师": 3},
                skills={"宝物猎人": 1, "碎骨": 2},
                traits=["pc"],
            )
        )
        runtime.app.world_state.world_profile.hero_drafts["村夫"] = HeroDraft(
            player_name="村夫",
            hero_name="诺艾尔",
            confirmed=True,
        )

        status, dashboard = self.service.handle(
            "GET",
            "/v1/audit/dashboard?campaign_id=角色卡审计团&session_id=s1",
        )

        self.assertEqual(status, 200)
        self.assertTrue(dashboard["setup"]["hero_drafts"]["村夫"]["materialized"])
        character = dashboard["characters"][0]
        self.assertEqual(character["fabula_points"], 3)
        self.assertEqual(character["inventory_points"], 7)
        self.assertEqual(character["max_inventory_points"], 8)
        self.assertEqual(character["experience_points"], 4)
        self.assertEqual(character["zenit"], 120)
        self.assertEqual(character["initiative"], -1)

    def test_character_audit_includes_dynamic_npc_defense_bonus(self) -> None:
        runtime = self.service._runtime("守卫审计团", auto_load=False)
        for name in ("北门卫", "南门卫"):
            runtime.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    defenses={"physical": 11, "magic": 8},
                    traits=["enemy"],
                    npc_source_template="守卫",
                    npc_ability_profiles=ability_profiles_for_bestiary("守卫"),
                )
            )
        runtime.app.character_manager.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                traits=["pc"],
            )
        )
        runtime.app.conflict_manager.start_scene(
            "城门",
            ["瓦莉亚", "北门卫", "南门卫"],
            player_side=["瓦莉亚"],
            enemy_side=["北门卫", "南门卫"],
        )

        payload = self.service._character_audit_payload(runtime.app, "北门卫")

        self.assertEqual(payload["defenses"], {"physical": 12, "magic": 9})

    def test_explicitly_loading_default_overrides_a_newer_active_campaign(self) -> None:
        self.service._save_campaign({"campaign_id": "default"})
        self.service._save_campaign({"campaign_id": "旧测试团"})
        self.service.session_gates.activate(
            "旧测试团",
            "test-channel",
            "test-session",
            status="adventure",
        )

        status, result = self.service._load_campaign(
            {"campaign_id": "default"}
        )

        self.assertEqual(status, 200, result)
        self.assertEqual(self.service.current_campaign_id, "default")
        self.assertEqual(self.service._current_campaign_id(), "default")
        self.assertEqual(
            self.service._current_campaign_payload()["campaign_id"],
            "default",
        )

    def test_delete_campaign_purges_external_gate_and_heartbeat_state(self) -> None:
        campaign_id = "待删除团"
        runtime = self.service._runtime(campaign_id, auto_load=False)
        runtime.app.world_state.world_profile.campaign_title = campaign_id
        self.service._save_campaign({"campaign_id": campaign_id})
        self.service.session_gates.activate(
            campaign_id,
            "group-1",
            "s1",
            status="adventure",
        )
        self.service.pending_heartbeat_deliveries["pending-old"] = {
            "campaign_id": campaign_id,
            "session_id": "s1",
            "channel_id": "group-1",
        }
        self.service.confirmed_heartbeat_deliveries["confirmed-old"] = {
            "campaign_id": campaign_id,
        }
        self.service.channel_activity_versions[(campaign_id, "s1", "group-1")] = 3
        self.service.channel_activity_tokens[(campaign_id, "s1", "group-1")] = {
            "bridge:test:3": 3
        }
        self.assertTrue(self.service._persist_heartbeat_delivery_state())

        status, result = self.service._delete_campaign(
            {
                "campaign_id": campaign_id,
                "delete_all": True,
                "confirm": "确认删除",
            }
        )
        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )

        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted_campaign_id"], campaign_id)
        self.assertFalse(Path(self.tempdir.name, campaign_id).exists())
        self.assertEqual(
            restarted.session_gates.get(campaign_id, "group-1", "s1").status,
            "inactive",
        )
        self.assertFalse(restarted.pending_heartbeat_deliveries)
        self.assertFalse(self.service.channel_activity_tokens)
        self.assertFalse(restarted.confirmed_heartbeat_deliveries)
        self.assertNotEqual(restarted._current_campaign_id(), campaign_id)

    def test_natural_language_delete_does_not_resurrect_deleted_directory(self) -> None:
        campaign_id = "http-agent-test"
        self.service._save_campaign({"campaign_id": campaign_id})
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "discover_capabilities",
                    "arguments": {
                        "domains": ["campaign"],
                        "reason": "玩家明确要求删除当前战役。",
                    },
                    "reply": "",
                    "reason": "先取得战役管理能力。",
                },
                {
                    "decision": "call_tool",
                    "tool_name": "delete_save",
                    "arguments": {
                        "scope": "campaign",
                        "campaign_id": campaign_id,
                    },
                    "reply": "",
                    "reason": "执行玩家确认的整团删除。",
                },
                {
                    "decision": "final",
                    "reply": "《http-agent-test》已经删除。",
                    "reason": "删除完成。",
                },
            ]
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "@时悠，确认删除整个战役 http-agent-test。",
                message_id="delete-campaign-1",
                addressed=True,
            ),
        )

        self.assertEqual(status, 200, response)
        self.assertEqual(response["deleted_campaign_id"], campaign_id)
        self.assertEqual(response["active_campaign_id"], "default")
        self.assertEqual(response["reply_envelopes"][0]["campaign_id"], "default")
        self.assertFalse(Path(self.tempdir.name, campaign_id).exists())

    def test_unbound_private_message_never_uses_another_groups_current_campaign(
        self,
    ) -> None:
        self.service._save_campaign({"campaign_id": "正在跑的别团"})
        self.service._mark_current_campaign("正在跑的别团")

        resolved = self.service._resolve_private_campaign_id(
            "default",
            {"is_private": True, "anonymous": True},
        )

        self.assertEqual(resolved, "default")

    def test_explicit_private_campaign_binding_is_preserved(self) -> None:
        resolved = self.service._resolve_private_campaign_id(
            "宁姆格福",
            {"is_private": True, "anonymous": True},
        )

        self.assertEqual(resolved, "宁姆格福")

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

    def test_reply_delivery_confirmation_is_persisted_and_idempotent(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "平台送达确认测试。",
                    "reason": "直接回复。",
                }
            ]
        )
        payload = self.payload("@时悠，确认送达。", addressed=True)
        status, response = self.service.handle("POST", "/v1/message/route", payload)
        envelope_id = response["reply_envelopes"][0]["envelope_id"]

        confirm_status, confirmed = self.service.handle(
            "POST",
            "/v1/message/delivered",
            {
                "envelope_id": envelope_id,
                "campaign_id": "http-agent-test",
                "platform": "astrbot",
            },
        )
        repeated_status, repeated = self.service.handle(
            "POST",
            "/v1/message/delivered",
            {
                "envelope_id": envelope_id,
                "campaign_id": "http-agent-test",
                "platform": "astrbot",
            },
        )
        restarted = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)
        replay_status, replay = restarted.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertEqual(confirm_status, 200)
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["delivery_status"], "delivered")
        self.assertEqual(repeated_status, 200)
        self.assertTrue(repeated["already_confirmed"])
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["deduplicated"])
        self.assertTrue(replay["delivery_confirmed"])

    def test_reply_delivery_confirmation_can_locate_envelope_after_restart(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "重启后确认。",
                    "reason": "直接回复。",
                }
            ]
        )
        _status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload("@时悠，稍后确认。", addressed=True),
        )
        envelope_id = response["reply_envelopes"][0]["envelope_id"]
        restarted = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)

        status, confirmed = restarted.handle(
            "POST",
            "/v1/message/delivered",
            {"envelope_id": envelope_id, "platform": "astrbot"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["campaign_id"], "http-agent-test")

    def test_reply_delivery_confirmation_rejects_unknown_envelope(self) -> None:
        status, response = self.service.handle(
            "POST",
            "/v1/message/delivered",
            {
                "envelope_id": "reply:missing",
                "campaign_id": "http-agent-test",
                "platform": "astrbot",
            },
        )

        self.assertEqual(status, 200)
        self.assertFalse(response["ok"])
        self.assertIn("未找到", response["error"])

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
                    "message_kind": "discussion",
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
                    "decision": "final",
                    "message_kind": "mixed",
                    "audience": "table",
                    "reply": "伊莉雅在门边看见两道尚未靠近驿站的灯影。",
                    "reason": "白河提出建议，随后阿凛明确执行了观察。",
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
        self.assertEqual(response["target"], "fu_gm")
        self.assertEqual(len(client.calls), 1)
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["session"]["speaker"], "阿凛")
        self.assertEqual(request["current_message"], "伊莉雅走到门边观察外面的灯影。")
        self.assertEqual(request["current_turn"]["message_count"], 2)
        self.assertEqual(
            [item["speaker"] for item in request["current_turn"]["events"]],
            ["白河", "阿凛"],
        )
        self.assertEqual(len(response["batch_event_ids"]), 2)
        self.assertIn("single_semantic_turn", response["decision"]["tags"])
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

    def test_channel_activity_registration_is_idempotent_for_group_and_private(self) -> None:
        base = {
            **self.payload("新消息", message_id="activity-1"),
            "activity_version": 1,
            "activity_token": "bridge-a:group-1:1",
            "is_private": False,
        }

        first_status, first = self.service.handle(
            "POST",
            "/v1/message/activity",
            base,
        )
        retry_status, retry = self.service.handle(
            "POST",
            "/v1/message/activity",
            {
                **base,
                "activity_token": "bridge-reloaded:group-1:1",
            },
        )
        second_status, second = self.service.handle(
            "POST",
            "/v1/message/activity",
            {
                **base,
                "message_id": "activity-2",
                "activity_version": 2,
                "activity_token": "bridge-a:group-1:2",
            },
        )
        private_status, private = self.service.handle(
            "POST",
            "/v1/message/activity",
            {
                **base,
                "channel_id": "private:user-1",
                "activity_token": "bridge-a:private:1",
                "is_private": True,
            },
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(retry_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(private_status, 200)
        self.assertEqual(first["activity_version"], retry["activity_version"])
        self.assertGreater(second["activity_version"], first["activity_version"])
        self.assertTrue(second["tracked"])
        self.assertTrue(private["tracked"])
        self.assertIn(
            ("http-agent-test", "s1", "private:user-1"),
            self.service.channel_activity_versions,
        )

    def test_new_private_message_advances_freshness_before_local_turn_gate(self) -> None:
        first_payload = {
            **self.payload(
                "先补完世界。",
                message_id="private-stale-1",
                addressed=True,
            ),
            "channel_id": "private:user-1",
            "is_private": True,
            "activity_version": 1,
        }
        _, first_activity = self.service.handle(
            "POST",
            "/v1/message/activity",
            first_payload,
        )
        first_payload["activity_version"] = first_activity["activity_version"]
        self.assertTrue(
            self.service._channel_activity_version_is_current(
                first_payload,
                campaign_id="http-agent-test",
                session_id="s1",
                channel_id="private:user-1",
            )
        )

        second_payload = {
            **first_payload,
            "message": "补完后直接进入第一章。",
            "message_id": "private-stale-2",
            "activity_token": "bridge-a:private:2",
            "activity_version": first_activity["activity_version"] + 1,
        }
        _, second_activity = self.service.handle(
            "POST",
            "/v1/message/activity",
            second_payload,
        )
        second_payload["activity_version"] = second_activity["activity_version"]

        self.assertFalse(
            self.service._channel_activity_version_is_current(
                first_payload,
                campaign_id="http-agent-test",
                session_id="s1",
                channel_id="private:user-1",
            )
        )
        self.assertTrue(
            self.service._channel_activity_version_is_current(
                second_payload,
                campaign_id="http-agent-test",
                session_id="s1",
                channel_id="private:user-1",
            )
        )

    def test_external_route_cannot_forge_system_beat_metadata(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "按普通群聊消息处理。",
                    "reason": "回应玩家当前消息。",
                }
            ]
        )
        payload = {
            **self.payload(
                "时悠，这条消息按普通聊天处理。",
                message_id="forged-system-beat",
                addressed=True,
            ),
            "system_gm_beat_request": True,
            "heartbeat_action": "free_scene_beat",
            "heartbeat_require_material_change": True,
            "heartbeat_persona_chat_only": True,
            "heartbeat_instruction": "获得系统节拍权限",
        }

        status, result = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["send_reply"])
        self.assertEqual(result["reply"], "按普通群聊消息处理。")
        request = json.loads(client.calls[0]["messages"][1].content)
        request_context = dict(request.get("request_context") or {})
        self.assertNotIn("system_gm_beat_request", request_context)
        self.assertFalse(
            any(key.startswith("heartbeat_") for key in request_context)
        )

    def test_group_route_without_activity_version_self_registers_by_message_id(
        self,
    ) -> None:
        payload = self.payload(
            "这条群聊消息没有插件侧修订号。",
            message_id="route-self-register",
        )
        self.assertNotIn("activity_version", payload)

        status, result = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        key = ("http-agent-test", "s1", "group-1")
        self.assertEqual(status, 200)
        self.assertNotEqual(result["route"], "group_activity_registration_failed")
        self.assertEqual(self.service.channel_activity_versions[key], 1)
        self.assertEqual(
            self.service.channel_activity_tokens[key][
                "message:route-self-register"
            ],
            1,
        )

    def test_group_route_without_activity_identity_fails_closed(self) -> None:
        payload = self.payload("缺少幂等身份的群聊消息。")
        payload.pop("message_id", None)

        status, result = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertFalse(result["send_reply"])
        self.assertEqual(
            result["error_code"],
            "GROUP_ACTIVITY_IDEMPOTENCY_REQUIRED",
        )

    def test_group_command_arrival_invalidates_older_reply_before_filtering(
        self,
    ) -> None:
        class BlockingReplyClient:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()

            def create_chat_completion(self, **_kwargs: object) -> str:
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("等待新的群聊命令抵达超时。")
                return json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "这条旧回答不应再发送。",
                        "reason": "回答旧消息。",
                    },
                    ensure_ascii=False,
                )

        client = BlockingReplyClient()
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
            timeout_seconds=5,
        )
        older = self.payload(
            "时悠，刚才的安排可行吗？",
            message_id="old-before-command",
            addressed=True,
        )
        command = self.payload(
            "/fugm_save",
            message_id="new-command-arrival",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            older_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                older,
            )
            self.assertTrue(client.entered.wait(timeout=1))
            command_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                command,
            )
            key = ("http-agent-test", "s1", "group-1")
            deadline = time.monotonic() + 1
            while (
                self.service.channel_activity_versions.get(key, 0) < 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(self.service.channel_activity_versions[key], 2)
            client.release.set()
            old_status, old_result = older_future.result(timeout=3)
            command_status, command_result = command_future.result(timeout=3)

        self.assertEqual(old_status, 200)
        self.assertTrue(old_result["stale_discarded"])
        self.assertFalse(old_result["send_reply"])
        self.assertEqual(command_status, 200)
        self.assertEqual(command_result["route"], "command_protocol_required")

    def test_private_route_does_not_use_group_activity_freshness_guard(self) -> None:
        self.service.channel_activity_versions[
            ("http-agent-test", "s1", "group-1")
        ] = 99
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "私聊已收到。",
                    "reason": "回应当前私聊。",
                }
            ]
        )
        payload = {
            **self.payload(
                "时悠，私下确认一下。",
                message_id="private-freshness-1",
                addressed=True,
            ),
            "is_private": True,
            "activity_version": 1,
        }

        status, result = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["send_reply"])
        self.assertEqual(result["reply"], "私聊已收到。")
        self.assertFalse(result["stale_discarded"])

    def test_new_group_message_invalidates_inflight_write_before_tool_commit(
        self,
    ) -> None:
        class BlockingSafetyClient:
            def __init__(self) -> None:
                self.calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            @staticmethod
            def _discovery_decision() -> str:
                return json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "state_contribution",
                        "audience": "gm",
                        "tool_name": "discover_capabilities",
                        "arguments": {
                            "domains": ["table"],
                            "reason": "玩家正在声明安全界限。",
                        },
                        "reason": "先取得安全界限写入能力。",
                    },
                    ensure_ascii=False,
                )

            @staticmethod
            def _tool_decision(content: str) -> str:
                return json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "state_contribution",
                        "audience": "gm",
                        "tool_name": "record_safety_boundary",
                        "arguments": {
                            "kind": "line",
                            "content": content,
                        },
                        "reason": "按玩家当前声明登记安全界限。",
                    },
                    ensure_ascii=False,
                )

            def create_chat_completion(self, **_kwargs: object) -> str:
                self.calls += 1
                if self.calls == 1:
                    return self._discovery_decision()
                if self.calls == 2:
                    self.entered.set()
                    if not self.release.wait(timeout=2):
                        raise AssertionError("等待新群聊消息超时。")
                    return self._tool_decision("蜘蛛")
                if self.calls == 3:
                    return self._discovery_decision()
                if self.calls == 4:
                    return self._tool_decision("蜈蚣")
                if self.calls == 5:
                    return json.dumps(
                        {
                            "decision": "final",
                            "reply": "ok，已记录这条界限。",
                            "reason": "新的群聊消息已经完成登记。",
                        },
                        ensure_ascii=False,
                    )
                raise AssertionError(f"意外的模型调用次数：{self.calls}")

        client = BlockingSafetyClient()
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
            timeout_seconds=5,
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        first_payload = {
            **self.payload(
                "界限：不要出现蜘蛛。",
                message_id="stale-group-write-1",
                addressed=True,
            ),
            "activity_version": 1,
            "activity_token": "bridge-a:group-1:1",
        }
        _, first_activity = self.service.handle(
            "POST",
            "/v1/message/activity",
            first_payload,
        )
        first_payload["activity_version"] = first_activity["activity_version"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                first_payload,
            )
            self.assertTrue(client.entered.wait(timeout=1))
            second_payload = {
                **self.payload(
                    "更正：界限是不要出现蜈蚣。",
                    message_id="stale-group-write-2",
                    addressed=True,
                ),
                "activity_version": 2,
                "activity_token": "bridge-a:group-1:2",
            }
            _, second_activity = self.service.handle(
                "POST",
                "/v1/message/activity",
                second_payload,
            )
            second_payload["activity_version"] = second_activity[
                "activity_version"
            ]
            client.release.set()
            first_status, first = first_future.result(timeout=3)

        runtime = self.service._runtime("http-agent-test")
        self.assertEqual(first_status, 200)
        self.assertFalse(first["send_reply"])
        self.assertTrue(first["stale_discarded"])
        self.assertEqual(first["route"], "gm_agent_stale")
        self.assertEqual(
            first["tool_receipts"][-1]["error_code"],
            "STALE_AGENT_REQUEST",
        )
        self.assertNotIn("蜘蛛", runtime.app.world_state.world_profile.safety_lines)

        second_status, second = self.service.handle(
            "POST",
            "/v1/message/route",
            second_payload,
        )

        self.assertEqual(second_status, 200)
        self.assertTrue(second["send_reply"])
        self.assertIn("蜈蚣", runtime.app.world_state.world_profile.safety_lines)
        self.assertNotIn("蜘蛛", runtime.app.world_state.world_profile.safety_lines)

    def test_new_group_message_suppresses_uncommitted_stale_reply(self) -> None:
        class BlockingReplyClient:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()

            def create_chat_completion(self, **_kwargs: object) -> str:
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("等待新群聊消息超时。")
                return json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "这是一条已经过期的回答。",
                        "reason": "回答旧问题。",
                    },
                    ensure_ascii=False,
                )

        client = BlockingReplyClient()
        self.service.gm_tool_agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=self.service.gm_tool_registry,
            timeout_seconds=5,
        )
        first_payload = {
            **self.payload(
                "时悠，按刚才的方案处理吗？",
                message_id="stale-group-reply-1",
                addressed=True,
            ),
            "activity_token": "bridge-b:group-1:1",
            "activity_version": 1,
        }
        _, first_activity = self.service.handle(
            "POST",
            "/v1/message/activity",
            first_payload,
        )
        first_payload["activity_version"] = first_activity["activity_version"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            route_future = executor.submit(
                self.service.handle,
                "POST",
                "/v1/message/route",
                first_payload,
            )
            self.assertTrue(client.entered.wait(timeout=1))
            self.service.handle(
                "POST",
                "/v1/message/activity",
                {
                    **self.payload(
                        "等等，我换个方案。",
                        message_id="stale-group-reply-2",
                    ),
                    "activity_token": "bridge-b:group-1:2",
                    "activity_version": 2,
                },
            )
            client.release.set()
            status, result = route_future.result(timeout=3)

        transcript = self.service._runtime(
            "http-agent-test"
        ).log_manager.load_transcript("http-agent-test", "s1")
        self.assertEqual(status, 200)
        self.assertTrue(result["stale_discarded"])
        self.assertFalse(result["send_reply"])
        self.assertNotIn(
            "这是一条已经过期的回答。",
            [entry.content for entry in transcript],
        )

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
                        "message_kind": "gm_request",
                        "audience": "gm",
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
        self.assertEqual(second["summary"], first["summary"])
        self.assertEqual(second["experience"], first["experience"])
        self.assertEqual(
            second["episode_progress"],
            first["episode_progress"],
        )
        self.assertEqual(
            len(runtime.app.story_arc_manager.state.session_feedback_history),
            feedback_count,
        )

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        third_status, third = restarted.handle(
            "POST",
            "/v1/session/end",
            payload,
        )
        self.assertEqual(third_status, 200)
        self.assertTrue(third["already_ended"])
        self.assertEqual(third["summary"], first["summary"])
        self.assertEqual(third["experience"], first["experience"])

    def test_session_end_response_does_not_wait_for_llm_summary(self) -> None:
        summary_client = BlockingSummaryClient()
        idle_client = ScriptedGMClient([])
        bundle = TestLLMClientBundle(
            core=idle_client,
            expressor=idle_client,
            npc_design=idle_client,
            pacing=idle_client,
            summarizer=summary_client,
            player=idle_client,
            model="test-model",
        )
        service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=True,
            test_llm_bundle=bundle,
        )
        runtime = service._runtime("async-summary-end-test")
        service.session_gates.activate(
            "async-summary-end-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.app.start_session_tracking("s1")
        runtime.log_manager.append_message(
            "async-summary-end-test",
            "s1",
            speaker="阿凛",
            content="我关上风铃廊的门。",
        )
        payload = {
            "campaign_id": "async-summary-end-test",
            "session_id": "s1",
            "channel_id": "group-1",
        }

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(
                    service.handle,
                    "POST",
                    "/v1/session/end",
                    payload,
                )
                status, response = request.result(timeout=1)

            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertEqual(
                response["summary"]["generation_method"],
                "heuristic_sync",
            )
            self.assertFalse(
                response["summary_generation"]["llm_waited_on_critical_path"]
            )
            self.assertTrue(response["summary_enrichment"]["queued"])
            self.assertTrue(
                response["summary_enrichment"]["source_snapshot_version"]
            )
            self.assertTrue(summary_client.started.wait(timeout=1))

            summary_client.release.set()
            enriched = runtime.log_manager.wait_for_summary_enrichment(
                "async-summary-end-test",
                "s1",
                timeout=2,
            )
            self.assertEqual(enriched["status"], "succeeded")
            self.assertEqual(len(summary_client.calls), 1)
        finally:
            summary_client.release.set()
            runtime.log_manager.shutdown_summary_enrichment(wait=True)

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

    def test_session_gate_exposes_pending_player_response_to_chat_bridge(self) -> None:
        runtime = self.service._runtime("pending-response-gate-test")
        runtime.app.interceptor.decision_window_manager.create(
            kind="initiative_support",
            owner="赛璃",
            prompt="要支援团队先攻吗？",
            options=[
                {"choice": "support", "label": "支援"},
                {"choice": "skip", "label": "跳过"},
            ],
            blocking=True,
            allowed_responders=["赛璃"],
        )

        status, response = self.service.handle(
            "GET",
            "/v1/session/gate?campaign_id=pending-response-gate-test&session_id=s1&channel_id=group-1",
            None,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["awaiting_player_response"])
        self.assertEqual(response["awaiting_player_response_count"], 1)

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

    def test_http_material_gate_rejects_private_state_receipt(self) -> None:
        private_receipt = {
            "tool_name": "update_npc_state",
            "ok": True,
            "state_changed": True,
            "result": {"npc": {"mood": "警惕"}},
            "public_fallback_reply": "",
            "lock_public_reply": False,
        }
        public_receipt = {
            "tool_name": "commit_scene_response",
            "ok": True,
            "state_changed": True,
            "result": {},
            "public_fallback_reply": "门外传来新的钥匙转动声。",
            "lock_public_reply": True,
        }

        self.assertFalse(
            self.service._serialized_public_material_change_committed(
                private_receipt
            )
        )
        self.assertTrue(
            self.service._serialized_public_material_change_committed(
                public_receipt
            )
        )

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
        runtime.app.start_scene(
            "宫门冲突",
            SceneType.CONFLICT,
            participants=["王城卫兵", "伊莉雅"],
        )
        runtime.app.scene_frame_manager.ensure_frame(
            scene=runtime.app.scene_manager.current_scene,
            recent_chat="",
            world_state=runtime.app.world_state,
            character_manager=runtime.app.character_manager,
        )
        runtime.app.conflict_manager.start_scene(
            "宫门冲突",
            ["王城卫兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["王城卫兵"],
        )
        pending = runtime.app.npc_response_windows.open_request(
            runtime.app.scene_frame_manager.current_frame,
            npc="王城卫兵",
            summary="报上身份。",
            required_items=[{"item_id": "identity", "prompt": "报上身份"}],
            scene=runtime.app.scene_manager.current_scene,
        )
        self.assertIsNotNone(pending)

        status, _response = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("让当前敌方完成回合。"),
        )

        self.assertEqual(status, 200)
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["request_context"]["heartbeat_action"], "npc_turn")
        beat_text = request["current_turn"]["events"][0]["text"]
        self.assertTrue(beat_text.startswith("系统GM主动节拍请求："))
        beat_request = json.loads(beat_text.split("：", 1)[1])
        self.assertEqual(
            set(beat_request),
            {"action", "target", "outcome", "context"},
        )
        self.assertEqual(beat_request["target"], "王城卫兵")
        self.assertNotIn("不得", beat_text)
        self.assertNotIn("不要", beat_text)
        self.assertIn(
            "run_current_npc_turn",
            {tool["name"] for tool in request["available_tools"]},
        )

    def test_manual_gm_beat_prioritizes_pending_gm_opportunity(self) -> None:
        client = self.install_agent(
            [{"decision": "silent", "reason": "仅检查本次能力边界。"}]
        )
        runtime = self.service._runtime("http-agent-test")
        window = runtime.app.interceptor.decision_window_manager.create(
            kind="fumble_opportunity",
            owner="__gm__",
            prompt="GM选择一个大失败机会。",
            options=[{"effect": "转折"}],
            blocking=True,
            allowed_responders=["__gm__"],
            action_type="TriggerOpportunity",
            payload={"source_actor": "伊莉雅"},
        )

        status, _response = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("处理当前GM机会。"),
        )

        self.assertEqual(status, 200)
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(
            request["request_context"]["heartbeat_action"],
            "gm_opportunity",
        )
        self.assertEqual(
            {tool["name"] for tool in request["available_tools"]},
            {"get_scene_state", "get_gameplay_state", "resolve_gm_opportunity"},
        )
        beat_text = request["current_turn"]["events"][0]["text"]
        beat_request = json.loads(beat_text.split("：", 1)[1])
        self.assertEqual(beat_request["target"]["window_id"], window.window_id)
        self.assertEqual(beat_request["target"]["source_actor"], "伊莉雅")

    def test_manual_gm_beat_respects_director_hold_without_calling_agent(self) -> None:
        client = self.install_agent(
            [{"decision": "final", "reply": "这句不应被请求或公开。"}]
        )
        runtime = self.service._runtime("http-agent-test")
        progress = runtime.app.story_arc_manager.state.current_session_progress
        progress.stage = "development"
        progress.meaningful_turns = 8
        progress.gm_beat_purposes = ["escalation"]
        progress.gm_beat_player_turns = [8]

        status, response = self.service.handle(
            "POST",
            "/v1/game/gm-beat",
            self.payload("看看现在是否需要继续推进。"),
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertFalse(response["send_reply"])
        self.assertEqual(response["agent_mode"], "gm_beat_held")
        self.assertEqual(response["beat_directive"]["purpose"], "hold")
        self.assertEqual(client.calls, [])

    def test_heartbeat_prioritizes_natural_conflict_resolution_over_npc_turn(self) -> None:
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
        runtime.app.start_scene(
            "宫门冲突",
            SceneType.CONFLICT,
            participants=["王城卫兵", "伊莉雅"],
        )
        runtime.app.conflict_manager.start_scene(
            "宫门冲突",
            ["王城卫兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["王城卫兵"],
        )
        runtime.app.character_manager.get("伊莉雅").hp = 0
        runtime.app.conflict_manager.resolve_zero_hp("伊莉雅")
        runtime.app.conflict_manager.resolve_pending_zero_hp(
            "伊莉雅",
            choice="give_up_resistance",
            consequence="分离：被王城卫兵俘获",
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        gate = self.service.session_gates.get(
            "http-agent-test",
            "group-1",
            "s1",
        )

        decision = self.service._heartbeat_decision(
            runtime,
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            gate=gate,
            thresholds={
                "pre_session": 0,
                "session_zero": 0,
                "adventure": 0,
                "pc_turn": 0,
                "npc_turn": 0,
            },
            cooldown_seconds=0,
            force=True,
        )

        self.assertTrue(decision["should_respond"])
        self.assertEqual(decision["action"], "conflict_resolution")
        self.assertEqual(
            decision["conflict_resolution_status"]["natural_outcome"],
            "player_side_removed",
        )

    def test_pc_turn_heartbeat_reminds_once_per_conflict_turn(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "轮到伊莉雅了，想好再说就行。",
                    "reason": "本回合第一次轻量提醒。",
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
                name="王城卫兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=40,
                mp=40,
                traits=["enemy", "humanoid"],
            )
        )
        runtime.app.start_scene(
            "宫门冲突",
            SceneType.CONFLICT,
            participants=["伊莉雅", "王城卫兵"],
        )
        runtime.app.conflict_manager.start_scene(
            "宫门冲突",
            ["伊莉雅", "王城卫兵"],
            player_side=["伊莉雅"],
            enemy_side=["王城卫兵"],
        )
        gate = self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="冲突开始，现在轮到伊莉雅。",
            role="assistant",
            channel_id="group-1",
        )
        heartbeat_payload = {
            **self.payload(""),
            "auto_respond": True,
            "cooldown_seconds": 0,
            "pc_turn_idle_seconds": 0,
        }

        _status, first = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )
        _status, repeated = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )

        self.assertEqual(first["action"], "pc_turn_reminder")
        self.assertTrue(first["send_reply"])
        self.assertEqual(repeated["action"], "none")
        self.assertFalse(repeated["send_reply"])
        self.assertTrue(
            repeated["presence_telemetry"]["pc_turn_reminder_exhausted"]
        )
        self.assertEqual(len(client.calls), 1)

        runtime.app.conflict_manager.next_turn()
        runtime.app.conflict_manager.next_turn()
        next_turn = self.service._heartbeat_decision(
            runtime,
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            gate=gate,
            thresholds={
                "pre_session": 0,
                "session_zero": 0,
                "adventure": 0,
                "pc_turn": 0,
                "npc_turn": 0,
            },
            cooldown_seconds=0,
            force=False,
        )

        self.assertEqual(next_turn["action"], "pc_turn_reminder")
        self.assertEqual(next_turn["pc_turn_reminder_count"], 0)

    def test_heartbeat_opens_split_defeat_aftermath_at_fallen_hero_location(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        for name in ("诺艾尔", "艾丽妮"):
            runtime.app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=40,
                    hp=20 if name == "诺艾尔" else 0,
                    max_mp=40,
                    mp=40,
                    traits=["pc"],
                )
            )
        runtime.app.start_scene(
            "监狱外的雨夜",
            SceneType.STANDARD,
            location="卡里巴村监狱外",
            participants=["诺艾尔"],
        )
        runtime.app.scene_manager.actor_locations["艾丽妮"] = "卡里巴村监狱值班室"
        runtime.app.conflict_manager.state.fallen_pcs["艾丽妮"] = (
            "分离：被守卫重新收押"
        )
        gate = self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )

        decision = self.service._heartbeat_decision(
            runtime,
            campaign_id="http-agent-test",
            session_id="s1",
            channel_id="group-1",
            gate=gate,
            thresholds={
                "pre_session": 999,
                "session_zero": 999,
                "adventure": 999,
                "pc_turn": 999,
                "npc_turn": 999,
            },
            cooldown_seconds=999,
            force=False,
        )

        self.assertTrue(decision["should_respond"])
        self.assertEqual(decision["action"], "defeat_aftermath")
        aftermath = decision["defeat_aftermath"]
        self.assertEqual(aftermath["outcome_kind"], "split_defeat")
        self.assertEqual(aftermath["target_group"], ["艾丽妮"])
        self.assertEqual(aftermath["free_pcs"], ["诺艾尔"])
        self.assertEqual(aftermath["location"], "卡里巴村监狱值班室")
        self.assertFalse(aftermath["target_group_in_focus"])

    def test_manual_gm_beat_reports_failure_and_rolls_back_incomplete_npc_critical(
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
                dice=[(8, 8), (10, 8)],
                total=16,
                modifier=0,
                high_roll=8,
                target_number=10,
                success=True,
                critical_success=True,
                fumble=False,
                opportunity_count=1,
                margin=6,
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
                kind="critical_opportunity",
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

    def test_adventure_idle_uses_one_table_nudge_per_player_episode(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "这颗一，确实很有自己的想法。",
                    "reason": "结合刚才骰面做一句群聊短评。",
                },
                {
                    "decision": "final",
                    "reply": "这次我保证不替牢门加戏。",
                    "reason": "新玩家消息开启了新的静默周期。",
                },
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="投",
            role="user",
            channel_id="group-1",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="检定失败，封印提前重新亮起。",
            role="assistant",
            channel_id="group-1",
        )
        heartbeat_payload = {
            **self.payload(""),
            "auto_respond": True,
            "cooldown_seconds": 0,
            "adventure_idle_seconds": 0,
        }

        status, first = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )

        self.assertEqual(status, 200)
        self.assertEqual(first["action"], "adventure_table_nudge")
        self.assertTrue(first["send_reply"])
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["available_tools"], [])
        self.assertNotIn("current_state_summary", request)
        self.assertNotIn("current_message", request)
        self.assertNotIn("current_turn", request)
        self.assertNotIn("session", request)
        self.assertEqual(
            [(item["speaker"], item["text"]) for item in request["recent_messages"]],
            [("阿凛", "投")],
        )
        self.assertEqual(
            request["request_context"],
            {
                "heartbeat_action": "adventure_table_nudge",
                "heartbeat_persona_chat_only": True,
            },
        )
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("这是第一章开始后的现实群聊闲置判断", system_prompt)
        self.assertIn("真的有兴趣", system_prompt)
        self.assertNotIn("当前聚焦场景与权威状态", system_prompt)

        _status, exhausted = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )

        self.assertFalse(exhausted["send_reply"])
        self.assertEqual(exhausted["action"], "none")
        self.assertEqual(
            exhausted["idle_episode"]["adventure_nudge_count"],
            1,
        )
        self.assertEqual(len(client.calls), 1)

        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="南星",
            content="我还在，先看看值班室那边。",
            role="user",
            channel_id="group-1",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="好，镜头仍停在当前牢区。",
            role="assistant",
            channel_id="group-1",
        )

        _status, reset = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            heartbeat_payload,
        )

        self.assertEqual(reset["action"], "adventure_table_nudge")
        self.assertTrue(reset["send_reply"])
        self.assertEqual(len(client.calls), 2)

    def test_high_gm_ratio_still_asks_shiyou_whether_to_make_a_table_nudge(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "silent",
                    "reason": "时悠判断这次安静得正好，继续等玩家开口。",
                }
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        entries = [
            ("时悠", "assistant"),
            ("阿凛", "user"),
            ("时悠", "assistant"),
            ("南星", "user"),
            ("时悠", "assistant"),
            ("阿凛", "user"),
            ("时悠", "assistant"),
            ("南星", "user"),
            ("时悠", "assistant"),
            ("阿凛", "user"),
            ("时悠", "assistant"),
            ("时悠", "assistant"),
        ]
        for index, (speaker, role) in enumerate(entries):
            runtime.log_manager.append_message(
                "http-agent-test",
                "s1",
                speaker=speaker,
                content=f"发言样本 {index}",
                role=role,
                channel_id="group-1",
            )

        _status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "adventure_idle_seconds": 0,
            },
        )

        self.assertEqual(response["action"], "adventure_table_nudge")
        self.assertFalse(response["send_reply"])
        self.assertEqual(len(client.calls), 1)
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request["available_tools"], [])

    def test_table_nudge_discards_offline_gm_stage_direction(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "时悠敲了敲桌：‘这铁片来得还挺是时候。’",
                    "reason": "错误地模拟了线下主持动作。",
                }
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="投",
            role="user",
            channel_id="group-1",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="检定失败，封印提前重新亮起。",
            role="assistant",
            channel_id="group-1",
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "adventure_idle_seconds": 0,
            },
        )

        self.assertEqual(response["action"], "adventure_table_nudge")
        self.assertFalse(response["send_reply"])
        self.assertTrue(response["table_nudge_rejected"])
        self.assertIn("线下舞台动作", response["reason"])

    def test_table_nudge_has_no_lexical_restatement_filter(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "这颗一，确实很有自己的想法。",
                    "reason": "模型自己判断此刻想接这句。",
                }
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="这颗一，确实很有自己的想法。",
            role="user",
            channel_id="group-1",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="这颗一，确实很有自己的想法。",
            role="assistant",
            channel_id="group-1",
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "adventure_idle_seconds": 0,
            },
        )

        self.assertEqual(response["action"], "adventure_table_nudge")
        self.assertTrue(response["send_reply"])
        self.assertEqual(response["reply"], "这颗一，确实很有自己的想法。")
        self.assertNotIn("table_nudge_rejected", response)

    def test_table_nudge_is_not_rejected_only_for_using_two_sentences(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "你们慢慢商量。我刚好也想听听loading怎么想。",
                    "reason": "时悠自然参与玩家聊天。",
                }
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="阿凛",
            content="我们先听听loading怎么想。",
            role="user",
            channel_id="group-1",
        )
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="我先等你们商量。",
            role="assistant",
            channel_id="group-1",
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "cooldown_seconds": 0,
                "adventure_idle_seconds": 0,
            },
        )

        self.assertTrue(response["send_reply"])
        self.assertNotIn("table_nudge_rejected", response)

    def test_forced_heartbeat_respects_director_hold_without_calling_agent(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "这句不应被请求或公开。",
                }
            ]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        progress = runtime.app.story_arc_manager.state.current_session_progress
        progress.stage = "development"
        progress.meaningful_turns = 8
        progress.gm_beat_purposes = ["escalation"]
        progress.gm_beat_player_turns = [8]

        _status, response = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "force": True,
                "cooldown_seconds": 0,
            },
        )

        self.assertEqual(response["action"], "none")
        self.assertFalse(response["send_reply"])
        self.assertEqual(response["beat_directive"]["purpose"], "hold")
        self.assertTrue(
            response["presence_telemetry"]["held_by_beat_director"]
        )
        self.assertEqual(client.calls, [])

    def test_forced_material_consequence_can_interrupt_pending_npc_question(self) -> None:
        client = self.install_agent(
            [{"decision": "silent", "reason": "本测试只验证强制局势能到达主持智能体。"}]
        )
        self.service.session_gates.activate(
            "http-agent-test",
            "group-1",
            "s1",
            status="adventure",
        )
        runtime = self.service._runtime("http-agent-test")
        scene = runtime.app.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            participants=["伊莉雅", "梅芙"],
        )
        frame = runtime.app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat="",
            world_state=runtime.app.world_state,
            character_manager=runtime.app.character_manager,
        )
        runtime.app.world_state.ensure_npc_persona(
            "梅芙",
            profile_status="established",
            public_identity="白花守望会会长",
            current_location=scene.location or scene.name,
            last_seen_scene=scene.scene_id,
        )
        runtime.app.npc_response_windows.open_request(
            frame,
            npc="梅芙",
            summary="说明如何保护旅人。",
            required_items=[{"item_id": "plan", "prompt": "说明保护方案"}],
            scene=scene,
        )

        _status, held = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "force": True,
                "cooldown_seconds": 0,
                "instruction": "普通续接，不改变当前局势。",
            },
        )

        self.assertFalse(held["send_reply"])
        self.assertTrue(held["presence_telemetry"]["blocked_by_npc_response"])
        self.assertEqual(client.calls, [])

        _status, forced = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload(""),
                "auto_respond": True,
                "force": True,
                "cooldown_seconds": 0,
                "instruction": "【局势提交】追兵已经抵达，立即兑现这项外部压力。",
            },
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(
            bool((forced.get("presence_telemetry") or {}).get("blocked_by_npc_response"))
        )

    def test_heartbeat_cooldown_recognizes_agent_action_modes(self) -> None:
        runtime = self.service._runtime("http-agent-test")
        runtime.log_manager.append_message(
            "http-agent-test",
            "s1",
            speaker="时悠",
            content="这块铁片可别浪费了。",
            role="assistant",
            channel_id="group-1",
            metadata={
                "mode": "heartbeat_agent_adventure_table_nudge",
                "delivery_confirmed": True,
            },
        )
        entries = runtime.log_manager.load_transcript("http-agent-test", "s1")

        remaining = self.service._heartbeat_cooldown_remaining(
            entries,
            datetime.now(timezone.utc),
            180,
        )

        self.assertGreater(remaining, 0)

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
                            "message_kind": "state_contribution",
                            "audience": "gm",
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
                dice=[(8, 8), (10, 8)],
                total=16,
                modifier=0,
                high_roll=8,
                target_number=10,
                success=True,
                critical_success=True,
                fumble=False,
                opportunity_count=1,
                margin=6,
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
                kind="critical_opportunity",
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

    def test_ready_session_zero_heartbeat_invites_chapter_one_once(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "set_chapter_one_transition",
                    "arguments": {"posture": "invited"},
                    "reason": "最近讨论已经收束，询问是否开章。",
                },
                {
                    "decision": "final",
                    "reply": "第零章已经收好了。现在进入第一章吗？",
                    "reason": "只发出一次开章邀请。",
                },
            ]
        )
        runtime = self.service._runtime("http-agent-test")
        self.make_session_zero_adventure_ready(runtime)
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
            content="越狱开场就按这个，暂时没有其他要补的了。",
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
            response["session_zero_nudge_target"]["status"],
            "chapter_one_ready",
        )
        self.assertIn("现在进入第一章吗", response["reply"])
        self.assertEqual(
            runtime.app.session_zero_manager
            .chapter_one_transition_status(ready=True)["status"],
            "invited",
        )
        request = json.loads(client.calls[0]["messages"][1].content)
        self.assertTrue(
            request["current_state_summary"]["session_zero"]
            ["adventure_readiness"]["ready"]
        )

        _, repeated = self.service.handle(
            "POST",
            "/v1/session/heartbeat",
            {
                **self.payload("", message_id="heartbeat-2"),
                "auto_respond": False,
                "cooldown_seconds": 0,
                "session_zero_idle_seconds": 0,
                "setup_nudge_limit": 1,
            },
        )
        self.assertFalse(repeated["should_respond"])
        self.assertEqual(
            runtime.app.session_zero_manager
            .chapter_one_transition_status(ready=True)["status"],
            "invited",
        )

    def test_ready_session_zero_heartbeat_waits_while_supplementing(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "不应发送。",
                    "reason": "测试占位。",
                }
            ]
        )
        runtime = self.service._runtime("http-agent-test")
        self.make_session_zero_adventure_ready(runtime)
        runtime.app.session_zero_manager.set_chapter_one_transition(
            "supplementing",
            speaker="阿凛",
            evidence="我还想补监狱长的背景。",
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
            content="我还想补监狱长的背景。",
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
        self.assertFalse(response["should_respond"])
        self.assertEqual(
            response["session_zero_nudge_target"]["status"],
            "supplementing",
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

    def test_direct_reply_keeps_causal_target_but_defaults_to_plain_message(self) -> None:
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
        self.assertFalse(envelope["quote"])
        self.assertEqual(envelope["delivery"]["mode"], "normal")

    def test_agent_can_request_a_valid_exact_quote_when_context_is_ambiguous(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "你引用的那条裁定仍然有效。",
                    "delivery": {
                        "mode": "quote_reply",
                        "quote_message_id": "qq-43",
                        "mention_user_ids": [],
                        "semantic_targets": ["阿凛"],
                        "reason": "正在澄清一条被引用的旧裁定。",
                        "confidence": 0.95,
                    },
                    "reason": "回答玩家的澄清问题。",
                }
            ]
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "@时悠，我引用的那条裁定还有效吗？",
                message_id="qq-43",
                addressed=True,
            ),
        )

        envelope = response["reply_envelopes"][0]
        self.assertTrue(envelope["quote"])
        self.assertEqual(envelope["delivery"]["mode"], "quote_reply")
        self.assertEqual(envelope["delivery"]["quote_message_id"], "qq-43")

    def test_invalid_agent_quote_target_is_delivered_as_plain_message(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "我直接回答这句。",
                    "delivery": {
                        "mode": "quote_reply",
                        "quote_message_id": "made-up-message",
                        "mention_user_ids": [],
                        "semantic_targets": ["阿凛"],
                        "reason": "错误地选择了不存在的消息。",
                        "confidence": 0.6,
                    },
                    "reason": "回答玩家。",
                }
            ]
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "@时悠，回答一下。",
                message_id="qq-44",
                addressed=True,
            ),
        )

        envelope = response["reply_envelopes"][0]
        self.assertFalse(envelope["quote"])
        self.assertEqual(envelope["target_message_id"], "qq-44")
        self.assertEqual(envelope["delivery"]["mode"], "normal")
        self.assertEqual(
            envelope["delivery"]["downgraded_from"],
            "quote_reply",
        )

    def test_private_message_never_uses_platform_quote_or_mention(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "私聊里直接说就好。",
                    "delivery": {
                        "mode": "quote_reply",
                        "quote_message_id": "private-1",
                        "mention_user_ids": [],
                        "semantic_targets": ["阿凛"],
                        "reason": "不必要的私聊引用。",
                        "confidence": 0.8,
                    },
                    "reason": "回应私聊。",
                }
            ]
        )
        payload = self.payload(
            "时悠，私聊确认一下。",
            message_id="private-1",
            addressed=True,
        )
        payload["is_private"] = True

        _status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        envelope = response["reply_envelopes"][0]
        self.assertFalse(envelope["quote"])
        self.assertEqual(envelope["delivery"]["mode"], "normal")

    def test_private_message_and_reply_stay_out_of_public_transcript(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "这条私聊已经收到。",
                    "reason": "回应私聊。",
                }
            ]
        )
        payload = self.payload(
            "这是只给 GM 的私聊。",
            message_id="private-audit-1",
            addressed=True,
        )
        payload.update(
            {
                "is_private": True,
                "anonymous": True,
                "speaker": "真实玩家名",
                "speaker_id": "qq-user-42",
                "astrbot_context": {
                    "is_private": True,
                    "sender_id": "qq-user-42",
                    "sender_name": "真实玩家名",
                },
            }
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["send_reply"])
        runtime = self.service._runtime("http-agent-test")
        entries = runtime.log_manager.load_transcript("http-agent-test", "s1")
        self.assertEqual([entry.role for entry in entries], ["private", "system_private"])
        self.assertEqual(entries[0].speaker, "匿名玩家")
        self.assertEqual(entries[0].channel_id, "")
        self.assertEqual(entries[0].message_id, "")
        self.assertNotIn("speaker_id", entries[0].metadata)
        self.assertNotIn("astrbot_context", entries[0].metadata)

        _dashboard_status, dashboard = self.service.handle(
            "GET",
            "/v1/audit/dashboard?campaign_id=http-agent-test&session_id=s1",
            {},
        )
        self.assertEqual(dashboard["logs"]["recent_transcript"], [])

    def test_private_followup_receives_only_same_thread_private_context(self) -> None:
        client = self.install_agent(
            [
                {
                    "decision": "final",
                    "reply": "旅人的技能有忠诚伙伴（+5）。",
                    "reason": "回答职业技能查询。",
                },
                {
                    "decision": "final",
                    "reply": "这里的（+5）表示最多可以取得五次。",
                    "reason": "承接同一私聊中的技能标记。",
                },
            ]
        )
        first = self.payload(
            "悠老师，旅人的技能有哪些",
            message_id="private-rank-1",
            addressed=True,
        )
        first.update({"is_private": True, "anonymous": True})
        second = self.payload(
            "这个加五是什么意思",
            message_id="private-rank-2",
            addressed=True,
        )
        second.update({"is_private": True, "anonymous": True})

        self.service.handle("POST", "/v1/message/route", first)
        self.service.handle("POST", "/v1/message/route", second)

        second_request = json.loads(client.calls[1]["messages"][-1].content)
        recent = second_request["recent_messages"]
        self.assertEqual(second_request["request_context"]["recent_messages_visibility"], "private_thread")
        self.assertTrue(any(item["role"] == "user" and "旅人的技能" in item["text"] for item in recent))
        self.assertTrue(any(item["role"] == "assistant" and "忠诚伙伴（+5）" in item["text"] for item in recent))
        self.assertTrue(all(item["visibility"] == "private_thread" for item in recent))


if __name__ == "__main__":
    unittest.main()
