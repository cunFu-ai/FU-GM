from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace

from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolMutationTransaction,
    GMToolReceipt,
    GMToolRegistry,
)


@dataclass
class GMMessageToolTransaction:
    """Outer transaction spanning every tool call made for one table message.

    Individual tools and one ``call_tools`` batch already have rollback
    envelopes. The model may, however, satisfy a mandatory follow-up over
    several iterations. This scope keeps an additional pre-call snapshot for
    every mutating tool so a provider failure cannot leave a preparatory write
    committed without its public consequence.
    """

    registry: GMToolRegistry
    context: GMToolExecutionContext
    state_summary: dict[str, object]
    transactions: list[GMToolMutationTransaction] = field(default_factory=list)
    original_campaign_id: str = ""
    original_gate_status: str = ""
    original_metadata: dict[str, object] = field(default_factory=dict)
    original_state_summary: dict[str, object] = field(default_factory=dict)
    active: bool = True

    @classmethod
    def begin(
        cls,
        *,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
    ) -> "GMMessageToolTransaction":
        return cls(
            registry=registry,
            context=context,
            state_summary=state_summary,
            original_campaign_id=str(context.campaign_id or ""),
            original_gate_status=str(context.gate_status or ""),
            original_metadata=deepcopy(context.metadata),
            original_state_summary=deepcopy(state_summary),
        )

    @property
    def has_mutating_calls(self) -> bool:
        return bool(self.transactions)

    def prepare(self, tool_name: str, arguments: object) -> str:
        """Capture one outer snapshot before the registry executes a write."""

        if not self.active:
            return "消息事务已经结束。"
        try:
            transaction = self.registry.begin_message_transaction(
                tool_name,
                arguments,
                self.context,
            )
        except Exception as exc:
            return str(exc) or exc.__class__.__name__
        if transaction is not None:
            self.transactions.append(transaction)
        return ""

    def commit(self) -> str:
        if not self.active:
            return ""
        errors: list[str] = []
        for transaction in self.transactions:
            try:
                transaction.commit()
            except Exception as exc:
                errors.append(str(exc) or exc.__class__.__name__)
        if not errors:
            self.active = False
        return "；".join(errors)

    def rollback(self) -> str:
        if not self.active:
            return ""
        errors: list[str] = []
        for transaction in reversed(self.transactions):
            try:
                transaction.rollback()
            except Exception as exc:
                errors.append(str(exc) or exc.__class__.__name__)
        self.context.campaign_id = self.original_campaign_id
        self.context.gate_status = self.original_gate_status
        self.context.metadata.clear()
        self.context.metadata.update(deepcopy(self.original_metadata))
        self.state_summary.clear()
        self.state_summary.update(deepcopy(self.original_state_summary))
        self.active = False
        return "；".join(errors)

    @staticmethod
    def mark_receipts_rolled_back(receipts: list[GMToolReceipt]) -> list[str]:
        rolled_back: list[str] = []
        for receipt in receipts:
            if not (receipt.ok and receipt.state_changed):
                continue
            rolled_back.append(receipt.tool_name)
            receipt.state_changed = False
            receipt.result = dict(receipt.result or {})
            receipt.result["rolled_back"] = True
            receipt.narrative_events = [
                replace(
                    event,
                    status="rolled_back",
                    outcome="",
                    public_facts=(),
                )
                for event in receipt.narrative_events
            ]
        return rolled_back

    @classmethod
    def mark_trace_rolled_back(
        cls,
        trace: list[dict[str, object]],
    ) -> None:
        """Keep diagnostics truthful after the outer transaction is restored."""

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if (
                    value.get("ok") is True
                    and value.get("state_changed") is True
                    and "tool_name" in value
                ):
                    value["state_changed"] = False
                    result = value.get("result")
                    if not isinstance(result, dict):
                        result = {}
                        value["result"] = result
                    result["rolled_back"] = True
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(trace)
