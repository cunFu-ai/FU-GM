from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, Clock, HeroDraft, SceneType


class ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [
            json.dumps(item, ensure_ascii=False)
            for item in responses
        ]

    def create_chat_completion(self, **kwargs: object) -> str:
        if not self.responses:
            raise AssertionError("缺少脚本化GM响应。")
        request = json.loads(kwargs["messages"][1].content)
        available = {
            str(item.get("name") or "")
            for item in list(request.get("available_tools") or [])
            if isinstance(item, dict)
        }
        scripted = json.loads(self.responses[0])
        requested = (
            {str(scripted.get("tool_name") or "")}
            if scripted.get("decision") == "call_tool"
            else set()
        )
        missing = {
            name for name in requested if name and name not in available
        }
        if missing and GMCapabilityBroker.DISCOVERY_TOOL in available:
            domains = GMCapabilityBroker.domains_for_tools(missing)
            if domains:
                discovery = {
                        "decision": "call_tool",
                        "tool_name": "discover_capabilities",
                        "arguments": {
                            "domains": domains[:4],
                            "reason": "测试模型按协议取得所需能力。",
                        },
                    }
                return json.dumps(
                    discovery,
                    ensure_ascii=False,
                )
        return self.responses.pop(0)


class TypedLongRunRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        # This suite supplies prebuilt tool decisions and validates their
        # transaction semantics. Message-relation parsing is covered by the
        # coordinator tests and remains default-on in production.
        self._legacy_semantics_contract = patch.dict(
            "os.environ",
            {"FU_GM_MESSAGE_SEMANTICS_CONTRACT": "0"},
        )
        self._legacy_semantics_contract.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime("typed-regression")
        self.app = self.runtime.app
        self.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=45,
                max_mp=35,
                mp=35,
                traits=["pc"],
            )
        )
        self.app.character_manager.add(
            Character(
                name="洛岚",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=40,
                hp=40,
                max_mp=50,
                mp=50,
                traits=["pc"],
            )
        )
        self.app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="伊莉雅",
        )
        self.app.world_state.world_profile.hero_drafts["白河"] = HeroDraft(
            player_name="白河",
            hero_name="洛岚",
        )
        self.app.scene_manager.start_scene(
            "白花碑驿站",
            SceneType.STANDARD,
            location="风铃廊",
            participants=["伊莉雅", "洛岚"],
        )
        self.service.session_gates.activate(
            "typed-regression",
            "group-1",
            "s1",
            status="adventure",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        self._legacy_semantics_contract.stop()

    def install_agent(self, responses: list[dict[str, object]]) -> None:
        self.service.gm_tool_agent = LLMGMToolAgent(
            ScriptedClient(responses),
            model="fake",
            registry=self.service.gm_tool_registry,
        )

    @staticmethod
    def payload(message: str, *, speaker: str, message_id: str) -> dict[str, object]:
        return {
            "campaign_id": "typed-regression",
            "session_id": "s1",
            "channel_id": "group-1",
            "speaker": speaker,
            "message": message,
            "message_id": message_id,
        }

    def test_agent_cannot_swap_the_declared_actor_during_rule_resolution(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "perform_in_scene_action",
                    "arguments": {
                        "actor": "洛岚",
                        "action_summary": "洛岚走到闸门边",
                        "position_note": "风铃廊·闸门边",
                    },
                    "terminal_decision": "silent",
                    "reason": "白河明确声明洛岚行动。",
                }
            ]
        )

        status, response = self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "洛岚走到闸门边。",
                speaker="白河",
                message_id="actor-1",
            ),
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            self.app.scene_manager.position_of("洛岚"),
            "风铃廊·闸门边",
        )
        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "")
        self.assertEqual(response["reply"], "")

    def test_rejected_wrong_owner_action_does_not_fall_back_to_another_character(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "perform_in_scene_action",
                    "arguments": {
                        "actor": "伊莉雅",
                        "action_summary": "伊莉雅检查闸门",
                        "position_note": "风铃廊·闸门边",
                    },
                    "reason": "错误地替换了行动者。",
                },
                {
                    "decision": "final",
                    "reply": "这次没有结算，我需要按白河控制的洛岚重新处理。",
                    "reason": "工具拒绝了错误归属。",
                },
            ]
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/game/turn",
            self.payload(
                "洛岚检查闸门。",
                speaker="白河",
                message_id="actor-2",
            ),
        )

        self.assertEqual(self.app.scene_manager.position_of("伊莉雅"), "")
        self.assertEqual(self.app.scene_manager.position_of("洛岚"), "")
        rejected = next(
            item
            for item in response["tool_receipts"]
            if item["tool_name"] == "perform_in_scene_action"
        )
        self.assertFalse(rejected["ok"])

    def test_round_clock_advances_only_after_every_present_pc_acts(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "perform_in_scene_action",
                    "arguments": {
                        "actor": "伊莉雅",
                        "action_summary": "伊莉雅守住门口",
                        "position_note": "风铃廊·门口",
                    },
                    "terminal_decision": "silent",
                    "reason": "只记录站位。",
                },
                {
                    "decision": "call_tool",
                    "tool_name": "perform_in_scene_action",
                    "arguments": {
                        "actor": "洛岚",
                        "action_summary": "洛岚检查门轴",
                        "position_note": "风铃廊·闸门边",
                    },
                    "terminal_decision": "reply",
                    "reason": "完成本轮所有英雄行动。",
                },
            ]
        )

        self.service.handle(
            "POST",
            "/v1/game/turn",
            self.payload(
                "伊莉雅守住门口。",
                speaker="阿凛",
                message_id="round-1",
            ),
        )
        self.assertEqual(self.app.clock_manager.get("巡逻队逼近").current, 0)

        _status, response = self.service.handle(
            "POST",
            "/v1/game/turn",
            self.payload(
                "洛岚检查门轴。",
                speaker="白河",
                message_id="round-2",
            ),
        )
        self.assertEqual(self.app.clock_manager.get("巡逻队逼近").current, 1)
        self.assertIn("【巡逻队逼近】1/6", response["reply"])

    def test_player_discussion_never_advances_the_round_clock(self) -> None:
        self.app.clock_manager.add(
            Clock(
                name="巡逻队逼近",
                max_segments=6,
                clock_type="threat",
                auto_advance="每个行动轮结束时推进1格",
                scope="session",
            )
        )
        self.install_agent(
            [
                {
                    "decision": "silent",
                    "reason": "玩家只是在商量分工。",
                }
            ]
        )

        self.service.handle(
            "POST",
            "/v1/message/route",
            self.payload(
                "谁方便盯外面，谁继续检查闸门？",
                speaker="阿凛",
                message_id="talk-1",
            ),
        )

        self.assertEqual(self.app.clock_manager.get("巡逻队逼近").current, 0)

    def test_a_tool_failure_cannot_leave_a_partial_world_fact(self) -> None:
        self.install_agent(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "perform_in_scene_action",
                    "arguments": {
                        "actor": "洛岚",
                        "action_summary": "洛岚检查闸门",
                        "public_result": "巡守已经接过路牌并打开闸门。",
                        "public_facts": ["巡守已经接过路牌并打开闸门。"],
                    },
                    "reason": "错误地把建议写成既成事实。",
                },
                {
                    "decision": "final",
                    "reply": "巡守还没有接牌，动作没有结算。",
                    "reason": "规则工具拒绝了无依据结果。",
                },
            ]
        )

        _status, response = self.service.handle(
            "POST",
            "/v1/game/turn",
            self.payload(
                "洛岚只是示意巡守接牌。",
                speaker="白河",
                message_id="fact-1",
            ),
        )

        public_memory = "\n".join(
            event.summary
            for event in self.app.world_state.memory_events
            if event.visibility == "public"
        )
        self.assertNotIn("已经接过", public_memory)
        self.assertNotIn("已经接过", response["reply"])


if __name__ == "__main__":
    unittest.main()
