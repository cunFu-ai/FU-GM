from __future__ import annotations

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
