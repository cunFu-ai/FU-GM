from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


def has_meaningful_message_activity(
    message: object,
    astrbot_context: dict[str, Any] | None = None,
) -> bool:
    """Return whether one AstrBot event carries actual conversational content.

    AstrBot may dispatch empty adapter events for notices or duplicated transport
    callbacks.  Those events must not advance FU-GM's freshness watermark and
    cancel an in-flight player request.  This check only inspects normalized
    platform structure; it does not classify the meaning of the player's text.
    """

    if str(message or "").strip():
        return True
    context = astrbot_context if isinstance(astrbot_context, dict) else {}
    if bool(context.get("is_at_bot") or context.get("is_reply_to_bot")):
        return True
    if any(
        isinstance(item, dict)
        and bool(str(item.get("type") or "").strip() or str(item.get("file") or "").strip())
        for item in list(context.get("attachments") or [])
    ):
        return True
    return False


@dataclass
class QueuedMessage:
    """等待合并的一条群聊消息。"""

    speaker: str
    message: str
    payload: dict[str, Any]
    timestamp: float


@dataclass
class BufferedPayload:
    """缓冲结束后交给 FU-GM 的合并输入。"""

    batch_id: str
    key: str
    payload: dict[str, Any]
    messages: list[QueuedMessage]


@dataclass
class _PendingBatch:
    key: str
    batch_id: str
    created_at: float
    last_update: float
    messages: list[QueuedMessage] = field(default_factory=list)
    updated: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[BufferedPayload | None] | None = None
    cancelled: bool = False


class DebouncedMessageBuffer:
    """把同一跑团会话里的连续发言合并成一个输入批次。

    第一个进入批次的调用者会等待缓冲结束并拿到合并 payload；后续调用者只负责
    追加消息并立即返回 None。这样 AstrBot 只会由第一条消息所在的 handler 发出
    一次最终回复。
    """

    def __init__(
        self,
        *,
        debounce_seconds: float = 3.0,
        max_wait_seconds: float = 12.0,
        max_messages: int = 5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if debounce_seconds <= 0:
            raise ValueError("debounce_seconds 必须大于 0。")
        if max_wait_seconds < debounce_seconds:
            raise ValueError("max_wait_seconds 不能小于 debounce_seconds。")
        if max_messages < 1:
            raise ValueError("max_messages 必须至少为 1。")
        self.debounce_seconds = debounce_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_messages = max_messages
        self._clock = clock or time.monotonic
        self._pending: dict[str, _PendingBatch] = {}

    async def add(self, key: str, payload: dict[str, Any]) -> BufferedPayload | None:
        """加入消息；只有批次创建者会等待并返回合并结果。"""

        now = self._clock()
        message = QueuedMessage(
            speaker=str(payload.get("speaker") or "玩家"),
            message=str(payload.get("message") or ""),
            payload=dict(payload),
            timestamp=now,
        )
        pending = self._pending.get(key)
        if pending is not None:
            pending.messages.append(message)
            pending.last_update = now
            pending.updated.set()
            return None

        batch = _PendingBatch(
            key=key,
            batch_id=uuid.uuid4().hex,
            created_at=now,
            last_update=now,
            messages=[message],
        )
        self._pending[key] = batch
        batch.task = asyncio.create_task(self._wait_and_collapse(batch))
        return await batch.task

    def pending_count(self, key: str) -> int:
        pending = self._pending.get(key)
        return len(pending.messages) if pending else 0

    def discard(self, key: str) -> bool:
        """丢弃尚未提交的旧被动批次，让新的直接消息优先。"""

        pending = self._pending.pop(key, None)
        if pending is None:
            return False
        pending.cancelled = True
        pending.updated.set()
        return True

    async def _wait_and_collapse(
        self,
        batch: _PendingBatch,
    ) -> BufferedPayload | None:
        try:
            while True:
                if batch.cancelled:
                    return None
                if len(batch.messages) >= self.max_messages:
                    break
                now = self._clock()
                quiet_remaining = self.debounce_seconds - (now - batch.last_update)
                max_remaining = self.max_wait_seconds - (now - batch.created_at)
                if quiet_remaining <= 0 or max_remaining <= 0:
                    break
                timeout = min(quiet_remaining, max_remaining)
                try:
                    await asyncio.wait_for(batch.updated.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                batch.updated.clear()
        finally:
            if self._pending.get(batch.key) is batch:
                self._pending.pop(batch.key, None)

        if batch.cancelled:
            return None

        return BufferedPayload(
            batch_id=batch.batch_id,
            key=batch.key,
            payload=self._collapse_payload(batch),
            messages=list(batch.messages),
        )

    def _collapse_payload(self, batch: _PendingBatch) -> dict[str, Any]:
        first = batch.messages[0]
        payload = dict(first.payload)
        payload["batch_id"] = batch.batch_id
        payload["batch_count"] = len(batch.messages)
        activity_members = [
            {
                "speaker": str(item.payload.get("speaker") or item.speaker),
                "speaker_id": str(item.payload.get("speaker_id") or ""),
                "activity_version": item.payload.get("activity_version"),
                "message_id": str(item.payload.get("message_id") or ""),
            }
            for item in batch.messages
        ]
        payload["activity_members"] = activity_members
        activity_versions = [
            int(item["activity_version"])
            for item in activity_members
            if item.get("activity_version") not in (None, "")
        ]
        if activity_versions:
            payload["activity_version"] = max(activity_versions)
        payload["activity_token"] = f"batch:{batch.batch_id}"
        payload["batch_messages"] = [
            {
                "speaker": item.speaker,
                "message": item.message,
                "timestamp": item.timestamp,
                "payload": dict(item.payload),
            }
            for item in batch.messages
        ]
        if len(batch.messages) == 1:
            return payload

        lines = ["以下是同一跑团会话中连续出现的群聊发言；每条发言保留各自行动者："]
        for index, item in enumerate(batch.messages, start=1):
            lines.append(f"{index}. {item.speaker}：{item.message}")
        payload["message"] = "\n".join(lines)
        payload["speaker"] = "多人发言"
        return payload
