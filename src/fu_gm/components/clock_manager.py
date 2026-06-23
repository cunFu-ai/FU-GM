from __future__ import annotations

from fu_gm.models import Clock


class ClockManager:
    def __init__(self) -> None:
        self._clocks: dict[str, Clock] = {}

    def add(self, clock: Clock) -> None:
        self._clocks[clock.name] = clock

    def get(self, name: str) -> Clock:
        return self._clocks[self._resolve_name(name)]

    def exists(self, name: str) -> bool:
        return self._resolve_name(name) in self._clocks

    def advance(self, name: str, delta: int) -> tuple[int, int]:
        clock = self.get(name)
        before = clock.current
        clock.current = max(0, min(clock.max_segments, clock.current + delta))
        return before, clock.current

    def formatted(self) -> list[str]:
        return [f"[{clock.name}] {clock.current}/{clock.max_segments}" for clock in self._clocks.values()]

    def all(self) -> list[Clock]:
        """返回当前所有命刻的只读快照入口。"""

        return list(self._clocks.values())

    def _resolve_name(self, name: str) -> str:
        text = str(name or "").strip()
        if text in self._clocks:
            return text
        # LLM 常会从面板里复制 "[命刻名] 0/6" 或 "【命刻名】"；这里统一还原成真实键名。
        bracket_pairs = (("[", "]"), ("【", "】"))
        for left, right in bracket_pairs:
            if text.startswith(left) and right in text:
                candidate = text[len(left) : text.index(right)].strip()
                if candidate in self._clocks:
                    return candidate
        if " " in text:
            candidate = text.split(" ", 1)[0].strip()
            for left, right in bracket_pairs:
                if candidate.startswith(left) and candidate.endswith(right):
                    candidate = candidate[len(left) : -len(right)].strip()
                    break
            if candidate in self._clocks:
                return candidate
        return text
