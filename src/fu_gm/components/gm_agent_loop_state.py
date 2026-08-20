from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from fu_gm.components.gm_live_run_monitor import emit_live_run_event


class GMAgentLoopPhase(str, Enum):
    """核心 GM 智能体一次事务内的可观测阶段。"""

    CREATED = "created"
    OBSERVING_STATE = "observing_state"
    BUILDING_CONTEXT = "building_context"
    REQUESTING_MODEL = "requesting_model"
    DISPATCHING_DECISION = "dispatching_decision"
    FINALIZING_TRANSACTION = "finalizing_transaction"
    FINISHED = "finished"


class GMAgentTerminalReason(str, Enum):
    """智能体停止循环的机器可读原因。"""

    COMPLETED = "completed"
    SILENT = "silent"
    EXTERNAL = "external"
    ASK_USER = "ask_user"
    NOT_APPLICABLE = "not_applicable"
    PROVIDER_FAILURE = "provider_failure"
    DEADLINE = "deadline"
    ITERATION_EXHAUSTED = "iteration_exhausted"
    UNRESOLVED = "unresolved"
    EXCEPTION = "exception"


@dataclass(frozen=True)
class GMAgentEvent:
    sequence: int
    phase: str
    elapsed_ms: int
    iteration: int = 0
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class GMAgentLoopState:
    """一次 GM Agent 循环的集中式运行状态。

    这里只保存诊断与编排信息，不复制权威游戏状态。权威事实仍由工具事务、
    回执与存档持有；因此启用该状态对象不会改变任何跑团结算。
    """

    timeout_seconds: float
    started_monotonic: float = field(default_factory=time.monotonic)
    deadline_monotonic: float = 0.0
    phase: GMAgentLoopPhase = GMAgentLoopPhase.CREATED
    iteration: int = 0
    terminal_reason: GMAgentTerminalReason | None = None
    events: list[GMAgentEvent] = field(default_factory=list)
    phase_durations_ms: dict[str, int] = field(default_factory=dict)
    _phase_started_monotonic: float = 0.0

    def __post_init__(self) -> None:
        self.timeout_seconds = max(0.0, float(self.timeout_seconds))
        if self.deadline_monotonic <= 0:
            self.deadline_monotonic = (
                self.started_monotonic + self.timeout_seconds
            )
        self._phase_started_monotonic = self.started_monotonic
        self.record(self.phase)

    def enter(
        self,
        phase: GMAgentLoopPhase,
        *,
        iteration: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        now = time.monotonic()
        previous = self.phase.value
        elapsed = max(0, int((now - self._phase_started_monotonic) * 1000))
        self.phase_durations_ms[previous] = (
            self.phase_durations_ms.get(previous, 0) + elapsed
        )
        self.phase = phase
        self._phase_started_monotonic = now
        if iteration is not None:
            self.iteration = max(0, int(iteration))
        self.record(phase, details=details, now=now)

    def record(
        self,
        phase: GMAgentLoopPhase | str,
        *,
        details: dict[str, object] | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = time.monotonic() if now is None else now
        phase_value = phase.value if isinstance(phase, Enum) else str(phase)
        event = GMAgentEvent(
            sequence=len(self.events) + 1,
            phase=phase_value,
            elapsed_ms=max(
                0,
                int((timestamp - self.started_monotonic) * 1000),
            ),
            iteration=self.iteration,
            details=dict(details or {}),
        )
        self.events.append(event)
        emit_live_run_event(
            "agent_loop_phase",
            phase=phase_value,
            iteration=self.iteration,
            summary=self._phase_summary(phase_value),
            public_details={
                "loop_sequence": event.sequence,
                "loop_elapsed_ms": event.elapsed_ms,
                **dict(details or {}),
            },
        )

    def finish(
        self,
        reason: GMAgentTerminalReason,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        self.terminal_reason = reason
        self.enter(
            GMAgentLoopPhase.FINISHED,
            details={"terminal_reason": reason.value, **dict(details or {})},
        )

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started_monotonic) * 1000))

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "iteration": self.iteration,
            "terminal_reason": (
                self.terminal_reason.value if self.terminal_reason else ""
            ),
            "elapsed_ms": self.elapsed_ms,
            "timeout_seconds": self.timeout_seconds,
            "phase_durations_ms": dict(self.phase_durations_ms),
            "events": [asdict(event) for event in self.events],
        }

    @classmethod
    def infer_terminal_reason(cls, outcome: Any) -> GMAgentTerminalReason:
        terminal_action = str(getattr(outcome, "terminal_action", "") or "")
        if terminal_action == "silent":
            return GMAgentTerminalReason.SILENT
        if terminal_action == "external":
            return GMAgentTerminalReason.EXTERNAL
        if terminal_action == "ask_user":
            return GMAgentTerminalReason.ASK_USER
        if terminal_action == "not_applicable" or not bool(
            getattr(outcome, "handled", False)
        ):
            return GMAgentTerminalReason.NOT_APPLICABLE

        mode = str(getattr(outcome, "mode", "") or "")
        error = str(getattr(outcome, "error", "") or "").lower()
        if "deadline" in error or "timeout" in error or "超时" in error:
            return GMAgentTerminalReason.DEADLINE
        if "unavailable" in mode:
            return GMAgentTerminalReason.PROVIDER_FAILURE
        if "unresolved" in mode or "incomplete" in mode:
            if "最大次数" in error or "循环" in error:
                return GMAgentTerminalReason.ITERATION_EXHAUSTED
            return GMAgentTerminalReason.UNRESOLVED
        return GMAgentTerminalReason.COMPLETED

    @staticmethod
    def _phase_summary(phase: str) -> str:
        return {
            GMAgentLoopPhase.CREATED.value: "智能体循环已创建。",
            GMAgentLoopPhase.OBSERVING_STATE.value: "正在刷新权威状态。",
            GMAgentLoopPhase.BUILDING_CONTEXT.value: "正在构建本轮模型上下文。",
            GMAgentLoopPhase.REQUESTING_MODEL.value: "正在等待模型返回决策。",
            GMAgentLoopPhase.DISPATCHING_DECISION.value: "模型决策已返回，正在校验并分派。",
            GMAgentLoopPhase.FINALIZING_TRANSACTION.value: "正在提交或回滚整条消息事务。",
            GMAgentLoopPhase.FINISHED.value: "智能体循环已结束。",
        }.get(str(phase or ""), "智能体运行状态已更新。")
