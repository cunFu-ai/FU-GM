from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolMutationTransaction,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy


@dataclass
class GMBatchToolTransaction:
    """Outer rollback scope for one model-issued ``call_tools`` batch."""

    transaction: GMToolMutationTransaction | None
    ledger: Any
    receipts: list[GMToolReceipt]
    history: list[dict[str, object]]
    receipt_start: int
    successful_write_calls_before: set[str]
    successful_tool_calls_before: dict[str, int]
    state_summary_before: dict[str, object]
    context: GMToolExecutionContext
    action_commit_metadata_before: dict[str, object]

    @classmethod
    def begin(
        cls,
        *,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        ledger: Any,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        calls: list[dict[str, object]],
    ) -> "GMBatchToolTransaction":
        action_commit_keys = (
            GMToolReceiptPolicy.COMMITTED_ACTION_ACTORS_KEY,
            GMToolReceiptPolicy.COMMITTED_ACTION_ROUNDS_KEY,
            GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY,
        )
        return cls(
            transaction=registry.begin_batch_transaction(
                [str(call.get("tool_name") or "") for call in calls],
                context,
            ),
            ledger=ledger,
            receipts=receipts,
            history=history,
            receipt_start=len(receipts),
            successful_write_calls_before=set(ledger.successful_write_calls),
            successful_tool_calls_before=dict(ledger.successful_tool_calls),
            state_summary_before=deepcopy(ledger.state_summary),
            context=context,
            action_commit_metadata_before={
                key: deepcopy(context.metadata[key])
                for key in action_commit_keys
                if key in context.metadata
            },
        )

    def commit(self) -> None:
        if self.transaction is not None:
            self.transaction.commit()

    def rollback(
        self,
        batch_receipts: list[dict[str, object]],
        *,
        reason: str,
    ) -> None:
        if self.transaction is not None:
            self.transaction.rollback()
        rolled_back_tools = [
            receipt.tool_name
            for receipt in self.receipts[self.receipt_start :]
            if receipt.ok and receipt.state_changed
        ]
        failures = [
            receipt
            for receipt in self.receipts[self.receipt_start :]
            if not receipt.ok
        ]
        del self.receipts[self.receipt_start :]
        self.receipts.extend(failures)
        self.ledger.successful_write_calls = self.successful_write_calls_before
        self.ledger.successful_tool_calls = self.successful_tool_calls_before
        self.ledger.state_summary.clear()
        self.ledger.state_summary.update(deepcopy(self.state_summary_before))
        for key in (
            GMToolReceiptPolicy.COMMITTED_ACTION_ACTORS_KEY,
            GMToolReceiptPolicy.COMMITTED_ACTION_ROUNDS_KEY,
            GMToolReceiptPolicy.REQUIRED_FOLLOWUP_CONTEXT_KEY,
        ):
            self.context.metadata.pop(key, None)
        self.context.metadata.update(deepcopy(self.action_commit_metadata_before))
        for item in batch_receipts:
            if bool(item.get("ok")) and bool(item.get("state_changed")):
                item.setdefault("result", {})["rolled_back"] = True
                item["state_changed"] = False
        self.history.append(
            {
                "batch_rollback": {
                    "reason": reason,
                    "rolled_back_tools": rolled_back_tools,
                    "message": (
                        "本批次的先前写入已全部回滚；"
                        "修正失败调用后重新提交整批必要动作。"
                    ),
                }
            }
        )
