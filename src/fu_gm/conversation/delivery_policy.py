from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from fu_gm.conversation.events import MessageEvent
from fu_gm.conversation.reply import DeliveryIntent


class DeliveryLedger(Protocol):
    def recent_events(
        self,
        campaign_id: str,
        session_id: str,
        channel_id: str = "",
        *,
        limit: int = 20,
    ) -> list[MessageEvent]: ...


class ReplyDeliveryPolicy:
    """Validate a model-proposed presentation without reclassifying prose."""

    def resolve(
        self,
        event: MessageEvent,
        proposed: DeliveryIntent | dict[str, object] | None,
        *,
        ledger: DeliveryLedger,
    ) -> DeliveryIntent:
        intent = (
            proposed
            if isinstance(proposed, DeliveryIntent)
            else DeliveryIntent.from_dict(proposed if isinstance(proposed, dict) else None)
        )
        if not event.is_group:
            return self._normal(intent, reason="私聊无需引用或艾特。")
        recent = ledger.recent_events(
            event.campaign_id,
            event.session_id,
            event.channel_id,
            limit=20,
        )
        known_message_ids = {
            item.message_id
            for item in recent
            if str(item.message_id or "").strip()
        }
        if event.message_id:
            known_message_ids.add(event.message_id)
        known_user_ids = {
            item.speaker_id
            for item in recent
            if str(item.speaker_id or "").strip()
        }
        if event.speaker_id:
            known_user_ids.add(event.speaker_id)

        if intent.mode == "quote_reply":
            quote_id = str(intent.quote_message_id or "").strip()
            if quote_id and quote_id in known_message_ids:
                return replace(
                    intent,
                    quote_message_id=quote_id,
                    mention_user_ids=(),
                    downgraded_from="",
                )
            return self._normal(
                intent,
                reason="引用目标不在当前近期消息记录中，已改为普通发送。",
                downgraded_from="quote_reply",
            )

        if intent.mode == "mention":
            valid_mentions = tuple(
                item for item in intent.mention_user_ids if item in known_user_ids
            )
            if valid_mentions:
                return replace(
                    intent,
                    quote_message_id="",
                    mention_user_ids=valid_mentions,
                    downgraded_from="",
                )
            return self._normal(
                intent,
                reason="艾特目标不在当前近期参与者记录中，已改为普通发送。",
                downgraded_from="mention",
            )

        return self._normal(intent, reason=intent.reason or "当前对话连续，普通发送即可。")

    @staticmethod
    def _normal(
        intent: DeliveryIntent,
        *,
        reason: str,
        downgraded_from: str = "",
    ) -> DeliveryIntent:
        return DeliveryIntent(
            mode="normal",
            semantic_targets=intent.semantic_targets,
            reason=str(reason or "").strip(),
            confidence=intent.confidence,
            downgraded_from=downgraded_from,
        )
