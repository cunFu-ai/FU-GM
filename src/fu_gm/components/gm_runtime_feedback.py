from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Type, TypeVar


MAX_RUNTIME_FEEDBACK_CHARS = 4096
MAX_RUNTIME_FEEDBACK_ISSUES = 5
MAX_TOOL_NAME_CHARS = 96
MAX_CORRECTION_HINT_CHARS = 512


class GMRuntimeFeedbackScope(str, Enum):
    """The feedback envelope can describe only the model's current turn."""

    CURRENT_TRANSACTION = "current_transaction"


class GMRuntimeFeedbackPhase(str, Enum):
    CREATED = "created"
    OBSERVING_STATE = "observing_state"
    BUILDING_CONTEXT = "building_context"
    REQUESTING_MODEL = "requesting_model"
    PROVIDER_RECOVERY = "provider_recovery"
    DISPATCHING_DECISION = "dispatching_decision"
    EXECUTING_TOOL = "executing_tool"
    VALIDATING_RECEIPT = "validating_receipt"
    REFRESHING_STATE = "refreshing_state"
    FINALIZING_TRANSACTION = "finalizing_transaction"
    ROLLING_BACK = "rolling_back"
    FINISHED = "finished"


class GMRuntimeFeedbackSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class GMRuntimeFeedbackIssueCode(str, Enum):
    PROVIDER_RECOVERED = "PROVIDER_RECOVERED"
    EMPTY_RESPONSE_RECOVERED = "EMPTY_RESPONSE_RECOVERED"
    RESPONSE_FORMAT_DOWNGRADED = "RESPONSE_FORMAT_DOWNGRADED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    STATE_REFRESH_FAILED = "STATE_REFRESH_FAILED"
    STATE_STALE = "STATE_STALE"
    DEADLINE_NEAR = "DEADLINE_NEAR"
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    ITERATION_LIMIT_NEAR = "ITERATION_LIMIT_NEAR"
    ITERATION_EXHAUSTED = "ITERATION_EXHAUSTED"
    TOOL_RETRY_REQUIRED = "TOOL_RETRY_REQUIRED"
    TOOL_REJECTED = "TOOL_REJECTED"
    TOOL_FAILED = "TOOL_FAILED"
    RECEIPT_VALIDATION_FAILED = "RECEIPT_VALIDATION_FAILED"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    TRANSACTION_COMMIT_FAILED = "TRANSACTION_COMMIT_FAILED"
    TRANSACTION_ROLLED_BACK = "TRANSACTION_ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class GMRuntimeRecoveryAction(str, Enum):
    NONE = "none"
    RETRY_CURRENT_STEP = "retry_current_step"
    RETRY_TOOL_WITH_CORRECTION = "retry_tool_with_correction"
    REFRESH_AUTHORITATIVE_STATE = "refresh_authoritative_state"
    FINISH_CURRENT_TASK_CONCISELY = "finish_current_task_concisely"
    COMPLETE_CURRENT_TASK_CONCISELY = "complete_current_task_concisely"
    USE_AUTHORITATIVE_RECEIPT = "use_authoritative_receipt"
    USE_RETAINED_AUTHORITATIVE_CONTEXT = (
        "use_retained_authoritative_context"
    )
    RETURN_VALID_PROTOCOL_JSON = "return_valid_protocol_json"
    ASK_USER = "ask_user"
    STOP_AND_ROLLBACK = "stop_and_rollback"


class GMRuntimeTransactionStatus(str, Enum):
    OPEN = "open"
    UNCOMMITTED = "uncommitted"
    PENDING_COMMIT = "pending_commit"
    COMMITTED = "committed"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    READ_ONLY = "read_only"


