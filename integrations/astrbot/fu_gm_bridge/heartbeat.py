from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any


def heartbeat_committed_state_change(response: dict[str, Any]) -> bool:
    """Return whether dropping this response would hide a committed GM move."""

    return any(
        isinstance(item, dict)
        and bool(item.get("ok"))
        and bool(item.get("state_changed"))
        for item in list(response.get("tool_receipts") or [])
    )


class HeartbeatDeliveryJournal:
    """Persist proactive messages sent to QQ but not yet confirmed upstream."""

    def __init__(self, path: str | Path, *, max_entries: int = 200) -> None:
        self.path = Path(path)
        self.max_entries = max(20, int(max_entries))
        self.sent: dict[str, str] = {}
        self.last_error = ""
        self._load()

    def was_sent(self, delivery_id: str) -> bool:
        return str(delivery_id or "").strip() in self.sent

    def mark_sent(self, delivery_id: str) -> bool:
        clean_id = str(delivery_id or "").strip()
        if not clean_id:
            return False
        self.sent[clean_id] = datetime.now(timezone.utc).isoformat()
        self.sent = dict(list(self.sent.items())[-self.max_entries :])
        return self._persist()

    def mark_confirmed(self, delivery_id: str) -> bool:
        clean_id = str(delivery_id or "").strip()
        if clean_id not in self.sent:
            return True
        previous = self.sent.pop(clean_id)
        if self._persist():
            return True
        self.sent[clean_id] = previous
        return False

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.sent = {
                    str(key): str(value)
                    for key, value in list(payload.items())[-self.max_entries :]
                    if str(key).strip()
                }
            self.last_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = str(exc)[:300]

    def _persist(self) -> bool:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.sent, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = str(exc)[:300]
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return False


class HeartbeatTaskRegistry:
    """Own one cancellable proactive heartbeat per chat channel."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, key: str, factory: Callable[[], Awaitable[None]]) -> bool:
        clean_key = str(key or "").strip()
        if not clean_key:
            return False
        current = self._tasks.get(clean_key)
        if current is not None and not current.done():
            return False
        task = asyncio.create_task(factory())
        self._tasks[clean_key] = task
        task.add_done_callback(lambda finished, channel=clean_key: self._cleanup(channel, finished))
        return True

    def cancel(self, key: str) -> bool:
        task = self._tasks.get(str(key or "").strip())
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def running(self, key: str) -> bool:
        task = self._tasks.get(str(key or "").strip())
        return bool(task is not None and not task.done())

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _cleanup(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
