from __future__ import annotations

from typing import Any, Protocol


class GMBatchedMessageHost(Protocol):
    def _message_route(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class GMBatchedMessageRouter:
    """Route buffered group messages without merging their semantic actors.

    AstrBot may debounce several messages to reduce reply chatter, but every
    original utterance remains an independent GM-agent transaction. Only the
    outgoing reply envelopes are combined. A campaign switch is inherited by
    later items only after a successful backend receipt confirms it.
    """

    def __init__(self, host: GMBatchedMessageHost) -> None:
        self.host = host

    def route(
        self,
        payload: dict[str, Any],
        raw_batch: list[object],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        initial_campaign_id = str(payload.get("campaign_id") or "default")
        active_campaign_id = initial_campaign_id
        active_campaign_speaker_id = ""
        active_campaign_speaker = ""

        for raw in raw_batch:
            if not isinstance(raw, dict):
                continue
            item_payload = self._item_payload(
                payload,
                raw,
                active_campaign_id=active_campaign_id,
            )
            if not str(item_payload.get("message") or "").strip():
                continue
            result = self.host._message_route(item_payload)
            results.append(result)
            confirmed_campaign_id = str(
                result.get("active_campaign_id") or ""
            ).strip()
            if confirmed_campaign_id and confirmed_campaign_id != active_campaign_id:
                active_campaign_id = confirmed_campaign_id
                active_campaign_speaker_id = str(
                    item_payload.get("speaker_id") or ""
                ).strip()
                active_campaign_speaker = str(
                    item_payload.get("speaker") or ""
                ).strip()

        return self._combined_response(
            payload,
            results,
            initial_campaign_id=initial_campaign_id,
            active_campaign_id=active_campaign_id,
            active_campaign_speaker_id=active_campaign_speaker_id,
            active_campaign_speaker=active_campaign_speaker,
        )

    @staticmethod
    def _item_payload(
        payload: dict[str, Any],
        raw: dict[str, Any],
        *,
        active_campaign_id: str,
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
        if item_payload.get("received_at") in (None, ""):
            item_payload["received_at"] = raw.get("timestamp")
        return item_payload

    @staticmethod
    def _combined_response(
        payload: dict[str, Any],
        results: list[dict[str, Any]],
        *,
        initial_campaign_id: str,
        active_campaign_id: str,
        active_campaign_speaker_id: str = "",
        active_campaign_speaker: str = "",
    ) -> dict[str, Any]:
        reply_envelopes = [
            envelope
            for result in results
            for envelope in (result.get("reply_envelopes") or [])
            if isinstance(envelope, dict)
            and str(envelope.get("text") or "").strip()
        ]
        replies = [
            str(envelope.get("text") or "").strip()
            for envelope in reply_envelopes
        ]
        any_fu_gm = any(result.get("target") == "fu_gm" for result in results)
        any_silent = any(result.get("target") == "silent" for result in results)
        routes = [str(result.get("route") or "") for result in results]
        route = next(
            (
                candidate
                for candidate in (
                    "game",
                    "session_zero",
                    "pre_session",
                    "safety",
                    "casual",
                )
                if candidate in routes
            ),
            "",
        )
        winner = next(
            (
                result
                for result in results
                if result.get("target") == "fu_gm"
                and (not route or result.get("route") == route)
            ),
            next(
                (result for result in results if result.get("target") == "silent"),
                results[0] if results else {},
            ),
        )
        winner_decision = dict(winner.get("decision") or {})
        winner_decision["reason"] = "缓冲消息已逐条保留发言者身份并依次路由。"
        winner_decision["tags"] = list(
            dict.fromkeys(
                [
                    *(winner_decision.get("tags") or []),
                    "batch",
                    "speaker_preserved",
                ]
            )
        )
        return {
            "ok": all(bool(result.get("ok", True)) for result in results),
            "campaign_id": initial_campaign_id,
            "active_campaign_id": active_campaign_id,
            "active_campaign_speaker_id": active_campaign_speaker_id,
            "active_campaign_speaker": active_campaign_speaker,
            "session_id": str(payload.get("session_id") or "default"),
            "target": (
                "fu_gm"
                if any_fu_gm
                else "silent"
                if any_silent
                else "astrbot"
            ),
            "route": route,
            "send_reply": bool(replies),
            "stop_astrbot": any_fu_gm or any_silent,
            "reply": "\n".join(replies),
            "reply_envelopes": reply_envelopes,
            "batch_id": str(payload.get("batch_id") or ""),
            "batch_results": results,
            "decision": winner_decision,
        }
