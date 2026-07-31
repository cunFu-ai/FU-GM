from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fu_gm.components.gm_message_tool_transaction import (
    GMMessageToolTransaction,
)
from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolFreshnessGuard,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_protocol import GMToolProtocol
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


@dataclass
class GMToolCallEvent:
    tool_name: str
    receipt: GMToolReceipt | None = None
    protocol_error_code: str = ""
    abort_repeated_call_loop: bool = False


class GMToolCallLedger:
    """One-message execution journal shared by single and batch tool calls."""

    _SAME_TOOL_SCHEMA_RETRY_CODES = frozenset(
        {
            "UNKNOWN_ARGUMENT",
            "MISSING_ARGUMENT",
            "SYSTEM_ARGUMENT_NOT_ALLOWED",
            "ARGUMENT_TYPE_MISMATCH",
            "ARGUMENT_ENUM_MISMATCH",
            "ARGUMENT_SCHEMA_MISMATCH",
            "INVALID_ARGUMENTS",
        }
    )
    _SAME_TOOL_AGENT_OUTPUT_RETRY_CODES = frozenset(
        {
            "NPC_RESPONSE_TRANSACTION_INVALID",
        }
    )
    _RETRY_PREPARATION_TOOLS = frozenset({"focus_scene_branch"})
    _MAX_SAME_TOOL_AGENT_OUTPUT_FAILURES = 3

    def __init__(
        self,
        *,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        freshness_guard: GMToolFreshnessGuard | None = None,
        side_effect_lock: Any | None = None,
        tool_permission_guard: Callable[[str], bool] | None = None,
        message_transaction: GMMessageToolTransaction | None = None,
    ) -> None:
        self.registry = registry
        self.context = context
        self.state_summary = state_summary
        self.freshness_guard = freshness_guard
        self.side_effect_lock = side_effect_lock
        self.tool_permission_guard = tool_permission_guard
        self.message_transaction = message_transaction
        self.history: list[dict[str, object]] = []
        self.receipts: list[GMToolReceipt] = []
        self.successful_write_calls: set[str] = set()
        self.successful_tool_calls: dict[str, int] = {}
        self.duplicate_write_attempts = 0
        self.pending_required_retry: dict[str, object] | None = None

    @property
    def required_retry_pending(self) -> bool:
        return self.pending_required_retry is not None

    @property
    def required_retry_tool(self) -> str:
        return str((self.pending_required_retry or {}).get("tool_name") or "").strip()

    def retry_protocol_error(
        self,
        decision: dict[str, object],
    ) -> dict[str, object] | None:
        """Require a model to repair the schema call it just submitted.

        A retryable argument error is not resolved by calling some unrelated
        write tool.  Read-only reference tools remain available because a
        receipt may explicitly require the GM to look up a canonical rule name
        before it can repair the original write.  Keeping this boundary in the
        execution journal makes the policy apply uniformly to Session 0, NPC,
        scene and management tools.
        """

        pending = self.pending_required_retry
        if pending is None:
            return None
        action = str(decision.get("decision") or "").strip().lower()
        expected = str(pending.get("tool_name") or "").strip()
        requested: list[str] = []
        if action == "call_tool":
            requested = [str(decision.get("tool_name") or "").strip()]
        elif action == "call_tools":
            calls = decision.get("calls")
            if isinstance(calls, list):
                requested = [
                    str(call.get("tool_name") or "").strip()
                    for call in calls
                    if isinstance(call, dict)
                ]
        # The GM may inspect any number of read-only references before the
        # required retry.  If a batch also contains a write, its first
        # non-read capability must be the failed tool itself.
        first_write = next(
            (
                name
                for name in requested
                if name
                and not self.registry.is_read_only(name)
                and name not in self._RETRY_PREPARATION_TOOLS
            ),
            "",
        )
        if requested and not first_write:
            return None
        if first_write == expected:
            return None
        retry_kind = str(pending.get("retry_kind") or "schema")
        if retry_kind == "redirect":
            issue_label = "语义重定向"
            repair_instruction = (
                "按上一回执的suggested_arguments与该工具schema重新提交；"
                "不得返回原工具，也不得把建议参数清空。"
            )
        elif retry_kind == "agent_output":
            issue_label = "智能体输出"
            repair_instruction = (
                "保留同一工具与对象，按上一回执的correction_hint修正输出；"
            )
        else:
            issue_label = "参数校验"
            repair_instruction = (
                "只修正上一回执指出的格式问题，保留previous_arguments中的其余合法字段和值；"
            )
        return {
            "protocol_error": {
                "error_code": (
                    "REDIRECT_TOOL_OMITTED"
                    if retry_kind == "redirect"
                    else "AGENT_OUTPUT_RETRY_TOOL_OMITTED"
                    if retry_kind == "agent_output"
                    else "SCHEMA_RETRY_TOOL_OMITTED"
                ),
                "message": (
                    f"工具 {expected} 的{issue_label}"
                    "仍未修复；不能用其他调用或文字跳过它。"
                ),
                "correction_hint": (
                    f"下一次写操作必须重新调用 {expected}。"
                    + repair_instruction
                    + "如需确认规则名，可先调用只读参考工具；若同批还有其他写工具，把它们排在修正后的调用之后。"
                ),
                "retryable": True,
                "required_retry": dict(pending),
            }
        }

    def execute(
        self,
        tool_name: str,
        arguments: object,
        *,
        batch_index: int | None = None,
    ) -> GMToolCallEvent:
        clean_name = str(tool_name or "").strip()
        if self.tool_permission_guard is not None and not self.tool_permission_guard(
            clean_name
        ):
            self.history.append(
                {
                    "protocol_error": {
                        "error_code": "TOOL_NOT_AVAILABLE_IN_CONTEXT",
                        "message": (
                            f"工具 {clean_name or '（空）'} 不属于当前阶段或系统节拍的"
                            "受信能力范围，未执行。"
                        ),
                        "correction_hint": (
                            "只能从本轮 available_tools 中重新选择；"
                            "不要凭记忆调用其他阶段或系统专用工具。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return GMToolCallEvent(
                tool_name=clean_name,
                protocol_error_code="TOOL_NOT_AVAILABLE_IN_CONTEXT",
            )
        call_limit = self.registry.successful_call_limit(clean_name)
        if call_limit and self.successful_tool_calls.get(clean_name, 0) >= call_limit:
            self.history.append(GMToolProtocol.tool_call_limit_error(clean_name, call_limit))
            return GMToolCallEvent(
                tool_name=clean_name,
                protocol_error_code="TOOL_CALL_LIMIT_REACHED",
                # The limit can only be reached after an authoritative success.
                # End the loop immediately instead of spending more model turns
                # asking it to restate a conclusion that is already committed.
                abort_repeated_call_loop=True,
            )

        fingerprint = GMToolProtocol.call_fingerprint(clean_name, arguments)
        if fingerprint in self.successful_write_calls:
            self.duplicate_write_attempts += 1
            qualifier = "批次中的" if batch_index is not None else ""
            self.history.append(
                {
                    "protocol_error": {
                        "error_code": "DUPLICATE_SUCCESSFUL_TOOL_CALL",
                        "message": f"{qualifier}相同工具和参数已经成功改变状态，禁止重复执行。",
                        "correction_hint": "停止重复调用；立即final并根据成功回执自然回应。",
                        "retryable": True,
                    }
                }
            )
            return GMToolCallEvent(
                tool_name=clean_name,
                protocol_error_code="DUPLICATE_SUCCESSFUL_TOOL_CALL",
                abort_repeated_call_loop=self.duplicate_write_attempts >= 2,
            )

        if self.message_transaction is not None:
            transaction_error = self.message_transaction.prepare(
                clean_name,
                arguments,
            )
            if transaction_error:
                self.history.append(
                    {
                        "protocol_error": {
                            "error_code": "MESSAGE_TRANSACTION_START_FAILED",
                            "message": (
                                "无法建立整条消息的回滚事务，工具未执行："
                                + transaction_error
                            ),
                            "correction_hint": (
                                "停止本轮写入并检查事务服务；"
                                "在事务恢复前不要声称状态已经改变。"
                            ),
                            "retryable": False,
                        }
                    }
                )
                return GMToolCallEvent(
                    tool_name=clean_name,
                    protocol_error_code="MESSAGE_TRANSACTION_START_FAILED",
                    abort_repeated_call_loop=True,
                )

        receipt = self.registry.execute(
            clean_name,
            arguments,
            self.context,
            freshness_guard=self.freshness_guard,
            side_effect_lock=self.side_effect_lock,
        )
        self.receipts.append(receipt)
        GMToolReceiptPolicy.apply_context(
            self.context,
            self.state_summary,
            receipt,
            tool_arguments=arguments if isinstance(arguments, dict) else {},
        )
        model_decision: dict[str, object] = {
            "decision": "call_tool",
            "tool_name": clean_name,
            "arguments": arguments if isinstance(arguments, dict) else arguments,
        }
        if batch_index is not None:
            model_decision["batch_index"] = batch_index
        self.history.append(
            {
                "model_decision": model_decision,
                "tool_receipt": receipt.to_dict(),
            }
        )
        retried_required_tool = bool(
            self.pending_required_retry is not None
            and str(self.pending_required_retry.get("tool_name") or "") == clean_name
        )
        abort_agent_output_retry_loop = False
        required_next_tool = str(
            receipt.result.get("required_next_tool") or ""
        ).strip()
        if required_next_tool and receipt.retryable:
            suggested_arguments = receipt.result.get("suggested_arguments")
            self.pending_required_retry = {
                "tool_name": required_next_tool,
                "retry_kind": "redirect",
                "error_code": receipt.error_code,
                "message": receipt.message,
                "correction_hint": receipt.correction_hint,
                "previous_arguments": (
                    dict(suggested_arguments)
                    if isinstance(suggested_arguments, dict)
                    else {}
                ),
            }
        elif receipt.error_code in self._SAME_TOOL_SCHEMA_RETRY_CODES and receipt.retryable:
            self.pending_required_retry = {
                "tool_name": clean_name,
                "retry_kind": "schema",
                "error_code": receipt.error_code,
                "message": receipt.message,
                "correction_hint": receipt.correction_hint,
                "previous_arguments": (
                    arguments if isinstance(arguments, dict) else arguments
                ),
            }
        elif (
            receipt.error_code in self._SAME_TOOL_AGENT_OUTPUT_RETRY_CODES
            and receipt.retryable
        ):
            previous_attempts = 0
            if retried_required_tool and str(
                self.pending_required_retry.get("retry_kind") or ""
            ) == "agent_output":
                previous_attempts = int(
                    self.pending_required_retry.get("attempt_count") or 0
                )
            attempt_count = previous_attempts + 1
            self.pending_required_retry = {
                "tool_name": clean_name,
                "retry_kind": "agent_output",
                "attempt_count": attempt_count,
                "max_attempts": self._MAX_SAME_TOOL_AGENT_OUTPUT_FAILURES,
                "error_code": receipt.error_code,
                "message": receipt.message,
                "correction_hint": receipt.correction_hint,
                "previous_arguments": (
                    arguments if isinstance(arguments, dict) else arguments
                ),
            }
            abort_agent_output_retry_loop = (
                attempt_count >= self._MAX_SAME_TOOL_AGENT_OUTPUT_FAILURES
            )
        elif retried_required_tool:
            # The required retry happened.  A different domain error now owns
            # recovery through its own receipt; do not keep enforcing the
            # stale schema or agent-output failure that preceded it.
            self.pending_required_retry = None
        if receipt.ok:
            self.successful_tool_calls[clean_name] = (
                self.successful_tool_calls.get(clean_name, 0) + 1
            )
        if receipt.ok and receipt.state_changed:
            self.successful_write_calls.add(fingerprint)
        return GMToolCallEvent(
            tool_name=clean_name,
            receipt=receipt,
            abort_repeated_call_loop=abort_agent_output_retry_loop,
        )
