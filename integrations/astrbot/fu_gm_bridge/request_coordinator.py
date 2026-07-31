from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar


T = TypeVar("T")


@dataclass
class _RequestLane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ChannelRequestCoordinator:
    """Serialize stateful FU-GM requests per chat channel.

    AstrBot may dispatch several handlers concurrently.  Keeping one short-lived
    lane per channel prevents a late natural-language turn from racing a save,
    load, session transition, or another turn.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, _RequestLane] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        clean_key = str(key or "").strip() or "__global__"
        lane = self._lanes.setdefault(clean_key, _RequestLane())
        lane.users += 1
        try:
            async with lane.lock:
                return await factory()
        finally:
            lane.users -= 1
            if lane.users == 0 and not lane.lock.locked():
                if self._lanes.get(clean_key) is lane:
                    self._lanes.pop(clean_key, None)

    def lane_count(self) -> int:
        return len(self._lanes)
