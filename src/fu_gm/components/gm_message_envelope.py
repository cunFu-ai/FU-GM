from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_QUOTED_MESSAGE_FIELDS = {"message_id", "sender_id", "text", "source"}
_ASTRBOT_CONTEXT_FIELDS = {
    "sender_id",
    "sender_name",
    "group_id",
    "self_id",
    "is_private",
    "is_at_bot",
    "is_reply_to_bot",
    "segment_types",
    "mentions",
    "attachments",
}


def trusted_flag(value: object) -> bool:
    """Interpret transport booleans without reading natural-language prose."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


@dataclass(frozen=True)
class GMMessageEnvelope:
    """Trusted transport data for one natural-language GM transaction.

    ``current_message`` remains the player's exact text (apart from outer
    whitespace). Quoted content and platform addressing stay in separate
    fields so the semantic agent can use them as context without mistaking
    them for words the current speaker just said.
    """

    campaign_id: str
    session_id: str
    channel_id: str
    speaker: str
    current_message: str
    is_private: bool
    platform_addressed: bool
    forced_reply: bool
    external_metadata: dict[str, Any]

    @property
    def directly_addressed(self) -> bool:
        return self.platform_addressed or self.forced_reply

    @property
    def is_command(self) -> bool:
        return self.current_message.lstrip().startswith("/")

    def routing_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result.update(
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "speaker": self.speaker,
                "message": self.current_message,
                "is_private": self.is_private,
                "is_at_bot": trusted_flag(payload.get("is_at_bot")),
                "is_reply_to_bot": trusted_flag(payload.get("is_reply_to_bot")),
                "force_gm_reply": self.forced_reply,
            }
        )
        return result


class GMMessageEnvelopeBuilder:
    """Normalize platform metadata without classifying player intent."""

    def build(
        self,
        payload: dict[str, Any],
        *,
        campaign_id: str | None = None,
    ) -> GMMessageEnvelope:
        resolved_campaign = str(
            campaign_id
            if campaign_id is not None
            else payload.get("campaign_id") or "default"
        ).strip() or "default"
        session_id = str(payload.get("session_id") or "default").strip() or "default"
        channel_id = str(payload.get("channel_id") or "")
        speaker = str(
            payload.get("speaker") or payload.get("user_name") or "玩家"
        ).strip() or "玩家"
        current_message = str(payload.get("message") or "").strip()
        platform_addressed = trusted_flag(payload.get("is_at_bot")) or trusted_flag(
            payload.get("is_reply_to_bot")
        )
        forced_reply = trusted_flag(payload.get("force_gm_reply"))
        metadata = self.external_metadata(payload)
        return GMMessageEnvelope(
            campaign_id=resolved_campaign,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            current_message=current_message,
            is_private=trusted_flag(payload.get("is_private")),
            platform_addressed=platform_addressed,
            forced_reply=forced_reply,
            external_metadata=metadata,
        )

    @staticmethod
    def quoted_message(payload: dict[str, Any]) -> dict[str, Any]:
        quoted = payload.get("quoted_message")
        if not isinstance(quoted, dict):
            context = payload.get("astrbot_context")
            if isinstance(context, dict) and isinstance(
                context.get("quoted_message"), dict
            ):
                quoted = context["quoted_message"]
            else:
                quoted = {}
        return {
            key: str(value)[:800]
            for key, value in quoted.items()
            if key in _QUOTED_MESSAGE_FIELDS and value not in ("", None)
        }

    @classmethod
    def external_metadata(cls, payload: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if payload.get("message_id"):
            metadata["message_id"] = str(payload["message_id"])
        if payload.get("speaker_id"):
            metadata["speaker_id"] = str(payload["speaker_id"])
        if trusted_flag(payload.get("is_at_bot")):
            metadata["is_at_bot"] = True
        if trusted_flag(payload.get("is_reply_to_bot")):
            metadata["is_reply_to_bot"] = True
        if trusted_flag(payload.get("force_gm_reply")):
            metadata["force_gm_reply"] = True
        quoted = cls.quoted_message(payload)
        if quoted:
            metadata["quoted_message"] = quoted
        context = payload.get("astrbot_context")
        if isinstance(context, dict) and context:
            compact = {
                key: value
                for key, value in context.items()
                if key in _ASTRBOT_CONTEXT_FIELDS
                and value not in ("", None, [], {})
            }
            if compact:
                metadata["astrbot_context"] = compact
        return metadata

    @classmethod
    def external_payload_fields(cls, payload: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key in (
            "message_id",
            "speaker_id",
            "is_at_bot",
            "is_reply_to_bot",
            "force_gm_reply",
            "quoted_message",
            "astrbot_context",
        ):
            value = payload.get(key)
            if value not in ("", None, [], {}):
                fields[key] = value
        return fields

    @staticmethod
    def model_request_context(metadata: dict[str, object]) -> dict[str, object]:
        """Expose only transport context useful for semantic resolution."""

        result: dict[str, object] = {}
        quoted = metadata.get("quoted_message")
        if isinstance(quoted, dict) and quoted:
            result["quoted_message"] = dict(quoted)
        for key in ("is_at_bot", "is_reply_to_bot", "force_gm_reply"):
            if metadata.get(key):
                result[key] = True
        context = metadata.get("astrbot_context")
        if isinstance(context, dict):
            selected = {
                key: context[key]
                for key in ("mentions", "attachments", "segment_types")
                if context.get(key) not in ("", None, [], {})
            }
            if selected:
                result["astrbot_context"] = selected
        return result
