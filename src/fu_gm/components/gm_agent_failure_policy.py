from __future__ import annotations

from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


class GMToolAgentFailurePolicy:
    """Fail closed without confusing table ownership with reply obligation."""

    _PROTOCOL_ERROR_CODES = frozenset(
        {
            "UNKNOWN_ARGUMENT",
            "MISSING_ARGUMENT",
            "ARGUMENT_TYPE_MISMATCH",
            "ARGUMENT_ENUM_MISMATCH",
            "ARGUMENT_SCHEMA_MISMATCH",
        }
    )

    @classmethod
    def provider_failure(
        cls,
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        error: str,
        must_decide: bool,
        must_reply: bool,
    ) -> GMToolAgentOutcome:
        incomplete = cls._incomplete_followup(
            receipts=receipts,
            trace=trace,
            error=error,
            must_reply=must_reply,
        )
        if incomplete is not None:
            return incomplete
        mixed_followup = cls._mixed_followup_failure(
            receipts=receipts,
            trace=trace,
            error=error,
        )
        if mixed_followup is not None:
            return mixed_followup
        if GMToolReceiptPolicy.state_change_recovered(receipts):
            return GMToolAgentOutcome(
                handled=True,
                reply=GMToolReceiptPolicy.authoritative_reply(receipts),
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="状态工具已经成功提交；最终表达格式异常，使用权威回执安全收尾。",
            )
        fallback = GMToolReceiptPolicy.interrupted_reply(receipts)
        if fallback:
            return GMToolAgentOutcome(
                handled=True,
                reply=fallback,
                receipts=receipts,
                trace=trace,
                error=error,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="权威工具已经返回可公开的确定性结果；模型服务中断后使用工具回执安全收尾。",
            )
        if must_reply:
            return GMToolAgentOutcome(
                handled=True,
                reply=cls._provider_failure_reply(receipts=receipts, error=error),
                receipts=receipts,
                trace=trace,
                error=error,
                target="fu_gm",
                mode="gm_agent_unavailable",
                reason="工具智能体不可用；为避免旧解析器误写，当前消息失败关闭。",
            )
        if must_decide:
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                error=error,
                target="silent",
                mode="gm_agent_unavailable_silent",
                stop_astrbot=True,
                reason="工具智能体不可用；普通群消息静默失败关闭，且未改动状态。",
            )
        return GMToolAgentOutcome(handled=False, trace=trace, error=error)

    @classmethod
    def exhausted(
        cls,
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        must_decide: bool,
        must_reply: bool,
    ) -> GMToolAgentOutcome:
        error = "GM工具循环达到最大次数。"
        incomplete = cls._incomplete_followup(
            receipts=receipts,
            trace=trace,
            error=error,
            must_reply=must_reply,
        )
        if incomplete is not None:
            return incomplete
        mixed_followup = cls._mixed_followup_failure(
            receipts=receipts,
            trace=trace,
            error=error,
        )
        if mixed_followup is not None:
            return mixed_followup
        if GMToolReceiptPolicy.state_change_recovered(receipts):
            return GMToolAgentOutcome(
                handled=True,
                reply=GMToolReceiptPolicy.authoritative_reply(receipts),
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="状态工具已经成功提交；达到循环上限后使用权威回执安全收尾。",
            )
        fallback = GMToolReceiptPolicy.interrupted_reply(receipts)
        if fallback:
            return GMToolAgentOutcome(
                handled=True,
                reply=fallback,
                receipts=receipts,
                trace=trace,
                error=error,
            )
        if must_reply:
            rejection = cls._rule_rejection_reply(receipts)
            if rejection:
                return GMToolAgentOutcome(
                    handled=True,
                    reply=rejection,
                    receipts=receipts,
                    trace=trace,
                    error=error,
                    target="fu_gm",
                    mode="gm_agent_unresolved",
                    reason="模型没有修正最后一个具体规则拒绝；向玩家说明原因并保留待决选择。",
                )
            return GMToolAgentOutcome(
                handled=True,
                reply=(
                    "模型在本轮没有形成可执行的处理结果；"
                    "这条消息没有记入或结算，请稍后重试。"
                ),
                receipts=receipts,
                trace=trace,
                error=error,
                target="fu_gm",
                mode="gm_agent_unresolved",
                reason="智能体未能在工具循环内形成可靠处理；当前消息失败关闭。",
            )
        if must_decide:
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                error=error,
                target="silent",
                mode="gm_agent_unresolved_silent",
                stop_astrbot=True,
                reason="智能体未能可靠处理普通群消息；静默失败关闭且未改动状态。",
            )
        return GMToolAgentOutcome(
            handled=False,
            receipts=receipts,
            trace=trace,
            error=error,
        )

    @classmethod
    def tool_retry_exhausted(
        cls,
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        must_reply: bool,
        error: str,
    ) -> GMToolAgentOutcome:
        """Stop a repeatedly invalid subordinate-agent tool output."""

        mixed_followup = cls._mixed_followup_failure(
            receipts=receipts,
            trace=trace,
            error=error,
        )
        if mixed_followup is not None:
            return mixed_followup
        if GMToolReceiptPolicy.state_change_recovered(receipts):
            return GMToolAgentOutcome(
                handled=True,
                reply=GMToolReceiptPolicy.authoritative_reply(receipts),
                receipts=receipts,
                trace=trace,
                error=error,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="已有权威状态提交；后续工具输出连续无效，保留已提交结果并停止重试。",
            )
        if must_reply:
            rejection = cls._rule_rejection_reply(receipts)
            if rejection:
                return GMToolAgentOutcome(
                    handled=True,
                    reply=rejection,
                    receipts=receipts,
                    trace=trace,
                    error=error,
                    target="fu_gm",
                    mode="gm_agent_unresolved",
                    reason="同一工具输出连续无效；公开最后一个可操作的规则原因。",
                )
            return GMToolAgentOutcome(
                handled=True,
                reply="这次没有结算成功；待决选择仍然保留。这不是你的行动失败。",
                receipts=receipts,
                trace=trace,
                error=error,
                target="fu_gm",
                mode="gm_agent_unresolved",
                reason="同一工具输出连续无效；未改用其他写工具绕过校验。",
            )
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            receipts=receipts,
            trace=trace,
            error=error,
            target="silent",
            mode="gm_agent_unresolved_silent",
            stop_astrbot=True,
            reason="同一工具输出连续无效；静默停止并保留后台诊断。",
        )

    @classmethod
    def _provider_failure_reply(
        cls,
        *,
        receipts: list[GMToolReceipt],
        error: str,
    ) -> str:
        """Expose the failure category without leaking provider internals."""

        clean_error = str(error or "").lower()
        rejection = cls._latest_rule_rejection(receipts)
        has_rule_rejection = rejection is not None
        circuit_open = "provider circuit is open" in clean_error
        timed_out = any(
            marker in clean_error
            for marker in (
                "timeout",
                "timed out",
                "wall-clock budget",
                "deadline",
                "handshake",
                "read operation",
            )
        )
        if has_rule_rejection and (timed_out or circuit_open):
            reason = cls._receipt_reason(rejection)
            outage = "模型服务暂时不可用" if circuit_open else "模型调用又超时了"
            return (
                f"这次还没结算：{reason}。随后{outage}；"
                "待决选择仍然保留。这不是你的行动失败。"
            )
        if circuit_open:
            return (
                "模型服务暂时不可用，这条消息没有记入或结算。"
                "待决选择仍然保留，请稍后再试。"
            )
        if timed_out:
            return (
                "模型调用超时，这条消息没有记入或结算。"
                "待决选择仍然保留，请稍后重试。"
            )
        if has_rule_rejection:
            return cls._rule_rejection_reply(receipts)
        return (
            "模型服务调用失败或没有返回可用结果；"
            "这条消息没有记入或结算，请稍后重试。"
        )

    @classmethod
    def _latest_rule_rejection(
        cls,
        receipts: list[GMToolReceipt],
    ) -> GMToolReceipt | None:
        """Find the latest safe, player-actionable rule rejection."""

        for receipt in reversed(receipts):
            if receipt.ok or not receipt.error_code:
                continue
            if receipt.error_code in cls._PROTOCOL_ERROR_CODES:
                continue
            if receipt.tool_name not in {"resolve_rule_window", "resolve_gm_opportunity"}:
                continue
            if receipt.public_fallback_reply or receipt.message:
                return receipt
        return None

    @staticmethod
    def _receipt_reason(receipt: GMToolReceipt | None) -> str:
        if receipt is None:
            return "规则参数仍不完整"
        reason = str(receipt.message or receipt.public_fallback_reply or "").strip()
        return reason.rstrip("。！？!?；;")

    @classmethod
    def _rule_rejection_reply(cls, receipts: list[GMToolReceipt]) -> str:
        rejection = cls._latest_rule_rejection(receipts)
        if rejection is None:
            return ""
        if rejection.lock_public_reply and rejection.public_fallback_reply:
            return str(rejection.public_fallback_reply).strip()
        reason = cls._receipt_reason(rejection)
        return (
            f"这次还没结算：{reason}。"
            "待决选择仍然保留；这不是你的行动失败。"
        )

    @classmethod
    def _mixed_followup_failure(
        cls,
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        error: str,
    ) -> GMToolAgentOutcome | None:
        """Expose partial success when prose failed after a committed choice."""

        if not GMToolReceiptPolicy.mixed_message_followup_pending(receipts):
            return None
        clean_error = str(error or "").lower()
        if "provider circuit is open" in clean_error:
            cause = "模型服务暂时不可用"
        elif any(
            marker in clean_error
            for marker in (
                "timeout",
                "timed out",
                "wall-clock budget",
                "deadline",
                "handshake",
                "read operation",
            )
        ):
            cause = "模型调用超时"
        elif "最大次数" in str(error or "") or "循环" in str(error or ""):
            cause = "模型没有按收尾协议完成回答"
        else:
            cause = "模型服务没有返回可用的后续回答"
        authoritative = GMToolReceiptPolicy.authoritative_reply(receipts)
        notice = (
            f"规则选择已经结算；你同句里的另一个问题因{cause}未能回答，"
            "请再问一次。"
        )
        reply = "\n".join(item for item in (authoritative, notice) if item)
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
            receipts=receipts,
            trace=trace,
            error=error,
            target="fu_gm",
            mode="gm_agent_partial",
            reason="规则状态已经提交，但同句独立问题的模型回答未完成；明确报告部分成功。",
        )

    @staticmethod
    def _incomplete_followup(
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        error: str,
        must_reply: bool,
    ) -> GMToolAgentOutcome | None:
        required = GMToolReceiptPolicy.required_followup_tools(receipts)
        if not required or not any(
            receipt.ok and receipt.state_changed for receipt in receipts
        ):
            return None
        return GMToolAgentOutcome(
            handled=True,
            reply=(
                "前一步已经完成，但后续处理没有接稳；我先停在当前状态，没有继续推进。请再说一次刚才要求的后续动作。"
                if must_reply
                else ""
            ),
            receipts=receipts,
            trace=trace,
            error=error,
            target="fu_gm" if must_reply else "silent",
            mode="gm_agent_incomplete_followup",
            stop_astrbot=True,
            reason=(
                "已提交的工具仍要求后续能力："
                + "、".join(sorted(required))
                + "；未把部分提交伪装成完整事务。"
            ),
        )
