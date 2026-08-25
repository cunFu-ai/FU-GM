from __future__ import annotations

from typing import Any, Protocol


class GMBatchedMessageHost(Protocol):
    def _message_route(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class GMBatchedMessageRouter:
    """Turn a debounce batch into one semantic GM transaction.

    Raw messages remain individually attributed in ``current_turn_messages``.
    The core GM therefore sees the whole exchange once and may remain silent,
    answer once, or execute several source-bound tools without producing one
    reply per chat bubble.
    """

    def __init__(self, host: GMBatchedMessageHost) -> None:
        self.host = host

    def route(
        self,
        payload: dict[str, Any],
        raw_batch: list[object],
    ) -> dict[str, Any]:
        valid_batch = [
            raw
            for raw in raw_batch
            if isinstance(raw, dict)
            and str(raw.get("message") or "").strip()
        ]
        if not valid_batch:
            clean_payload = dict(payload)
            clean_payload.pop("batch_messages", None)
            return self.host._message_route(clean_payload)

        turn_messages = [
            self._item_payload(
                payload,
                raw,
                active_campaign_id=str(payload.get("campaign_id") or "default"),
                batch_index=batch_index,
                batch_count=len(valid_batch),
            )
            for batch_index, raw in enumerate(valid_batch, start=1)
        ]
        primary = dict(turn_messages[-1])
        primary.pop("batch_parent_id", None)
        primary.pop("batch_index", None)
        primary.pop("batch_count", None)
        primary.pop("batch_has_later_messages", None)
        primary["campaign_id"] = str(payload.get("campaign_id") or "default")
        primary["session_id"] = str(payload.get("session_id") or "default")
        primary["channel_id"] = str(payload.get("channel_id") or "")
        primary["batch_id"] = str(payload.get("batch_id") or "")
        primary["current_turn_messages"] = turn_messages
        primary["batch_count"] = len(turn_messages)
        primary["activity_members"] = [
            {
                "speaker": str(item.get("speaker") or ""),
                "speaker_id": str(item.get("speaker_id") or ""),
                "activity_version": item.get("activity_version"),
                "message_id": str(item.get("message_id") or ""),
            }
            for item in turn_messages
        ]
        activity_versions = [
            int(item.get("activity_version"))
            for item in turn_messages
            if item.get("activity_version") not in (None, "")
        ]
        if activity_versions:
            primary["activity_version"] = max(activity_versions)
        primary["activity_token"] = f"batch:{primary['batch_id']}"
        primary["turn_force_gm_reply"] = any(
            bool(item.get("force_gm_reply"))
            or bool(item.get("is_at_bot"))
            or bool(item.get("is_reply_to_bot"))
            for item in turn_messages
        )
        result = self.host._message_route(primary)
        decision = dict(result.get("decision") or {})
        decision["reason"] = str(
            decision.get("reason")
            or "缓冲消息已作为一个桌面轮次处理，并保留每位发言者身份。"
        )
        decision["tags"] = list(
            dict.fromkeys(
                [
                    *(decision.get("tags") or []),
                    "batch",
                    "single_semantic_turn",
                    "speaker_preserved",
                ]
            )
        )
        result["decision"] = decision
        result["batch_id"] = str(payload.get("batch_id") or "")
        result["batch_count"] = len(turn_messages)
        result["batch_event_ids"] = list(result.get("batch_event_ids") or [])
        return result

    @staticmethod
    def _item_payload(
        payload: dict[str, Any],
        raw: dict[str, Any],
        *,
        active_campaign_id: str,
        batch_index: int,
        batch_count: int,
    ) -> dict[str, Any]:
        item_payload = dict(payload)
        item_payload.pop("batch_messages", None)
        item_payload.pop("batch_count", None)
        nested_payload = raw.get("payload")
        if isinstance(nested_payload, dict):
            item_payload.update(nested_payload)

        item_payload["campaign_id"] = active_campaign_id
        item_payload["speaker"] = str(
            raw.get("speaker") or item_payload.get("speaker") or "玩家"
        )
        item_payload["message"] = str(raw.get("message") or "")
        item_payload["batch_parent_id"] = str(payload.get("batch_id") or "")
        item_payload["batch_index"] = int(batch_index)
        item_payload["batch_count"] = int(batch_count)
        item_payload["batch_has_later_messages"] = batch_index < batch_count
        if item_payload.get("received_at") in (None, ""):
            item_payload["received_at"] = raw.get("timestamp")
        return item_payload
