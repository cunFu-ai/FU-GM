from __future__ import annotations

from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.gm_tool_contracts import GMToolReceipt
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


class GMToolAgentFailurePolicy:
    """Fail closed without confusing table ownership with reply obligation."""

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
            circuit_open = "provider circuit is open" in str(error or "").lower()
            return GMToolAgentOutcome(
                handled=True,
                reply=(
                    "主持服务暂时不可用，这条消息没有记入或结算。请稍后再试。"
                    if circuit_open
                    else "刚才这句我没接稳，先没有记入或结算。麻烦再说一次。"
                ),
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
            return GMToolAgentOutcome(
                handled=True,
                reply="这句我还没判断清楚，先不改动团里的任何内容。你可以换个说法再告诉我。",
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
            return GMToolAgentOutcome(
                handled=True,
                reply="刚才这句我没接稳，先没有记入或结算。麻烦再说一次。",
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
