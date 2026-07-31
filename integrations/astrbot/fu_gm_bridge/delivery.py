from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


def _normalized_media(items: object) -> list[dict[str, str]]:
    media: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in list(items or []) if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        url = str(item.get("url") or "").strip()
        if media_type != "image" or not (path or url):
            continue
        identity = (media_type, path, url)
        if identity in seen:
            continue
        seen.add(identity)
        media.append(
            {
                "type": media_type,
                "path": path,
                "url": url,
                "alt": str(item.get("alt") or "图片").strip(),
            }
        )
    return media


class ReplyDeliveryCoordinator:
    """Send each reply envelope once and retry only its upstream confirmation."""

    def __init__(self, journal: Any) -> None:
        self.journal = journal

    async def recover(
        self,
        confirm: Callable[[str], Awaitable[bool]],
    ) -> list[str]:
        confirmed: list[str] = []
        for envelope_id in list(self.journal.sent):
            if await confirm(envelope_id):
                self.journal.mark_confirmed(envelope_id)
                confirmed.append(envelope_id)
        return confirmed

    async def deliver(
        self,
        specs: list[dict[str, Any]],
        results: list[Any],
        *,
        already_confirmed: bool,
        send: Callable[[Any], Awaitable[None]],
        confirm: Callable[[str], Awaitable[bool]],
    ) -> bool:
        if len(specs) != len(results):
            return False
        delivered_any = False
        for spec, result in zip(specs, results):
            envelope_id = str(spec.get("envelope_id") or "").strip()
            sent_before = bool(envelope_id and self.journal.was_sent(envelope_id))
            if not already_confirmed and not sent_before:
                try:
                    await send(result)
                except Exception:
                    return delivered_any
                delivered_any = True
                if envelope_id and not self.journal.mark_sent(envelope_id):
                    return delivered_any
            else:
                delivered_any = True
            if not envelope_id or already_confirmed:
                continue
            if await confirm(envelope_id):
                self.journal.mark_confirmed(envelope_id)
        return delivered_any


def reply_delivery_specs(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize FU-GM reply envelopes for an AstrBot adapter.

    The function stays independent from AstrBot so delivery behavior can be
    regression-tested even when AstrBot is not installed in the test venv.
    """

    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    top_level_media = _normalized_media(response.get("reply_media"))
    raw_envelopes = response.get("reply_envelopes")
    if isinstance(raw_envelopes, list):
        for index, item in enumerate(raw_envelopes):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            envelope_media = _normalized_media(metadata.get("reply_media"))
            media = envelope_media or (top_level_media if index == 0 else [])
            if not text and not media:
                continue
            envelope_id = str(item.get("envelope_id") or "")
            identity = envelope_id or "::".join(
                (
                    str(item.get("target_message_id") or ""),
                    text,
                    repr(media),
                )
            )
            if identity in seen:
                continue
            seen.add(identity)
            target_message_id = str(item.get("target_message_id") or "").strip()
            specs.append(
                {
                    "envelope_id": envelope_id,
                    "text": text,
                    "quote": bool(item.get("quote")) and bool(target_message_id),
                    "target_message_id": target_message_id,
                    "target_speaker": str(item.get("target_speaker") or ""),
                    "kind": str(item.get("kind") or "gm_reply"),
                    "media": media,
                }
            )
    if specs:
        return specs

    fallback = str(response.get("reply") or response.get("message") or "").strip()
    if fallback or top_level_media:
        return [
            {
                "envelope_id": "",
                "text": fallback,
                "quote": False,
                "target_message_id": "",
                "target_speaker": "",
                "kind": "gm_reply",
                "media": top_level_media,
            }
        ]
    return []
