from __future__ import annotations

import inspect
import json

import pytest

import fu_gm.components.gm_runtime_feedback as runtime_feedback_module
from fu_gm.components.gm_runtime_feedback import (
    MAX_CORRECTION_HINT_CHARS,
    MAX_RUNTIME_FEEDBACK_CHARS,
    MAX_RUNTIME_FEEDBACK_ISSUES,
    MAX_TOOL_NAME_CHARS,
    GMRuntimeBudget,
    GMRuntimeBudgetStatus,
    GMRuntimeFeedback,
    GMRuntimeFeedbackIssue,
    GMRuntimeFeedbackIssueCode,
    GMRuntimeFeedbackPhase,
    GMRuntimeFeedbackSeverity,
    GMRuntimeRecoveryAction,
    GMRuntimeTransactionStatus,
)


def _issue(
    code: GMRuntimeFeedbackIssueCode,
    *,
    severity: GMRuntimeFeedbackSeverity = GMRuntimeFeedbackSeverity.WARNING,
    tool_name: str = "",
    correction_hint: str = "",
) -> GMRuntimeFeedbackIssue:
    return GMRuntimeFeedbackIssue(
        code=code,
        phase=GMRuntimeFeedbackPhase.EXECUTING_TOOL,
        severity=severity,
        retryable=True,
        tool_name=tool_name,
        correction_hint=correction_hint,
        recovery_action=GMRuntimeRecoveryAction.RETRY_TOOL_WITH_CORRECTION,
    )


def test_normal_empty_feedback_is_omitted() -> None:
    feedback = GMRuntimeFeedback()

    assert feedback.should_emit() is False
    assert feedback.to_payload() is None
    assert feedback.to_json() is None


def test_payload_has_only_current_transaction_and_whitelisted_issue_fields() -> None:
    feedback = GMRuntimeFeedback(
        phase=GMRuntimeFeedbackPhase.EXECUTING_TOOL,
        budget=GMRuntimeBudget(
            iteration=3,
            max_iterations=5,
            elapsed_ms=12_000,
            timeout_ms=40_000,
            status=GMRuntimeBudgetStatus.NEAR_LIMIT,
        ),
    )
    feedback.add_issue(
        _issue(
            GMRuntimeFeedbackIssueCode.TOOL_RETRY_REQUIRED,
            tool_name="  cast_spell\n\x00 now  ",
            correction_hint="  补齐目标 ID。\n然后重试。  ",
        )
    )

    payload = feedback.to_payload()
    assert payload is not None
    assert set(payload) == {"runtime_feedback"}
    body = payload["runtime_feedback"]
    assert isinstance(body, dict)
    assert body["scope"] == "current_transaction"
    assert body["phase"] == "executing_tool"
    assert body["transaction"] == {"status": "uncommitted"}
    assert body["budget"] == {
        "status": "near_limit",
        "iteration": 3,
        "max_iterations": 5,
        "remaining_iterations": 3,
        "elapsed_ms": 12_000,
        "timeout_ms": 40_000,
        "remaining_ms": 28_000,
    }
    issue = body["issues"][0]
    assert set(issue) == {
        "code",
        "phase",
        "severity",
        "retryable",
        "tool_name",
        "correction_hint",
        "recovery_action",
    }
    assert issue == {
        "code": "TOOL_RETRY_REQUIRED",
        "phase": "executing_tool",
        "severity": "warning",
        "retryable": True,
        "tool_name": "cast_spell now",
        "correction_hint": "补齐目标 ID。 然后重试。",
        "recovery_action": "retry_tool_with_correction",
    }


def test_enum_fields_reject_arbitrary_strings_and_retryable_is_strict_bool() -> None:
    with pytest.raises(TypeError, match="GMRuntimeFeedbackPhase"):
        GMRuntimeFeedback(phase="executing_tool")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="GMRuntimeFeedbackIssueCode"):
        GMRuntimeFeedbackIssue(
            code="CUSTOM_ERROR",  # type: ignore[arg-type]
            phase=GMRuntimeFeedbackPhase.EXECUTING_TOOL,
            severity=GMRuntimeFeedbackSeverity.ERROR,
            retryable=True,
        )
    with pytest.raises(TypeError, match="retryable must be a bool"):
        GMRuntimeFeedbackIssue(
            code=GMRuntimeFeedbackIssueCode.TOOL_FAILED,
            phase=GMRuntimeFeedbackPhase.EXECUTING_TOOL,
            severity=GMRuntimeFeedbackSeverity.ERROR,
            retryable="false",  # type: ignore[arg-type]
        )


