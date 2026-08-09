from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from fu_gm.conversation.events import MessageEvent


@dataclass(frozen=True)
class DeliveryIntent:
    """How one visible GM message should appear on the chat platform.

    This is deliberately separate from ``ReplyEnvelope.target_message_id``.
    The target field records which incoming event caused the GM transaction;
    it does not imply that QQ should render a quoted reply.
    """

    mode: str = "normal"
    quote_message_id: str = ""
    mention_user_ids: tuple[str, ...] = ()
    semantic_targets: tuple[str, ...] = ()
    reason: str = ""
    confidence: float = 1.0
    downgraded_from: str = ""

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any] | None,
        *,
        legacy_quote: bool = False,
        legacy_target_message_id: str = "",
    ) -> "DeliveryIntent":
        if not isinstance(payload, dict):
            if legacy_quote and str(legacy_target_message_id or "").strip():
                return cls(
                    mode="quote_reply",
                    quote_message_id=str(legacy_target_message_id).strip(),
                    reason="兼容旧版引用回复信封。",
                )
            return cls()
        mode = str(payload.get("mode") or "normal").strip().lower()
        if mode not in {"normal", "quote_reply", "mention"}:
            mode = "normal"
        try:
            confidence = float(payload.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        raw_mentions = payload.get("mention_user_ids")
        mention_values = raw_mentions if isinstance(raw_mentions, (list, tuple)) else ()
        raw_targets = payload.get("semantic_targets")
        target_values = raw_targets if isinstance(raw_targets, (list, tuple)) else ()
        return cls(
            mode=mode,
            quote_message_id=str(payload.get("quote_message_id") or "").strip(),
            mention_user_ids=tuple(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in mention_values
                    if str(item or "").strip()
                )
            ),
            semantic_targets=tuple(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in target_values
                    if str(item or "").strip()
                )
            ),
            reason=str(payload.get("reason") or "").strip(),
            confidence=confidence,
            downgraded_from=str(payload.get("downgraded_from") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "quote_message_id": self.quote_message_id,
            "mention_user_ids": list(self.mention_user_ids),
            "semantic_targets": list(self.semantic_targets),
            "reason": self.reason,
            "confidence": self.confidence,
            "downgraded_from": self.downgraded_from,
        }


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
    """A visible reply, its causal event, and its independent delivery style."""

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
    delivery: DeliveryIntent = field(default_factory=DeliveryIntent)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReplyEnvelope":
        target_message_id = str(payload.get("target_message_id") or "")
        delivery = DeliveryIntent.from_dict(
            payload.get("delivery"),
            legacy_quote=bool(payload.get("quote", False)),
            legacy_target_message_id=target_message_id,
        )
        return cls(
            envelope_id=str(payload.get("envelope_id") or ""),
            campaign_id=str(payload.get("campaign_id") or "default"),
            session_id=str(payload.get("session_id") or "default"),
            channel_id=str(payload.get("channel_id") or ""),
            target_event_id=str(payload.get("target_event_id") or ""),
            target_message_id=target_message_id,
            target_speaker=str(payload.get("target_speaker") or ""),
            target_speaker_id=str(payload.get("target_speaker_id") or ""),
            text=str(payload.get("text") or ""),
            created_at=str(
                payload.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            quote=delivery.mode == "quote_reply" and bool(delivery.quote_message_id),
            kind=str(payload.get("kind") or "gm_reply"),
            intent=SpeechIntent.from_dict(payload.get("intent")),
            delivery=delivery,
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
        delivery: DeliveryIntent | dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ReplyEnvelope":
        normalized_text = str(text or "").strip()
        if isinstance(delivery, DeliveryIntent):
            effective_delivery = delivery
        elif isinstance(delivery, dict):
            effective_delivery = DeliveryIntent.from_dict(delivery)
        elif quote is True and event.message_id and event.is_group:
            effective_delivery = DeliveryIntent(
                mode="quote_reply",
                quote_message_id=event.message_id,
                reason="调用方显式要求引用触发消息。",
            )
        else:
            effective_delivery = DeliveryIntent()
        effective_quote = bool(
            event.is_group
            and effective_delivery.mode == "quote_reply"
            and effective_delivery.quote_message_id
        )
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
            delivery=effective_delivery,
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
            delivery=DeliveryIntent(
                mode="normal",
                reason="GM主动消息不引用玩家消息。",
            ),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.intent is not None:
            data["intent"] = self.intent.to_dict()
        data["delivery"] = self.delivery.to_dict()
        return data
