from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolMutationTransaction,
    GMToolReceipt,
    GMToolRegistry,
)


@dataclass
class GMMessageToolTransaction:
    """覆盖一条桌面消息中全部工具调用的外层事务。

    首次普通写入时只保留一份总快照，并持有逻辑写租约。消息内部的各个
    工具仍可单独回滚，但只有整条消息形成可交付结果后才统一提升状态版本。
    """

    registry: GMToolRegistry
    context: GMToolExecutionContext
    state_summary: dict[str, object]
    transactions: list[GMToolMutationTransaction] = field(default_factory=list)
    original_campaign_id: str = ""
    original_gate_status: str = ""
    original_metadata: dict[str, object] = field(default_factory=dict)
    original_state_summary: dict[str, object] = field(default_factory=dict)
    side_effect_lock: Any | None = field(default=None, repr=False)
    active: bool = True
    stale_before_commit: bool = False

    @classmethod
    def begin(
        cls,
        *,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        side_effect_lock: Any | None = None,
    ) -> "GMMessageToolTransaction":
        transaction = cls(
            registry=registry,
            context=context,
            state_summary=state_summary,
            original_campaign_id=str(context.campaign_id or ""),
            original_gate_status=str(context.gate_status or ""),
            original_metadata=deepcopy(context.metadata),
            original_state_summary=deepcopy(state_summary),
            side_effect_lock=side_effect_lock,
        )
        context.metadata.setdefault(
            "_gm_message_transaction_id",
            uuid4().hex,
        )
        return transaction

    @property
    def has_mutating_calls(self) -> bool:
        return bool(self.transactions)

    def mark_state_changed(self) -> None:
        """标记消息内至少有一次权威写入，供最终提交统一升级版本。"""

        for transaction in self.transactions:
            setter = getattr(transaction, "set_state_changed", None)
            if callable(setter):
                setter(True)

    def prepare(self, tool_name: str, arguments: object) -> str:
        """在首次普通写入前保留一份消息级总快照。"""

        if not self.active:
            return "消息事务已经结束。"
        side_effect = self.registry.side_effect(tool_name)
        if side_effect in {"", "read"}:
            return ""
        if side_effect != "replace_state" and any(
            str(getattr(getattr(item, "definition", None), "side_effect", ""))
            in {"write", "write_pending"}
            for item in self.transactions
        ):
            return ""
        try:
            with self._lock_context():
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

    def commit(
        self,
        *,
        freshness_guard: Callable[[], bool] | None = None,
    ) -> str:
        if not self.active:
            return ""
        errors: list[str] = []
        with self._lock_context():
            if freshness_guard is not None and self.transactions:
                try:
                    still_current = bool(freshness_guard())
                except Exception:
                    still_current = False
                if not still_current:
                    self.stale_before_commit = True
                    return "STALE_AGENT_REQUEST"
            for transaction in self.transactions:
                try:
                    transaction.commit()
                except Exception as exc:
                    errors.append(str(exc) or exc.__class__.__name__)
        if errors:
            self.context.discard_post_commit_callbacks()
            return "；".join(errors)

        # Authoritative child transactions have committed and released their
        # write lease before any derived/background work is allowed to queue.
        self.active = False
        diagnostics: list[dict[str, str]] = []
        for name, callback in self.context.take_post_commit_callbacks():
            try:
                callback()
            except Exception as exc:
                diagnostics.append(
                    {
                        "name": name,
                        "error": (str(exc) or exc.__class__.__name__)[:500],
                    }
                )
        if diagnostics:
            self.context.metadata["_gm_post_commit_diagnostics"] = diagnostics
        return "；".join(errors)

    def rollback(self) -> str:
        if not self.active:
            return ""
        self.context.discard_post_commit_callbacks()
        errors: list[str] = []
        version_conflict = deepcopy(
            self.context.metadata.get("_gm_campaign_version_conflict")
        )
        with self._lock_context():
            for transaction in reversed(self.transactions):
                try:
                    transaction.rollback()
                except Exception as exc:
                    errors.append(str(exc) or exc.__class__.__name__)
        self.context.campaign_id = self.original_campaign_id
        self.context.gate_status = self.original_gate_status
        self.context.metadata.clear()
        self.context.metadata.update(deepcopy(self.original_metadata))
        if version_conflict:
            self.context.metadata["_gm_campaign_version_conflict"] = (
                version_conflict
            )
        self.state_summary.clear()
        self.state_summary.update(deepcopy(self.original_state_summary))
        self.active = False
        return "；".join(errors)

    def _lock_context(self):
        return self.side_effect_lock if self.side_effect_lock is not None else nullcontext()

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