class GMRuntimeBudgetStatus(str, Enum):
    NORMAL = "normal"
    NEAR_LIMIT = "near_limit"
    EXHAUSTED = "exhausted"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _require_enum(value: object, enum_type: Type[_EnumT], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")


def _clean_text(value: object, *, limit: int) -> str:
    """Flatten untrusted diagnostics into one bounded prompt-safe line."""

    raw = str(value or "")
    visible = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in raw
    )
    clean = " ".join(visible.split())
    if len(clean) <= limit:
        return clean
    if limit <= 1:
        return clean[:limit]
    return clean[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class GMRuntimeBudget:
    """A sanitized budget snapshot supplied by the current agent loop."""

    iteration: int = 0
    max_iterations: int = 0
    elapsed_ms: int = 0
    timeout_ms: int = 0
    status: GMRuntimeBudgetStatus = GMRuntimeBudgetStatus.NORMAL

    def __post_init__(self) -> None:
        _require_enum(self.status, GMRuntimeBudgetStatus, "status")
        object.__setattr__(self, "iteration", max(0, int(self.iteration or 0)))
        object.__setattr__(
            self,
            "max_iterations",
            max(0, int(self.max_iterations or 0)),
        )
        object.__setattr__(
            self,
            "elapsed_ms",
            max(0, int(self.elapsed_ms or 0)),
        )
        object.__setattr__(
            self,
            "timeout_ms",
            max(0, int(self.timeout_ms or 0)),
        )

    @classmethod
    def from_limits(
        cls,
        *,
        iteration: int,
        max_iterations: int,
        elapsed_ms: int,
        timeout_ms: int,
        near_iteration_threshold: int = 1,
        near_time_threshold_ms: int = 10_000,
    ) -> "GMRuntimeBudget":
        clean_iteration = max(0, int(iteration or 0))
        clean_max_iterations = max(0, int(max_iterations or 0))
        clean_elapsed_ms = max(0, int(elapsed_ms or 0))
        clean_timeout_ms = max(0, int(timeout_ms or 0))
        remaining_iterations = (
            max(0, clean_max_iterations - clean_iteration + 1)
            if clean_max_iterations > 0
            else 0
        )
        remaining_ms = max(0, clean_timeout_ms - clean_elapsed_ms)
        exhausted = (
            clean_max_iterations > 0
            and clean_iteration > clean_max_iterations
        ) or (clean_timeout_ms > 0 and remaining_ms == 0)
        time_threshold = min(
            max(0, int(near_time_threshold_ms)),
            max(1, clean_timeout_ms // 4),
        )
        near_limit = (
            clean_max_iterations > 0
            and remaining_iterations <= max(0, int(near_iteration_threshold))
        ) or (
            clean_timeout_ms > 0
            and remaining_ms <= time_threshold
        )
        status = GMRuntimeBudgetStatus.NORMAL
        if exhausted:
            status = GMRuntimeBudgetStatus.EXHAUSTED
        elif near_limit:
            status = GMRuntimeBudgetStatus.NEAR_LIMIT
        return cls(
            iteration=clean_iteration,
            max_iterations=clean_max_iterations,
            elapsed_ms=clean_elapsed_ms,
            timeout_ms=clean_timeout_ms,
            status=status,
        )

    @property
    def remaining_iterations(self) -> int:
        if self.max_iterations <= 0:
            return 0
        return max(0, self.max_iterations - self.iteration + 1)

    @property
    def remaining_ms(self) -> int:
        return max(0, self.timeout_ms - self.elapsed_ms)

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "remaining_iterations": self.remaining_iterations,
            "elapsed_ms": self.elapsed_ms,
            "timeout_ms": self.timeout_ms,
            "remaining_ms": self.remaining_ms,
        }


@dataclass(frozen=True)
class GMRuntimeFeedbackIssue:
    """One actionable, unresolved issue visible to the current model call."""

    code: GMRuntimeFeedbackIssueCode
    phase: GMRuntimeFeedbackPhase
    severity: GMRuntimeFeedbackSeverity
    retryable: bool
    tool_name: str = ""
    correction_hint: str = ""
    recovery_action: GMRuntimeRecoveryAction = GMRuntimeRecoveryAction.NONE

    def __post_init__(self) -> None:
        _require_enum(self.code, GMRuntimeFeedbackIssueCode, "code")
        _require_enum(self.phase, GMRuntimeFeedbackPhase, "phase")
        _require_enum(self.severity, GMRuntimeFeedbackSeverity, "severity")
        _require_enum(
            self.recovery_action,
            GMRuntimeRecoveryAction,
            "recovery_action",
        )
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a bool")
        object.__setattr__(
            self,
            "tool_name",
            _clean_text(self.tool_name, limit=MAX_TOOL_NAME_CHARS),
        )
        object.__setattr__(
            self,
            "correction_hint",
            _clean_text(
                self.correction_hint,
                limit=MAX_CORRECTION_HINT_CHARS,
            ),
        )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.code.value, self.phase.value, self.tool_name)

    def to_payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "phase": self.phase.value,
            "severity": self.severity.value,
            "retryable": self.retryable,
            "tool_name": self.tool_name,
            "correction_hint": self.correction_hint,
            "recovery_action": self.recovery_action.value,
        }


