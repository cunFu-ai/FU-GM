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


def test_envelope_recognizes_configured_gm_alias_as_an_identity_address() -> None:
    builder = GMMessageEnvelopeBuilder(gm_aliases=("时悠", "悠老师"))
    envelope = builder.build(
        {
            "message": "时悠，你觉得大家刚才的方案怎么样？",
            "is_at_bot": "false",
            "is_reply_to_bot": 0,
            "force_gm_reply": "off",
        }
    )

    assert envelope.platform_addressed is False
    assert envelope.forced_reply is True
    assert envelope.directly_addressed is True
    assert envelope.external_metadata["identity_addressed"] is True


def test_envelope_keeps_ordinary_player_prose_unaddressed() -> None:
    builder = GMMessageEnvelopeBuilder(gm_aliases=("时悠", "悠老师"))
    envelope = builder.build(
        {
            "message": "大家刚才说时悠会怎么裁定？先等队友的意见。",
            "is_at_bot": "false",
            "is_reply_to_bot": 0,
            "force_gm_reply": "off",
        }
    )

    assert envelope.platform_addressed is False
    assert envelope.forced_reply is False
    assert envelope.directly_addressed is False


def test_anonymous_private_envelope_uses_alias_and_drops_platform_identity() -> None:
    builder = GMMessageEnvelopeBuilder()
    envelope = builder.build(
        {
            "speaker": "真实玩家名",
            "speaker_id": "qq-user-42",
            "message_id": "private-message-1",
            "message": "界限：不要出现蜘蛛。",
            "anonymous": True,
            "astrbot_context": {
                "is_private": True,
                "sender_id": "qq-user-42",
                "sender_name": "真实玩家名",
                "group_id": "private:qq-user-42",
                "self_id": "bot-1",
            },
        }
    )

    assert envelope.is_private is True
    assert envelope.speaker == "匿名玩家"
    assert envelope.external_metadata["anonymous"] is True
    assert "speaker_id" not in envelope.external_metadata
    assert "message_id" not in envelope.external_metadata
    assert envelope.external_metadata["astrbot_context"] == {"is_private": True}


def test_envelope_recognizes_honorific_without_punctuation_and_common_greeting() -> None:
    builder = GMMessageEnvelopeBuilder(gm_aliases=("时悠", "悠老师"))

    assert builder.is_identity_addressed("悠老师重新开场") is True
    assert builder.is_identity_addressed("oi，时悠，重新开场") is True


def test_model_delivery_context_exposes_only_trusted_message_and_batch_ids() -> None:
    context = GMMessageEnvelopeBuilder.model_request_context(
        {
            "message_id": "m-current",
            "speaker_id": "u-current",
            "recent_message_delivery_context": [
                {
                    "message_id": "m-old",
                    "speaker": "白河",
                    "speaker_id": "u-old",
                    "text": "前一条消息",
                    "is_current": False,
                }
            ],
            "batch_parent_id": "batch-1",
            "batch_index": 1,
            "batch_count": 2,
            "batch_has_later_messages": True,
        }
    )

    assert context["current_transport_message"] == {
        "message_id": "m-current",
        "speaker_id": "u-current",
    }
    assert context["recent_message_delivery_context"][0]["message_id"] == "m-old"
    assert "text" not in context["recent_message_delivery_context"][0]
    assert context["buffered_batch"]["has_later_messages"] is True


def test_typed_route_keeps_transport_selected_private_campaign_and_raw_message() -> None:
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
        assert response["campaign_id"] == "default"
        assert len(agent.calls) == 1
        call = agent.calls[0]
        assert call["message"] == "这里说错了，loading才是玩家名。"
        context = call["context"]
        assert context.campaign_id == "default"
        assert context.directly_addressed is True
        assert context.metadata["quoted_message"]["text"] == (
            "艾丽妮的角色是艾丽妮。"
        )
        assert "引用消息" not in call["message"]

        logs = service._runtime("default").log_manager.load_transcript(
            "default",
            "private-user-1",
        )
        user_entry = next(item for item in logs if item.role == "private")
        assert user_entry.content == "这里说错了，loading才是玩家名。"
        assert user_entry.speaker == "匿名玩家"
        assert user_entry.metadata["private"] is True
        assert "quoted_message" not in user_entry.metadata


def test_buffered_turn_preserves_an_earlier_gm_identity_address() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        service = FUGMHttpService(data_root=tmpdir, use_llm=False)
        service.session_gates.activate(
            "default",
            "group-1",
            "s1",
            status="adventure",
        )
        agent = CapturingAgent()
        service.gm_tool_agent = agent

        status, _response = service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "default",
                "session_id": "s1",
                "channel_id": "group-1",
                "speaker": "loading",
                "message": "我也想从这里开始。",
                "batch_id": "turn-1",
                "current_turn_messages": [
                    {
                        "speaker": "村夫",
                        "message": "悠老师重新开场",
                        "message_id": "m-1",
                    },
                    {
                        "speaker": "loading",
                        "message": "我也想从这里开始。",
                        "message_id": "m-2",
                    },
                ],
            },
        )

        assert status == 200
        assert len(agent.calls) == 1
        assert agent.calls[0]["message"] == "我也想从这里开始。"
        assert agent.calls[0]["context"].directly_addressed is True
