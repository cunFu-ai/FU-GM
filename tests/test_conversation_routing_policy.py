from __future__ import annotations

import tempfile
import unittest

from fu_gm.gm_tool_agent import GMToolAgentOutcome
from fu_gm.http_server import FUGMHttpService


class _ScriptedAgent:
    def __init__(self, outcome: GMToolAgentOutcome) -> None:
        self.outcome = outcome
        self.messages: list[str] = []

    def run(self, message: str, *_args, **_kwargs) -> GMToolAgentOutcome:
        self.messages.append(message)
        return self.outcome


class SingleAuthorityRoutingTests(unittest.TestCase):
    def _route(
        self,
        outcome: GMToolAgentOutcome,
        *,
        message: str,
        explicitly_addressed: bool = False,
    ) -> tuple[_ScriptedAgent, dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = _ScriptedAgent(outcome)
            service.gm_tool_agent = agent
            service.session_gates.activate(
                "单一路由测试",
                "group-1",
                "s1",
                status="adventure",
            )
            status, body = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "单一路由测试",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "speaker": "南星",
                    "message": message,
                    "explicitly_addressed": explicitly_addressed,
                },
            )
        self.assertEqual(status, 200)
        return agent, body

    def test_player_coordination_is_read_then_silenced_by_agent(self) -> None:
        agent, body = self._route(
            GMToolAgentOutcome(
                handled=True,
                target="silent",
                mode="gm_agent_silent",
                stop_astrbot=True,
                reason="玩家只在彼此商量，没有提交行动或要求裁定。",
            ),
            message="我倾向于先核对凭证，再决定谁去登记。",
        )

        self.assertEqual(agent.messages, ["我倾向于先核对凭证，再决定谁去登记。"])
        self.assertEqual(body["target"], "silent")
        self.assertFalse(body["send_reply"])
        self.assertEqual(body["route"], "gm_agent_silent")

    def test_external_request_is_delegated_by_agent(self) -> None:
        _agent, body = self._route(
            GMToolAgentOutcome(
                handled=True,
                target="astrbot",
                mode="external",
                stop_astrbot=False,
                reason="这是跑团外的天气查询。",
            ),
            message="帮我查一下明天会不会下雨。",
        )

        self.assertEqual(body["target"], "astrbot")
        self.assertFalse(body["stop_astrbot"])
        self.assertFalse(body["send_reply"])

    def test_direct_address_reply_is_authored_by_agent_not_local_policy(self) -> None:
        _agent, body = self._route(
            GMToolAgentOutcome(
                handled=True,
                target="fu_gm",
                mode="gm_agent_reply",
                reply="我在。你想查看当前场景，还是角色卡？",
                stop_astrbot=True,
                reason="玩家直接询问GM。",
            ),
            message="你在吗？",
            explicitly_addressed=True,
        )

        self.assertEqual(body["target"], "fu_gm")
        self.assertTrue(body["send_reply"])
        self.assertEqual(body["reply"], "我在。你想查看当前场景，还是角色卡？")


if __name__ == "__main__":
    unittest.main()
