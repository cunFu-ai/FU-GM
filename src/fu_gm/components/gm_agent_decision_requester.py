from __future__ import annotations

import time
from typing import Any

from fu_gm.components.gm_live_run_monitor import emit_live_run_event
from fu_gm.gm_tool_protocol import (
    GMToolDecisionProtocolError,
    GMToolProtocol,
)
from fu_gm.llm_client import ChatMessage, LLMEmptyResponseError
from fu_gm.llm_utils import extract_json_object_sequence


class GMToolAgentDecisionRequester:
    """Own provider I/O and syntax-only recovery for one agent decision.

    This boundary deliberately knows nothing about campaign state or tool side
    effects. It may repair malformed JSON syntax, but it cannot reinterpret the
    player's message or alter a proposed tool call.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        repair_model: str = "",
        protocol: type[GMToolProtocol] = GMToolProtocol,
        parse_retries: int = 1,
        empty_response_retries: int = 0,
        max_output_tokens: int = 4096,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.repair_model = str(repair_model or model or "").strip()
        self.protocol = protocol
        self.parse_retries = max(0, int(parse_retries))
        self.empty_response_retries = max(0, int(empty_response_retries))
        self.max_output_tokens = max(512, int(max_output_tokens))
        # Private, process-local diagnostics for the latest malformed model
        # response.  Callers may persist this only in restricted audit logs;
        # it must never be copied into a player-facing reply.
        self.last_protocol_diagnostics: dict[str, object] = {}

    def request(
        self,
        messages: list[ChatMessage],
        *,
        iteration: int,
        deadline: float,
        trace: list[dict[str, object]],
        runtime_feedback_issues: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        self.last_protocol_diagnostics = {}
        active_messages = list(messages)
        malformed_protocol_draft = ""
        parse_error: Exception | None = None
        empty_attempts = 0
        for parse_attempt in range(self.parse_retries + 1):
            while True:
                operation = (
                    f"gm_tool_agent.iteration_{iteration}"
                    if parse_attempt == 0
                    else (
                        f"gm_tool_agent.iteration_{iteration}"
                        f".parse_retry_{parse_attempt}"
                    )
                )
                if empty_attempts:
                    operation = f"{operation}.empty_retry_{empty_attempts}"
                try:
                    response_format = (
                        {"type": "json_object"}
                        if bool(
                            getattr(
                                getattr(self.client, "config", None),
                                "response_format_enabled",
                                True,
                            )
                        )
                        else None
                    )
                    request_model = (
                        self.model
                        if parse_attempt == 0
                        else self.repair_model
                    )
                    emit_live_run_event(
                        "model_request_started",
                        phase="requesting_model",
                        iteration=iteration,
                        attempt=parse_attempt + empty_attempts + 1,
                        summary="已向模型提交决策请求，正在等待完整响应。",
                        public_details={
                            "model": request_model,
                            "operation": operation,
                            "parse_attempt": parse_attempt + 1,
                            "empty_retry": empty_attempts,
                        },
                    )
                    raw = self.client.create_chat_completion(
                        model=(
                            request_model
                        ),
                        messages=active_messages,
                        temperature=0.0,
                        response_format=response_format,
                        max_tokens=self.max_output_tokens,
                        deadline=deadline,
                        operation=operation,
                        thinking_enabled=False,
                        max_recovery_retries=1,
                        retry_without_response_format_on_empty=True,
                    )
                    self._collect_provider_recovery_issues(
                        runtime_feedback_issues,
                        trace=trace,
                        iteration=iteration,
                    )
                    emit_live_run_event(
                        "model_response_raw",
                        phase="parsing_model_response",
                        iteration=iteration,
                        attempt=parse_attempt + empty_attempts + 1,
                        summary="模型已返回完整 assistant 正文，正在解析决策。",
                        public_details={
                            "model": request_model,
                            "operation": operation,
                            "response_chars": len(str(raw or "")),
                        },
                        private_details={"raw_output": str(raw or "")},
                    )
                    break
                except LLMEmptyResponseError as exc:
                    if (
                        empty_attempts >= self.empty_response_retries
                        or time.monotonic() >= deadline
                    ):
                        emit_live_run_event(
                            "model_request_failed",
                            phase="provider_recovery",
                            iteration=iteration,
                            attempt=parse_attempt + empty_attempts + 1,
                            summary="模型请求以空响应结束，已停止重试。",
                            public_details={
                                "model": request_model,
                                "operation": operation,
                                "error_type": type(exc).__name__,
                            },
                        )
                        raise
                    empty_attempts += 1
                    self._append_runtime_feedback_issue(
                        runtime_feedback_issues,
                        code="EMPTY_RESPONSE_RECOVERED",
                    )
                    emit_live_run_event(
                        "model_empty_response_retry",
                        phase="requesting_model",
                        iteration=iteration,
                        attempt=empty_attempts,
                        summary="模型返回空正文，正在进行有界重试。",
                        public_details={
                            "error_type": type(exc).__name__,
                            "empty_retry": empty_attempts,
                        },
                    )
                    trace.append(
                        {
                            "iteration": iteration,
                            "phase": "provider_empty_recovery",
                            "attempt": empty_attempts,
                            "error": str(exc)[:300],
                        }
                    )
                except Exception as exc:
                    emit_live_run_event(
                        "model_request_failed",
                        phase="provider_recovery",
                        iteration=iteration,
                        attempt=parse_attempt + empty_attempts + 1,
                        summary="模型请求失败，正在按事务失败策略收束。",
                        public_details={
                            "model": request_model,
                            "operation": operation,
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
            try:
                decisions = extract_json_object_sequence(raw)
            except (TypeError, ValueError) as exc:
                parse_error = exc
                finish_reason = self._latest_finish_reason(operation)
                # Continue from the latest syntax-only repair instead of
                # asking every retry to reproduce the same broken draft.
                # No repaired value can execute before protocol, schema and
                # semantic validation all succeed.
                malformed_protocol_draft = str(raw)
                self.last_protocol_diagnostics = {
                    "iteration": iteration,
                    "operation": operation,
                    "parse_attempt": parse_attempt + 1,
                    "finish_reason": finish_reason,
                    "response_chars": len(malformed_protocol_draft),
                    "parser_error": str(exc)[:300],
                    "raw_output": malformed_protocol_draft[:16000],
                }
                emit_live_run_event(
                    "model_response_parse_failed",
                    phase="repairing_model_response",
                    iteration=iteration,
                    attempt=parse_attempt + 1,
                    summary="模型正文不是完整合法的决策 JSON，正在进行语法修复。",
                    public_details={
                        "error_type": type(exc).__name__,
                        "parse_attempt": parse_attempt + 1,
                    },
                )
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "parse_recovery",
                        "attempt": parse_attempt + 1,
                        "error": str(exc)[:300],
                    }
                )
                # A length-limited draft is semantically incomplete, not just
                # syntactically damaged.  Asking a syntax-only repair model to
                # reproduce it cannot restore the missing tail and previously
                # multiplied one bad response into many 4096-token retries.
                # Return a concise correction to the full GM loop instead; it
                # still has the original message, state and tool schemas and
                # can choose a smaller atomic decision.
                if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
                    trace.append(
                        {
                            "iteration": iteration,
                            "phase": "length_limited_protocol_output",
                            "finish_reason": finish_reason,
                            "response_chars": len(malformed_protocol_draft),
                        }
                    )
                    raise GMToolDecisionProtocolError(
                        "上一次工具决策达到输出长度上限且内容不完整；不要复制旧草稿。"
                        "请只提交下一项必要的原子工具调用，并保持arguments简洁；"
                        "其余事项留到后续迭代处理。"
                    ) from exc
                if parse_attempt >= self.parse_retries:
                    raise GMToolDecisionProtocolError(
                        "工具智能体输出的JSON语法仍不完整；请根据原始消息、当前状态和工具回执重新生成完整决策。",
                        invalid_draft=malformed_protocol_draft,
                    ) from exc
                active_messages = self.protocol.syntax_repair_messages(
                    malformed_protocol_draft,
                    error=exc,
                )
                continue
            try:
                normalized = self.protocol.normalize_decision_sequence(decisions)
                decision_items = (
                    list(normalized.get("calls") or [])
                    if str(normalized.get("decision") or "") == "call_tools"
                    else [normalized]
                )
                emit_live_run_event(
                    "model_decision_parsed",
                    phase="dispatching_decision",
                    iteration=iteration,
                    attempt=parse_attempt + 1,
                    summary="模型决策 JSON 已通过协议解析。",
                    public_details={
                        "decision": str(normalized.get("decision") or ""),
                        "tool_name": str(normalized.get("tool_name") or ""),
                        "decision_count": len(decision_items),
                    },
                    private_details={"parsed_decision": normalized},
                )
                return normalized
            except GMToolDecisionProtocolError as exc:
                # The JSON is readable, but repairing an omitted tool name or
                # arguments object would require guessing intent.  Return the
                # exact contract error to the full GM loop instead, where the
                # original message, state, tools and receipts are available.
                rejected = GMToolDecisionProtocolError(
                    str(exc),
                    invalid_draft=str(raw),
                )
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "decision_protocol_rejection",
                        "error": str(exc)[:300],
                    }
                )
                emit_live_run_event(
                    "model_decision_rejected",
                    phase="repairing_model_response",
                    iteration=iteration,
                    attempt=parse_attempt + 1,
                    summary="模型 JSON 可读取，但未满足工具决策协议。",
                    public_details={"error_type": type(exc).__name__},
                )
                raise rejected from exc
        raise parse_error or ValueError("未找到合法 JSON 对象。")

    def _latest_finish_reason(self, operation: str) -> str:
        """Read the provider's finish reason without coupling to its client."""

        records = getattr(self.client, "recent_calls", None)
        if not isinstance(records, list) or not records:
            return ""
        latest = records[-1]
        if not isinstance(latest, dict):
            return ""
        if str(latest.get("operation") or "") != str(operation or ""):
            return ""
        return str(latest.get("finish_reason") or "").strip().lower()

    def _collect_provider_recovery_issues(
        self,
        issues: list[dict[str, object]] | None,
        *,
        trace: list[dict[str, object]],
        iteration: int,
    ) -> None:
        consume = getattr(self.client, "consume_call_diagnostics", None)
        if not callable(consume):
            return
        try:
            diagnostics = dict(consume() or {})
        except Exception:
            return
        codes = [
            str(item or "").strip().upper()
            for item in list(diagnostics.get("recovery_codes") or [])
            if str(item or "").strip()
        ]
        if not codes:
            return
        trace.append(
            {
                "iteration": iteration,
                "phase": "provider_recovered",
                "recovery_codes": list(dict.fromkeys(codes)),
                "attempt_count": max(
                    1,
                    int(diagnostics.get("attempt_count") or 1),
                ),
            }
        )
        for code in codes:
            self._append_runtime_feedback_issue(issues, code=code)

    @staticmethod
    def _append_runtime_feedback_issue(
        issues: list[dict[str, object]] | None,
        *,
        code: str,
    ) -> None:
        if issues is None:
            return
        specifications = {
            "PROVIDER_RECOVERED": (
                "warning",
                "供应商请求已恢复；当前步骤可继续使用已保留的权威上下文。",
                "none",
            ),
            "EMPTY_RESPONSE_RECOVERED": (
                "warning",
                "模型请求已从空响应恢复；当前步骤可继续处理。",
                "none",
            ),
            "CONTEXT_COMPACTED": (
                "warning",
                "本次请求已压缩上下文；优先依据保留的当前消息、权威状态和工具回执。",
                "use_retained_authoritative_context",
            ),
            "RESPONSE_FORMAT_DOWNGRADED": (
                "info",
                "供应商已切换为普通文本响应；继续严格返回决策JSON。",
                "return_valid_protocol_json",
            ),
        }
        normalized = str(code or "").strip().upper()
        specification = specifications.get(normalized)
        if specification is None:
            return
        severity, hint, action = specification
        issues.append(
            {
                "code": normalized or "PROVIDER_RECOVERED",
                "phase": "requesting_model",
                "severity": severity,
                # These signals describe a recovery that already succeeded.
                # They may inform the next ordinary decision, but they never
                # authorize another provider or agent retry by themselves.
                "retryable": False,
                "correction_hint": hint,
                "recovery_action": action,
            }
        )