def test_only_five_most_important_unresolved_issues_are_retained() -> None:
    codes = [
        GMRuntimeFeedbackIssueCode.PROVIDER_RECOVERED,
        GMRuntimeFeedbackIssueCode.EMPTY_RESPONSE_RECOVERED,
        GMRuntimeFeedbackIssueCode.RESPONSE_FORMAT_DOWNGRADED,
        GMRuntimeFeedbackIssueCode.STATE_REFRESH_FAILED,
        GMRuntimeFeedbackIssueCode.STATE_STALE,
        GMRuntimeFeedbackIssueCode.DEADLINE_NEAR,
    ]
    feedback = GMRuntimeFeedback()
    for code in codes[:MAX_RUNTIME_FEEDBACK_ISSUES]:
        feedback.add_issue(
            _issue(code, severity=GMRuntimeFeedbackSeverity.ERROR)
        )
    feedback.add_issue(
        _issue(codes[5], severity=GMRuntimeFeedbackSeverity.INFO)
    )

    assert len(feedback.issues) == MAX_RUNTIME_FEEDBACK_ISSUES
    assert codes[5] not in {issue.code for issue in feedback.issues}

    replacement = _issue(
        codes[1],
        severity=GMRuntimeFeedbackSeverity.CRITICAL,
        correction_hint="使用更精确的参数。",
    )
    feedback.add_issue(replacement)
    assert len(feedback.issues) == MAX_RUNTIME_FEEDBACK_ISSUES
    assert feedback.issues[-1] == replacement


def test_resolved_issues_are_removed_before_serialization() -> None:
    feedback = GMRuntimeFeedback()
    code = GMRuntimeFeedbackIssueCode.TOOL_RETRY_REQUIRED
    feedback.add_issue(_issue(code, tool_name="first"))
    feedback.add_issue(_issue(code, tool_name="second"))

    assert feedback.resolve_issue(code=code, tool_name="first") == 1
    payload = feedback.to_payload()
    assert payload is not None
    assert [
        issue["tool_name"]
        for issue in payload["runtime_feedback"]["issues"]
    ] == ["second"]

    assert feedback.resolve_issue(code=code) == 1
    assert feedback.to_payload() is None


def test_text_is_sanitized_and_whole_envelope_never_exceeds_four_kib_chars() -> None:
    feedback = GMRuntimeFeedback(
        phase=GMRuntimeFeedbackPhase.PROVIDER_RECOVERY,
    )
    for code in (
        GMRuntimeFeedbackIssueCode.PROVIDER_RECOVERED,
        GMRuntimeFeedbackIssueCode.EMPTY_RESPONSE_RECOVERED,
        GMRuntimeFeedbackIssueCode.RESPONSE_FORMAT_DOWNGRADED,
        GMRuntimeFeedbackIssueCode.STATE_REFRESH_FAILED,
        GMRuntimeFeedbackIssueCode.STATE_STALE,
    ):
        feedback.add_issue(
            _issue(
                code,
                severity=GMRuntimeFeedbackSeverity.CRITICAL,
                tool_name="tool\n" + "x" * 2_000,
                correction_hint="修正\x00\n" + "界" * 20_000,
            )
        )

    assert all(
        len(issue.tool_name) <= MAX_TOOL_NAME_CHARS
        and len(issue.correction_hint) <= MAX_CORRECTION_HINT_CHARS
        and "\n" not in issue.tool_name
        and "\n" not in issue.correction_hint
        and "\x00" not in issue.correction_hint
        for issue in feedback.issues
    )
    serialized = feedback.to_json()
    assert serialized is not None
    assert len(serialized) <= MAX_RUNTIME_FEEDBACK_CHARS
    decoded = json.loads(serialized)
    assert decoded == feedback.to_payload()
    assert len(decoded["runtime_feedback"]["issues"]) == 5


