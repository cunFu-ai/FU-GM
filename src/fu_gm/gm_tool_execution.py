from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fu_gm.components.gm_message_tool_transaction import (
    GMMessageToolTransaction,
)
from fu_gm.components.gm_live_run_monitor import emit_live_run_event
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
            "HERO_SKILL_OPTION_MAPPED_TO_BASE_ATTRIBUTES",
        }
    )
    _SAME_TOOL_AGENT_OUTPUT_RETRY_CODES = frozenset(
        {
            "NPC_RESPONSE_TRANSACTION_INVALID",
            # 规则动作已经进入硬规则层，但模型给出的领域参数仍不合法。
            # 允许它依据回执修正；连续三次仍失败就停止，避免一次玩家
            # 消息反复消耗模型调用直至整个请求超时。
            "RULE_ACTION_REJECTED",
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
        self.attempted_mutating_calls: set[str] = set()
        self.duplicate_write_attempts = 0
        self.pending_required_retry: dict[str, object] | None = None

    @property
    def required_retry_pending(self) -> bool:
        return self.pending_required_retry is not None

    @property
    def required_retry_tool(self) -> str:
        return str((self.pending_required_retry or {}).get("tool_name") or "").strip()

    @property
    def mutating_call_attempted(self) -> bool:
        return bool(self.attempted_mutating_calls)

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
        emit_live_run_event(
            "tool_call_started",
            phase="executing_tool",
            summary=f"正在校验并执行工具 {clean_name or '（空工具名）'}。",
            public_details={
                "tool_name": clean_name,
                "batch_index": batch_index,
                "side_effect": self.registry.side_effect(clean_name),
            },
            private_details={"arguments": arguments},
        )
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
            emit_live_run_event(
                "tool_call_rejected",
                phase="dispatching_decision",
                summary=f"工具 {clean_name or '（空工具名）'} 未进入执行器。",
                public_details={
                    "tool_name": clean_name,
                    "error_code": "TOOL_NOT_AVAILABLE_IN_CONTEXT",
                    "batch_index": batch_index,
                },
            )
            return GMToolCallEvent(
                tool_name=clean_name,
                protocol_error_code="TOOL_NOT_AVAILABLE_IN_CONTEXT",
            )
        call_limit = self.registry.successful_call_limit(clean_name)
        if call_limit and self.successful_tool_calls.get(clean_name, 0) >= call_limit:
            self.history.append(GMToolProtocol.tool_call_limit_error(clean_name, call_limit))
            emit_live_run_event(
                "tool_call_rejected",
                phase="dispatching_decision",
                summary=f"工具 {clean_name} 已达到本轮调用上限。",
                public_details={
                    "tool_name": clean_name,
                    "error_code": "TOOL_CALL_LIMIT_REACHED",
                    "call_limit": call_limit,
                    "batch_index": batch_index,
                },
            )
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
            emit_live_run_event(
                "tool_call_rejected",
                phase="dispatching_decision",
                summary=f"工具 {clean_name} 的相同写入已成功执行，已阻止重复调用。",
                public_details={
                    "tool_name": clean_name,
                    "error_code": "DUPLICATE_SUCCESSFUL_TOOL_CALL",
                    "batch_index": batch_index,
                },
            )
            return GMToolCallEvent(
                tool_name=clean_name,
                protocol_error_code="DUPLICATE_SUCCESSFUL_TOOL_CALL",
                abort_repeated_call_loop=self.duplicate_write_attempts >= 2,
            )

        if self.registry.side_effect(clean_name) not in {"", "read"}:
            self.attempted_mutating_calls.add(clean_name)

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
                emit_live_run_event(
                    "tool_call_rejected",
                    phase="dispatching_decision",
                    summary=f"工具 {clean_name} 的消息事务未能建立。",
                    public_details={
                        "tool_name": clean_name,
                        "error_code": "MESSAGE_TRANSACTION_START_FAILED",
                        "batch_index": batch_index,
                    },
                )
                return GMToolCallEvent(
                    tool_name=clean_name,
                    protocol_error_code="MESSAGE_TRANSACTION_START_FAILED",
                    abort_repeated_call_loop=True,
                )

        try:
            receipt = self.registry.execute(
                clean_name,
                arguments,
                self.context,
                freshness_guard=self.freshness_guard,
                side_effect_lock=self.side_effect_lock,
            )
        except Exception as exc:
            emit_live_run_event(
                "tool_call_exception",
                phase="dispatching_decision",
                summary=f"工具 {clean_name} 执行器抛出异常。",
                public_details={
                    "tool_name": clean_name,
                    "error_type": type(exc).__name__,
                    "batch_index": batch_index,
                },
            )
            raise
        self.receipts.append(receipt)
        emit_live_run_event(
            "tool_receipt",
            phase="processing_tool_receipt",
            summary=(
                f"工具 {clean_name} 执行成功，回执已返回。"
                if receipt.ok
                else f"工具 {clean_name} 返回拒绝或失败回执。"
            ),
            public_details={
                "tool_name": clean_name,
                "ok": receipt.ok,
                "state_changed": receipt.state_changed,
                "error_code": receipt.error_code,
                "retryable": receipt.retryable,
                "batch_index": batch_index,
            },
            private_details={"tool_receipt": receipt.to_dict()},
        )
        if (
            self.message_transaction is not None
            and receipt.ok
            and receipt.state_changed
        ):
            self.message_transaction.mark_state_changed()
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
                "tool_receipt": GMToolReceiptPolicy.model_view(
                    receipt,
                    max_result_chars=self.registry.model_result_char_budget(
                        clean_name
                    ),
                ),
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
            if self._action_type_requires_tool_reselection(receipt, arguments):
                # action_type不是普通格式字段，而是整项行动的语义判别符。
                # 当前工具不支持模型提交的动作类型时，强迫它从合法枚举
                # 里任选一个，会把“撤离”之类的原意篡改成Guard。
                self.pending_required_retry = None
                self.history.append(
                    {
                        "protocol_error": {
                            "error_code": "ACTION_TYPE_TOOL_RESELECTION_REQUIRED",
                            "message": (
                                f"工具 {clean_name} 不能表达刚才提交的动作类型；"
                                "不得改成另一项合法行动来通过枚举校验。"
                            ),
                            "correction_hint": (
                                "保持玩家原始行动不变，重新选择能够表达该行动的工具；"
                                "若原意是移动或撤离，按是否存在阻碍选择移动检定或场景移动工具。"
                            ),
                            "retryable": True,
                            "previous_arguments": (
                                dict(arguments)
                                if isinstance(arguments, dict)
                                else arguments
                            ),
                        }
                    }
                )
            else:
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

    @staticmethod
    def _action_type_requires_tool_reselection(
        receipt: GMToolReceipt,
        arguments: object,
    ) -> bool:
        """判断枚举修正是否可能静默改变玩家选择的行动。"""

        if receipt.error_code != "ARGUMENT_ENUM_MISMATCH":
            return False
        if not isinstance(arguments, dict) or "action_type" not in arguments:
            return False
        schema = receipt.result.get("argument_schema")
        schema = schema if isinstance(schema, dict) else {}
        action_schema = schema.get("action_type")
        action_schema = action_schema if isinstance(action_schema, dict) else {}
        allowed = [
            str(item or "").strip()
            for item in list(action_schema.get("enum") or [])
            if str(item or "").strip()
        ]
        proposed = str(arguments.get("action_type") or "").strip()
        if not proposed or not allowed:
            return False
        # 只有大小写差异仍属于同一动作的格式修正。其他替换都可能改变
        # 玩家决定，必须回到工具选择层重新处理。
        return proposed.casefold() not in {
            item.casefold() for item in allowed
        }
