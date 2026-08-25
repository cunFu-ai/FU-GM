from __future__ import annotations

import os
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
_DEFAULT_GM_IDENTITY_ALIASES = ("时悠", "悠老师")
_VOCATIVE_SEPARATORS = " \t\r\n,，:：!！?？"
_GREETING_PREFIXES = ("hello", "哈喽", "oi", "hi", "嗨", "嘿", "喂")


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

    def __init__(self, *, gm_aliases: tuple[str, ...] | None = None) -> None:
        if gm_aliases is None:
            configured = tuple(
                item.strip()
                for item in os.environ.get("FU_GM_GM_ALIASES", "").split(",")
                if item.strip()
            )
            gm_aliases = configured or _DEFAULT_GM_IDENTITY_ALIASES
        self.gm_aliases = tuple(dict.fromkeys(
            str(item or "").strip() for item in gm_aliases if str(item or "").strip()
        ))

    def with_identity_addressing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mark a clear GM vocative without inferring the requested action.

        Platform mentions and replies remain the primary transport facts.  A
        configured GM name at the beginning of a message is the one narrow
        textual exception: it only guarantees a failure reply when the model
        service is down, while semantic routing still decides what to do.
        """

        normalized = dict(payload)
        if self.is_identity_addressed(str(normalized.get("message") or "")):
            normalized["force_gm_reply"] = True
            normalized["identity_addressed"] = True
        return normalized

    def is_identity_addressed(self, message: str) -> bool:
        """Recognize a configured GM vocative at the opening of a message."""

        clean = str(message or "").lstrip("@ \t\r\n")
        if not clean or not self.gm_aliases:
            return False
        heads = [clean]
        lowered = clean.lower()
        for greeting in _GREETING_PREFIXES:
            if not lowered.startswith(greeting):
                continue
            tail = clean[len(greeting) :]
            if tail and tail[0] in _VOCATIVE_SEPARATORS:
                heads.append(tail.lstrip(_VOCATIVE_SEPARATORS))
            break
        for head in heads:
            for alias in self.gm_aliases:
                if not head.startswith(alias):
                    continue
                tail = head[len(alias) :]
                if not tail or tail[0] in _VOCATIVE_SEPARATORS:
                    return True
                # Chinese honorifics are commonly written without punctuation,
                # for example: "悠老师重新开场".
                if alias.endswith("老师"):
                    return True
        return False

    def build(
        self,
        payload: dict[str, Any],
        *,
        campaign_id: str | None = None,
    ) -> GMMessageEnvelope:
        payload = self.with_identity_addressing(payload)
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
        astrbot_context = payload.get("astrbot_context")
        is_private = trusted_flag(payload.get("is_private")) or (
            isinstance(astrbot_context, dict)
            and trusted_flag(astrbot_context.get("is_private"))
        )
        anonymous = is_private and trusted_flag(payload.get("anonymous"))
        if anonymous:
            speaker = "匿名玩家"
        current_message = str(payload.get("message") or "").strip()
        platform_addressed = trusted_flag(payload.get("is_at_bot")) or trusted_flag(
            payload.get("is_reply_to_bot")
        )
        forced_reply = trusted_flag(payload.get("force_gm_reply"))
        metadata = self.external_metadata(payload)
        if trusted_flag(payload.get("identity_addressed")):
            metadata["identity_addressed"] = True
        return GMMessageEnvelope(
            campaign_id=resolved_campaign,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            current_message=current_message,
            is_private=is_private,
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
        context = payload.get("astrbot_context")
        is_private = trusted_flag(payload.get("is_private")) or (
            isinstance(context, dict)
            and trusted_flag(context.get("is_private"))
        )
        anonymous = is_private and trusted_flag(payload.get("anonymous"))
        if is_private:
            metadata["is_private"] = True
        if anonymous:
            metadata["anonymous"] = True
        if payload.get("message_id") and not anonymous:
            metadata["message_id"] = str(payload["message_id"])
        if payload.get("speaker_id") and not anonymous:
            metadata["speaker_id"] = str(payload["speaker_id"])
        if trusted_flag(payload.get("is_at_bot")):
            metadata["is_at_bot"] = True
        if trusted_flag(payload.get("is_reply_to_bot")):
            metadata["is_reply_to_bot"] = True
        if trusted_flag(payload.get("force_gm_reply")):
            metadata["force_gm_reply"] = True
        quoted = cls.quoted_message(payload)
        if anonymous and quoted:
            quoted.pop("sender_id", None)
        if quoted:
            metadata["quoted_message"] = quoted
        if isinstance(context, dict) and context:
            hidden_fields = (
                {"sender_id", "sender_name", "group_id", "self_id", "mentions"}
                if anonymous
                else set()
            )
            compact = {
                key: value
                for key, value in context.items()
                if key in _ASTRBOT_CONTEXT_FIELDS
                and key not in hidden_fields
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
        current_transport = {
            key: str(metadata.get(key) or "")
            for key in ("message_id", "speaker_id")
            if str(metadata.get(key) or "").strip()
        }
        if current_transport:
            result["current_transport_message"] = current_transport
        conversation_anchor = metadata.get("conversation_anchor")
        if isinstance(conversation_anchor, dict) and conversation_anchor:
            allowed_anchor_fields = (
                "anchor_id",
                "kind",
                "status",
                "question",
                "accepted_action",
                "interpretation",
                "blocking",
                "player_visible",
            )
            result["conversation_anchor"] = {
                key: conversation_anchor.get(key)
                for key in allowed_anchor_fields
                if conversation_anchor.get(key) not in (None, "")
            }
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
        recent = metadata.get("recent_message_delivery_context")
        if isinstance(recent, list):
            result["recent_message_delivery_context"] = [
                {
                    key: item.get(key)
                    for key in (
                        "message_id",
                        "speaker",
                        "speaker_id",
                        "is_current",
                    )
                    if item.get(key) not in (None, "", [], {})
                }
                for item in recent[-8:]
                if isinstance(item, dict)
                and str(item.get("message_id") or "").strip()
            ]
        current_turn = metadata.get("current_turn_events")
        if isinstance(current_turn, list) and current_turn:
            result["current_turn"] = {
                "turn_id": str(metadata.get("conversation_turn_id") or ""),
                "message_count": len(current_turn),
                "events": [
                    {
                        key: item.get(key)
                        for key in (
                            "event_id",
                            "message_id",
                            "speaker",
                            "speaker_id",
                            "text",
                            "created_at",
                            "is_private",
                            "is_at_gm",
                            "is_reply_to_gm",
                            "quoted_message_id",
                            "quoted_text",
                        )
                        if item.get(key) not in (None, "")
                    }
                    for item in current_turn
                    if isinstance(item, dict)
                ],
            }
        recent_private = metadata.get("recent_private_messages")
        recent_public = metadata.get("recent_public_messages")
        if isinstance(recent_private, list) and recent_private:
            result["recent_messages"] = [
                dict(item)
                for item in recent_private[-12:]
                if isinstance(item, dict)
            ]
            result["recent_messages_visibility"] = "private_thread"
        elif isinstance(recent_public, list):
            result["recent_messages"] = [
                dict(item)
                for item in recent_public[-12:]
                if isinstance(item, dict)
            ]
        if str(metadata.get("batch_parent_id") or "").strip():
            result["buffered_batch"] = {
                "batch_id": str(metadata.get("batch_parent_id") or ""),
                "index": int(metadata.get("batch_index") or 0),
                "count": int(metadata.get("batch_count") or 0),
                "has_later_messages": bool(
                    metadata.get("batch_has_later_messages")
                ),
            }
        return result