def test_budget_status_is_derived_and_can_emit_without_an_issue() -> None:
    budget = GMRuntimeBudget.from_limits(
        iteration=4,
        max_iterations=5,
        elapsed_ms=91_000,
        timeout_ms=100_000,
    )
    assert budget.status is GMRuntimeBudgetStatus.NEAR_LIMIT
    assert budget.remaining_iterations == 2
    assert budget.remaining_ms == 9_000

    feedback = GMRuntimeFeedback(budget=budget)
    payload = feedback.to_payload()
    assert payload is not None
    assert payload["runtime_feedback"]["issues"] == []
    assert payload["runtime_feedback"]["budget"]["status"] == "near_limit"

    last_allowed = GMRuntimeBudget.from_limits(
        iteration=5,
        max_iterations=5,
        elapsed_ms=50_000,
        timeout_ms=100_000,
    )
    assert last_allowed.status is GMRuntimeBudgetStatus.NEAR_LIMIT
    assert last_allowed.remaining_iterations == 1

    exhausted = GMRuntimeBudget.from_limits(
        iteration=6,
        max_iterations=5,
        elapsed_ms=50_000,
        timeout_ms=100_000,
    )
    assert exhausted.status is GMRuntimeBudgetStatus.EXHAUSTED

    short_first_call = GMRuntimeBudget.from_limits(
        iteration=1,
        max_iterations=5,
        elapsed_ms=0,
        timeout_ms=8_000,
    )
    assert short_first_call.status is GMRuntimeBudgetStatus.NORMAL


def test_provider_recovery_codes_and_actions_are_explicitly_whitelisted() -> None:
    assert GMRuntimeFeedbackIssueCode.EMPTY_RESPONSE_RECOVERED.value == (
        "EMPTY_RESPONSE_RECOVERED"
    )
    assert GMRuntimeFeedbackIssueCode.RESPONSE_FORMAT_DOWNGRADED.value == (
        "RESPONSE_FORMAT_DOWNGRADED"
    )
    assert (
        GMRuntimeRecoveryAction.USE_RETAINED_AUTHORITATIVE_CONTEXT.value
        == "use_retained_authoritative_context"
    )
    assert GMRuntimeRecoveryAction.RETURN_VALID_PROTOCOL_JSON.value == (
        "return_valid_protocol_json"
    )


def test_transaction_status_is_reported_only_while_model_can_continue() -> None:
    feedback = GMRuntimeFeedback(
        transaction_status=GMRuntimeTransactionStatus.PENDING_COMMIT,
    )
    feedback.add_issue(
        _issue(GMRuntimeFeedbackIssueCode.TOOL_RETRY_REQUIRED)
    )
    payload = feedback.to_payload()
    assert payload is not None
    assert payload["runtime_feedback"]["transaction"] == {
        "status": "pending_commit"
    }

    feedback.set_transaction_status(GMRuntimeTransactionStatus.ROLLED_BACK)
    assert feedback.to_payload() is None


def test_terminal_issues_and_exhausted_budget_cannot_trigger_an_llm_handoff() -> None:
    feedback = GMRuntimeFeedback()
    feedback.add_issue(
        _issue(GMRuntimeFeedbackIssueCode.TRANSACTION_COMMIT_FAILED)
    )
    assert feedback.issues == ()
    assert feedback.to_payload() is None

    feedback.add_issue(_issue(GMRuntimeFeedbackIssueCode.TOOL_RETRY_REQUIRED))
    feedback.set_budget(
        GMRuntimeBudget(
            iteration=6,
            max_iterations=5,
            status=GMRuntimeBudgetStatus.EXHAUSTED,
        )
    )
    assert feedback.to_payload() is None


def test_component_has_no_monitor_dashboard_or_lock_dependency() -> None:
    feedback = GMRuntimeFeedback()
    assert feedback.to_payload() is None

    source = inspect.getsource(runtime_feedback_module)
    assert "gm_live_run_monitor" not in source
    assert "transaction_lock" not in source
    assert "threading" not in source