_SEVERITY_RANK = {
    GMRuntimeFeedbackSeverity.INFO: 0,
    GMRuntimeFeedbackSeverity.WARNING: 1,
    GMRuntimeFeedbackSeverity.ERROR: 2,
    GMRuntimeFeedbackSeverity.CRITICAL: 3,
}

_TERMINAL_TRANSACTION_STATUSES = frozenset(
    {
        GMRuntimeTransactionStatus.COMMITTED,
        GMRuntimeTransactionStatus.ROLLBACK_PENDING,
        GMRuntimeTransactionStatus.ROLLED_BACK,
        GMRuntimeTransactionStatus.SUPERSEDED,
        GMRuntimeTransactionStatus.FAILED,
    }
)

_TERMINAL_ISSUE_CODES = frozenset(
    {
        GMRuntimeFeedbackIssueCode.PROVIDER_FAILURE,
        GMRuntimeFeedbackIssueCode.DEADLINE_EXHAUSTED,
        GMRuntimeFeedbackIssueCode.ITERATION_EXHAUSTED,
        GMRuntimeFeedbackIssueCode.TRANSACTION_COMMIT_FAILED,
        GMRuntimeFeedbackIssueCode.TRANSACTION_ROLLED_BACK,
        GMRuntimeFeedbackIssueCode.SUPERSEDED,
    }
)


