from __future__ import annotations

import tempfile

from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_message_envelope import GMMessageEnvelopeBuilder
from fu_gm.http_server import FUGMHttpService


class CapturingAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, message: str, **kwargs) -> GMToolAgentOutcome:
        self.calls.append({"message": message, **kwargs})
        return GMToolAgentOutcome(
            handled=True,
            reply="我看到了。",
            target="fu_gm",
            mode="gm_agent_reply",
            stop_astrbot=True,
            terminal_action="final",
        )


def test_envelope_keeps_current_message_separate_from_quote() -> None:
    builder = GMMessageEnvelopeBuilder()
    envelope = builder.build(
        {
            "campaign_id": "default",
            "session_id": "s1",
            "speaker": "村夫",
            "message": "这里的玩家名说反了。",
            "is_reply_to_bot": True,
            "quoted_message": {
                "message_id": "old-1",
                "sender_id": "bot-1",
                "text": "艾丽妮的角色是艾丽妮。",
            },
        },
        campaign_id="当前团",
    )

    assert envelope.campaign_id == "当前团"
    assert envelope.current_message == "这里的玩家名说反了。"
    assert envelope.external_metadata["quoted_message"]["text"] == (
        "艾丽妮的角色是艾丽妮。"
    )
    assert envelope.directly_addressed is True
    assert envelope.routing_payload({"campaign_id": "default"})["campaign_id"] == (
        "当前团"
    )


def test_envelope_never_infers_addressing_from_player_prose() -> None:
    builder = GMMessageEnvelopeBuilder()
    envelope = builder.build(
        {
            "message": "时悠，你觉得大家刚才的方案怎么样？",
            "is_at_bot": "false",
            "is_reply_to_bot": 0,
            "force_gm_reply": "off",
        }
    )

    assert envelope.platform_addressed is False
    assert envelope.forced_reply is False
    assert envelope.directly_addressed is False


def test_typed_route_uses_resolved_private_campaign_and_raw_message() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        service = FUGMHttpService(data_root=tmpdir, use_llm=False)
        service.session_gates.activate(
            "群内当前团",
            "qq-group-1",
            "qq-group-1",
            status="adventure",
        )
        agent = CapturingAgent()
        service.gm_tool_agent = agent

        status, response = service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "default",
                "session_id": "private-user-1",
                "speaker": "阿凛",
                "message": "这里说错了，loading才是玩家名。",
                "is_private": True,
                "is_reply_to_bot": True,
                "quoted_message": {
                    "message_id": "old-msg",
                    "sender_id": "bot-1",
                    "text": "艾丽妮的角色是艾丽妮。",
                },
            },
        )

        assert status == 200
        assert response["campaign_id"] == "群内当前团"
        assert len(agent.calls) == 1
        call = agent.calls[0]
        assert call["message"] == "这里说错了，loading才是玩家名。"
        context = call["context"]
        assert context.campaign_id == "群内当前团"
        assert context.directly_addressed is True
        assert context.metadata["quoted_message"]["text"] == (
            "艾丽妮的角色是艾丽妮。"
        )
        assert "引用消息" not in call["message"]

        logs = service._runtime("群内当前团").log_manager.load_transcript(
            "群内当前团",
            "private-user-1",
        )
        user_entry = next(item for item in logs if item.role == "user")
        assert user_entry.content == "这里说错了，loading才是玩家名。"
        assert user_entry.metadata["quoted_message"]["message_id"] == "old-msg"
