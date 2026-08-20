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


class ChannelTurnGate:
    """按聊天频道串行提交并投递一个完整 FU-GM 回合。

    ``factory`` 的边界必须覆盖后端请求、权威状态提交、平台发送和送达确认。
    这样下一条消息不会在上一条回复尚未真正出现在群里时读取到一条更靠后的
    时间线。调用方也可以继续只包住 HTTP 请求，以兼容旧命令处理器。
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


# 保留旧名称，避免已安装的 AstrBot 插件副本和第三方扩展立即失效。
ChannelRequestCoordinator = ChannelTurnGate