@dataclass
class GMRuntimeFeedback:
    """Per-transaction feedback for the next model iteration.

    This object receives explicit loop/tool facts only.  It has no runtime,
    Dashboard, live-monitor, persistence, or lock dependency and is intended
    to be created anew for each message transaction.
    """

    phase: GMRuntimeFeedbackPhase = GMRuntimeFeedbackPhase.CREATED
    budget: GMRuntimeBudget = field(default_factory=GMRuntimeBudget)
    transaction_status: GMRuntimeTransactionStatus = (
        GMRuntimeTransactionStatus.UNCOMMITTED
    )
    _issues: list[GMRuntimeFeedbackIssue] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_enum(self.phase, GMRuntimeFeedbackPhase, "phase")
        if not isinstance(self.budget, GMRuntimeBudget):
            raise TypeError("budget must be a GMRuntimeBudget")
        _require_enum(
            self.transaction_status,
            GMRuntimeTransactionStatus,
            "transaction_status",
        )

    @property
    def scope(self) -> GMRuntimeFeedbackScope:
        return GMRuntimeFeedbackScope.CURRENT_TRANSACTION

    @property
    def issues(self) -> tuple[GMRuntimeFeedbackIssue, ...]:
        return tuple(self._issues)

    def set_phase(self, phase: GMRuntimeFeedbackPhase) -> None:
        _require_enum(phase, GMRuntimeFeedbackPhase, "phase")
        self.phase = phase

    def set_budget(self, budget: GMRuntimeBudget) -> None:
        if not isinstance(budget, GMRuntimeBudget):
            raise TypeError("budget must be a GMRuntimeBudget")
        self.budget = budget

    def set_transaction_status(
        self,
        status: GMRuntimeTransactionStatus,
    ) -> None:
        _require_enum(status, GMRuntimeTransactionStatus, "status")
        self.transaction_status = status

    def add_issue(self, issue: GMRuntimeFeedbackIssue) -> None:
        if not isinstance(issue, GMRuntimeFeedbackIssue):
            raise TypeError("issue must be a GMRuntimeFeedbackIssue")
        if issue.code in _TERMINAL_ISSUE_CODES:
            return
        self._issues = [
            current
            for current in self._issues
            if current.identity != issue.identity
        ]
        self._issues.append(issue)
        self._retain_most_important_issues()

    def report_issue(
        self,
        *,
        code: GMRuntimeFeedbackIssueCode,
        severity: GMRuntimeFeedbackSeverity,
        retryable: bool,
        recovery_action: GMRuntimeRecoveryAction,
        phase: GMRuntimeFeedbackPhase | None = None,
        tool_name: object = "",
        correction_hint: object = "",
    ) -> None:
        self.add_issue(
            GMRuntimeFeedbackIssue(
                code=code,
                phase=phase or self.phase,
                severity=severity,
                retryable=retryable,
                tool_name=str(tool_name or ""),
                correction_hint=str(correction_hint or ""),
                recovery_action=recovery_action,
            )
        )

    def resolve_issue(
        self,
        *,
        code: GMRuntimeFeedbackIssueCode,
        tool_name: object | None = None,
    ) -> int:
        _require_enum(code, GMRuntimeFeedbackIssueCode, "code")
        clean_tool_name = (
            None
            if tool_name is None
            else _clean_text(tool_name, limit=MAX_TOOL_NAME_CHARS)
        )
        original_count = len(self._issues)
        self._issues = [
            issue
            for issue in self._issues
            if not (
                issue.code is code
                and (
                    clean_tool_name is None
                    or issue.tool_name == clean_tool_name
                )
            )
        ]
        return original_count - len(self._issues)

    def should_emit(self) -> bool:
        if (
            self.transaction_status in _TERMINAL_TRANSACTION_STATUSES
            or self.budget.status is GMRuntimeBudgetStatus.EXHAUSTED
        ):
            return False
        return bool(self._model_visible_issues()) or (
            self.budget.status is GMRuntimeBudgetStatus.NEAR_LIMIT
        )

    def to_payload(self) -> dict[str, object] | None:
        if not self.should_emit():
            return None
        payload: dict[str, object] = {
            "runtime_feedback": {
                "scope": self.scope.value,
                "phase": self.phase.value,
                "budget": self.budget.to_payload(),
                "transaction": {"status": self.transaction_status.value},
                "issues": [
                    issue.to_payload()
                    for issue in self._model_visible_issues()
                ],
            }
        }
        return self._fit_serialized_limit(payload)

    def to_json(self) -> str | None:
        payload = self.to_payload()
        if payload is None:
            return None
        serialized = _serialize(payload)
        if len(serialized) > MAX_RUNTIME_FEEDBACK_CHARS:
            raise AssertionError("runtime feedback exceeded its hard limit")
        return serialized

    def _retain_most_important_issues(self) -> None:
        while len(self._issues) > MAX_RUNTIME_FEEDBACK_ISSUES:
            lowest_index = min(
                range(len(self._issues)),
                key=lambda index: (
                    _SEVERITY_RANK[self._issues[index].severity],
                    index,
                ),
            )
            self._issues.pop(lowest_index)

    def _model_visible_issues(self) -> tuple[GMRuntimeFeedbackIssue, ...]:
        return tuple(
            issue
            for issue in self._issues
            if issue.code not in _TERMINAL_ISSUE_CODES
        )

    @staticmethod
    def _fit_serialized_limit(
        payload: dict[str, object],
    ) -> dict[str, object]:
        feedback = payload.get("runtime_feedback")
        if not isinstance(feedback, dict):
            return payload
        issues = feedback.get("issues")
        if not isinstance(issues, list):
            return payload
        for field_name in ("correction_hint", "tool_name"):
            while len(_serialize(payload)) > MAX_RUNTIME_FEEDBACK_CHARS:
                candidates = [
                    issue
                    for issue in issues
                    if isinstance(issue, dict)
                    and str(issue.get(field_name, ""))
                ]
                if not candidates:
                    break
                longest = max(
                    candidates,
                    key=lambda issue: len(str(issue.get(field_name, ""))),
                )
                current = str(longest.get(field_name, ""))
                overage = len(_serialize(payload)) - MAX_RUNTIME_FEEDBACK_CHARS
                target = max(0, len(current) - max(1, overage))
                longest[field_name] = _clean_text(current, limit=target)
        while len(_serialize(payload)) > MAX_RUNTIME_FEEDBACK_CHARS and issues:
            issues.pop(0)
        return payload


def _serialize(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
