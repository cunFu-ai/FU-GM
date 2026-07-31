from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from fu_gm.conversation.events import MessageEvent


@dataclass(frozen=True)
class SpeechIntent:
    """Structured intent passed between conversation policy and expression."""

    act: str
    reason: str = ""
    tone: str = "自然、简洁"
    target_message_id: str = ""
    target_speaker: str = ""
    must_reply: bool = False
    can_be_silent: bool = True
    max_sentences: int = 3
    include_facts: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "SpeechIntent | None":
        if not isinstance(payload, dict) or not str(payload.get("act") or "").strip():
            return None
        return cls(
            act=str(payload.get("act") or "reply"),
            reason=str(payload.get("reason") or ""),
            tone=str(payload.get("tone") or "自然、简洁"),
            target_message_id=str(payload.get("target_message_id") or ""),
            target_speaker=str(payload.get("target_speaker") or ""),
            must_reply=bool(payload.get("must_reply", False)),
            can_be_silent=bool(payload.get("can_be_silent", True)),
            max_sentences=max(1, int(payload.get("max_sentences") or 3)),
            include_facts=tuple(str(item) for item in payload.get("include_facts") or ()),
            avoid=tuple(str(item) for item in payload.get("avoid") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["include_facts"] = list(self.include_facts)
        data["avoid"] = list(self.avoid)
        return data


@dataclass(frozen=True)
class ReplyEnvelope:
    """A visible reply plus the exact message it belongs to."""

    envelope_id: str
    campaign_id: str
    session_id: str
    channel_id: str
    target_event_id: str
    target_message_id: str
    target_speaker: str
    target_speaker_id: str
    text: str
    created_at: str
    quote: bool = False
    kind: str = "gm_reply"
    intent: SpeechIntent | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReplyEnvelope":
        return cls(
            envelope_id=str(payload.get("envelope_id") or ""),
            campaign_id=str(payload.get("campaign_id") or "default"),
            session_id=str(payload.get("session_id") or "default"),
            channel_id=str(payload.get("channel_id") or ""),
            target_event_id=str(payload.get("target_event_id") or ""),
            target_message_id=str(payload.get("target_message_id") or ""),
            target_speaker=str(payload.get("target_speaker") or ""),
            target_speaker_id=str(payload.get("target_speaker_id") or ""),
            text=str(payload.get("text") or ""),
            created_at=str(
                payload.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            quote=bool(payload.get("quote", False)),
            kind=str(payload.get("kind") or "gm_reply"),
            intent=SpeechIntent.from_dict(payload.get("intent")),
            metadata=dict(payload.get("metadata") or {}),
        )

    @classmethod
    def create(
        cls,
        event: MessageEvent,
        text: str,
        *,
        kind: str = "gm_reply",
        intent: SpeechIntent | None = None,
        quote: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ReplyEnvelope":
        normalized_text = str(text or "").strip()
        effective_quote = bool(event.message_id and event.is_group) if quote is None else bool(quote)
        return cls(
            envelope_id=f"reply:{uuid.uuid4().hex}",
            campaign_id=event.campaign_id,
            session_id=event.session_id,
            channel_id=event.channel_id,
            target_event_id=event.event_id,
            target_message_id=event.message_id,
            target_speaker=event.speaker,
            target_speaker_id=event.speaker_id,
            text=normalized_text,
            created_at=datetime.now(timezone.utc).isoformat(),
            quote=effective_quote,
            kind=kind,
            intent=intent,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def proactive(
        cls,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        text: str,
        kind: str = "gm_proactive",
        intent: SpeechIntent | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ReplyEnvelope":
        """Create an unquoted GM message that is not a reply to one player."""

        return cls(
            envelope_id=f"reply:{uuid.uuid4().hex}",
            campaign_id=str(campaign_id or "default"),
            session_id=str(session_id or "default"),
            channel_id=str(channel_id or ""),
            target_event_id="",
            target_message_id="",
            target_speaker="",
            target_speaker_id="",
            text=str(text or "").strip(),
            created_at=datetime.now(timezone.utc).isoformat(),
            quote=False,
            kind=kind,
            intent=intent,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.intent is not None:
            data["intent"] = self.intent.to_dict()
        return data
