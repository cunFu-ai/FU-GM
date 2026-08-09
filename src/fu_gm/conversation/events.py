from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class MessageEvent:
    """A stable, platform-neutral representation of one incoming message."""

    event_id: str
    campaign_id: str
    session_id: str
    channel_id: str
    message_id: str
    speaker: str
    speaker_id: str
    text: str
    created_at: str
    is_private: bool = False
    is_group: bool = True
    is_at_gm: bool = False
    is_reply_to_gm: bool = False
    is_named_gm: bool = False
    quoted_message_id: str = ""
    quoted_sender_id: str = ""
    quoted_text: str = ""
    batch_id: str = ""
    batch_parent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MessageEvent":
        """Restore one persisted transport event without regenerating its id."""

        return cls(
            event_id=str(payload.get("event_id") or ""),
            campaign_id=str(payload.get("campaign_id") or "default"),
            session_id=str(payload.get("session_id") or "default"),
            channel_id=str(payload.get("channel_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            speaker=str(payload.get("speaker") or "玩家"),
            speaker_id=str(payload.get("speaker_id") or ""),
            text=str(payload.get("text") or ""),
            created_at=str(
                payload.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            is_private=bool(payload.get("is_private", False)),
            is_group=bool(payload.get("is_group", True)),
            is_at_gm=bool(payload.get("is_at_gm", False)),
            is_reply_to_gm=bool(payload.get("is_reply_to_gm", False)),
            is_named_gm=bool(payload.get("is_named_gm", False)),
            quoted_message_id=str(payload.get("quoted_message_id") or ""),
            quoted_sender_id=str(payload.get("quoted_sender_id") or ""),
            quoted_text=str(payload.get("quoted_text") or ""),
            batch_id=str(payload.get("batch_id") or ""),
            batch_parent_id=str(payload.get("batch_parent_id") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        campaign_id: str | None = None,
        session_id: str | None = None,
        channel_id: str | None = None,
        text: str | None = None,
    ) -> "MessageEvent":
        resolved_campaign = str(campaign_id or payload.get("campaign_id") or "default")
        resolved_session = str(session_id or payload.get("session_id") or "default")
        resolved_channel = str(channel_id if channel_id is not None else payload.get("channel_id") or "")
        message_id = str(payload.get("message_id") or "").strip()
        speaker = str(payload.get("speaker") or payload.get("user_name") or "玩家").strip() or "玩家"
        speaker_id = str(payload.get("speaker_id") or "").strip()
        content = str(payload.get("message") if text is None else text)
        is_private = bool(payload.get("is_private", False))
        quoted = cls._quoted_message(payload)
        created_at = cls._created_at(payload)
        event_id = cls._event_id(
            campaign_id=resolved_campaign,
            session_id=resolved_session,
            channel_id=resolved_channel,
            message_id=message_id,
            speaker=speaker,
            speaker_id=speaker_id,
            text=content,
            created_at=created_at,
            batch_parent_id=str(payload.get("batch_parent_id") or ""),
        )
        return cls(
            event_id=event_id,
            campaign_id=resolved_campaign,
            session_id=resolved_session,
            channel_id=resolved_channel,
            message_id=message_id,
            speaker=speaker,
            speaker_id=speaker_id,
            text=content,
            created_at=created_at,
            is_private=is_private,
            is_group=not is_private,
            is_at_gm=bool(payload.get("is_at_bot")),
            is_reply_to_gm=bool(payload.get("is_reply_to_bot")),
            is_named_gm=bool(payload.get("force_gm_reply")),
            quoted_message_id=str(quoted.get("message_id") or ""),
            quoted_sender_id=str(quoted.get("sender_id") or ""),
            quoted_text=str(quoted.get("text") or "")[:800],
            batch_id=str(payload.get("batch_id") or ""),
            batch_parent_id=str(payload.get("batch_parent_id") or ""),
            metadata=cls._metadata(payload),
        )

    @property
    def scope_key(self) -> str:
        return "::".join((self.campaign_id, self.session_id, self.channel_id))

    @property
    def directly_addresses_gm(self) -> bool:
        return self.is_at_gm or self.is_reply_to_gm or self.is_named_gm

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def for_campaign(self, campaign_id: str) -> "MessageEvent":
        """Re-home a transport event after its original campaign was deleted."""

        clean_campaign = str(campaign_id or "default").strip() or "default"
        event_id = self._event_id(
            campaign_id=clean_campaign,
            session_id=self.session_id,
            channel_id=self.channel_id,
            message_id=self.message_id,
            speaker=self.speaker,
            speaker_id=self.speaker_id,
            text=self.text,
            created_at=self.created_at,
            batch_parent_id=self.batch_parent_id,
        )
        return replace(self, campaign_id=clean_campaign, event_id=event_id)

    @staticmethod
    def _quoted_message(payload: dict[str, Any]) -> dict[str, Any]:
        quoted = payload.get("quoted_message")
        if isinstance(quoted, dict):
            return quoted
        context = payload.get("astrbot_context")
        if isinstance(context, dict) and isinstance(context.get("quoted_message"), dict):
            return context["quoted_message"]
        return {}

    @staticmethod
    def _created_at(payload: dict[str, Any]) -> str:
        raw = payload.get("received_at") or payload.get("timestamp")
        if raw in (None, ""):
            context = payload.get("astrbot_context")
            if isinstance(context, dict):
                raw = context.get("timestamp")
        if raw not in (None, ""):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                text = str(raw).strip()
                if text:
                    return text
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _event_id(
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        message_id: str,
        speaker: str,
        speaker_id: str,
        text: str,
        created_at: str,
        batch_parent_id: str,
    ) -> str:
        if message_id:
            return f"message:{campaign_id}:{channel_id or session_id}:{message_id}"
        source = "\n".join(
            (
                campaign_id,
                session_id,
                channel_id,
                speaker_id or speaker,
                text,
                created_at,
                batch_parent_id,
            )
        )
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
        return f"event:{digest}"

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key in ("segment_types", "mentions", "attachments", "raw_message"):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                metadata[key] = value
        context = payload.get("astrbot_context")
        if isinstance(context, dict):
            for key in ("platform", "group_id", "self_id", "segment_types", "mentions", "attachments"):
                value = context.get(key)
                if value not in (None, "", [], {}) and key not in metadata:
                    metadata[key] = value
        return metadata


@dataclass(frozen=True)
class ConversationTurn:
    """One semantic table transaction containing one or more raw messages.

    A debounce window is a delivery optimization, not permission to merge
    speakers.  Every event therefore keeps its own identity and exact text,
    while the GM makes one decision for the whole chronological turn.
    """

    turn_id: str
    events: tuple[MessageEvent, ...]

    @classmethod
    def from_events(
        cls,
        events: Iterable[MessageEvent],
        *,
        turn_id: str = "",
    ) -> "ConversationTurn":
        ordered = tuple(events)
        if not ordered:
            raise ValueError("ConversationTurn requires at least one event.")
        clean_turn_id = str(turn_id or "").strip()
        if not clean_turn_id:
            source = "\n".join(event.event_id for event in ordered)
            clean_turn_id = "turn:" + hashlib.sha256(
                source.encode("utf-8")
            ).hexdigest()[:20]
        return cls(turn_id=clean_turn_id, events=ordered)

    @property
    def primary_event(self) -> MessageEvent:
        """Use the newest event only as the delivery anchor, never as author of
        the other events in this turn.
        """

        return self.events[-1]

    @property
    def directly_addresses_gm(self) -> bool:
        return any(event.directly_addresses_gm for event in self.events)

    def event(self, event_id: str) -> MessageEvent | None:
        clean_id = str(event_id or "").strip()
        return next(
            (event for event in self.events if event.event_id == clean_id),
            None,
        )

    def to_model_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "message_count": len(self.events),
            "events": [
                {
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "speaker": event.speaker,
                    "speaker_id": event.speaker_id,
                    "text": event.text,
                    "created_at": event.created_at,
                    "directly_addresses_gm": event.directly_addresses_gm,
                    "is_private": event.is_private,
                    "quoted_message_id": event.quoted_message_id,
                    "quoted_text": event.quoted_text,
                }
                for event in self.events
            ],
        }
