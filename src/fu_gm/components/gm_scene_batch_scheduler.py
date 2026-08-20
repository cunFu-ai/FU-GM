from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class GMSceneBatchSchedule:
    """一次工具批次经过场景边界排序后的执行计划。"""

    calls: tuple[dict[str, object], ...]
    reordered: bool = False
    reason: str = ""
    original_order: tuple[str, ...] = ()
    execution_order: tuple[str, ...] = ()


class GMSceneBatchScheduler:
    """保留模型显式提交的批次顺序。

    排序器只返回脱离原输入的副本。调用之间的真实依赖由工具回执中的
    ``required_followup_calls``和硬规则前置条件表达，编排层不再维护另一套
    消息级条款依赖。
    """

    @classmethod
    def schedule(
        cls,
        calls: Iterable[dict[str, object]],
        *,
        observed_state: dict[str, object],
    ) -> GMSceneBatchSchedule:
        del observed_state
        clean_calls = [dict(call) for call in calls if isinstance(call, dict)]
        order = tuple(cls._tool_name(call) for call in clean_calls)
        return GMSceneBatchSchedule(
            calls=tuple(clean_calls),
            original_order=order,
            execution_order=order,
        )

    @staticmethod
    def _tool_name(call: dict[str, object]) -> str:
        return str(call.get("tool_name") or "").strip()
