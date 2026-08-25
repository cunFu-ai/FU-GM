from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
import logging
from pathlib import Path
from typing import Any, Callable, Protocol


LOGGER = logging.getLogger(__name__)


def json_safe_value(value: Any) -> Any:
    """Convert domain values into the lossless JSON shape used by GM tools.

    Rule transactions intentionally keep rich dataclasses such as
    ``RollOutcome`` in memory.  Those values must become plain data before a
    state summary, receipt, or model request crosses the JSON protocol
    boundary; otherwise a valid pending decision can crash the next agent
    iteration.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return json_safe_value(asdict(value))
    if isinstance(value, Enum):
        return json_safe_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): json_safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, set):
        return [json_safe_value(item) for item in sorted(value, key=str)]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True)
class GMToolParameter:
    name: str
    kind: str
    description: str
    required: bool = False
    enum: tuple[str, ...] = ()
    schema_details: dict[str, object] = field(default_factory=dict)
    source: str = "model"

    def schema(self) -> dict[str, object]:
        result: dict[str, object] = {
            "type": self.kind,
            "description": self.description,
        }
        if self.enum:
            result["enum"] = list(self.enum)
        if self.schema_details:
            result.update(deepcopy(self.schema_details))
        return result


@dataclass
class GMToolExecutionContext:
    campaign_id: str
    session_id: str
    channel_id: str
    speaker: str
    gate_status: str
    is_private: bool = False
    directly_addressed: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    _post_commit_callbacks: list[tuple[str, Callable[[], object]]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def agent_deadline_monotonic(self) -> float | None:
        """Return the enclosing core-agent deadline carried by the coordinator."""

        raw = self.metadata.get("_gm_agent_deadline_monotonic")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def defer_post_commit(
        self,
        name: str,
        callback: Callable[[], object],
    ) -> None:
        """Register a derived side effect that is safe only after commit.

        The queue deliberately lives outside ``metadata`` so transaction
        snapshots never deepcopy closures or leak them into model-visible
        state.  ``GMMessageToolTransaction`` is the sole owner of draining or
        discarding the queue.
        """

        if not callable(callback):
            raise TypeError("post-commit callback must be callable")
        self._post_commit_callbacks.append((str(name or "post_commit"), callback))

    def take_post_commit_callbacks(
        self,
    ) -> list[tuple[str, Callable[[], object]]]:
        callbacks = list(self._post_commit_callbacks)
        self._post_commit_callbacks.clear()
        return callbacks

    def discard_post_commit_callbacks(self) -> None:
        self._post_commit_callbacks.clear()


@dataclass(frozen=True)
class GMToolPacingEvent:
    """Structured episode evidence emitted by an authoritative tool.

    Tools report only facts they own.  The message coordinator later merges
    all events from one agent transaction, so a dialogue followed by a scene
    transition still counts as one player turn rather than two.
    """

    player_action: bool = False
    action_summary: str = ""
    consequence: str = ""
    local_payoff: str = ""
    reveal: str = ""
    reversal: bool = False
    climax: str = ""
    opposition_move: str = ""
    public_image: str = ""
    local_question_changed: bool = False
    local_question_resolved: bool = False
    deliberate_cliffhanger: bool = False
    signature_image_evolved: bool = False
    callback_to_previous: str = ""
    gm_beat_purpose: str = ""

    @property
    def meaningful(self) -> bool:
        return bool(
            self.player_action
            or self.consequence
            or self.local_payoff
            or self.reveal
            or self.reversal
            or self.climax
            or self.opposition_move
            or self.public_image
            or self.local_question_changed
            or self.local_question_resolved
            or self.deliberate_cliffhanger
            or self.signature_image_evolved
            or self.callback_to_previous
        )


@dataclass(frozen=True)
class GMNarrativeEvent:
    """Provenance-safe evidence produced by an authoritative tool.

    ``declaration`` preserves what a player said they were attempting.  It is
    deliberately separate from ``outcome`` and ``public_facts``: a declaration
    such as "示意巡守接过牌子" must never become "巡守已经接过牌子" unless a
    rules or scene tool explicitly commits that consequence.
    """

    event_type: str
    tool_name: str
    status: str = "tool_committed"
    source_event_id: str = ""
    source_message_id: str = ""
    source_speaker: str = ""
    declaration: str = ""
    outcome: str = ""
    public_facts: tuple[str, ...] = ()

    @property
    def meaningful(self) -> bool:
        return bool(
            self.event_type
            and (
                self.source_event_id
                or self.declaration
                or self.outcome
                or self.public_facts
            )
        )


@dataclass
class GMToolReceipt:
    """Authoritative result of one typed GM capability.

    A receipt is the only bridge from a model decision to committed game
    state.  Failure receipts can carry a locked clarification, but can never
    claim that state changed.
    """

    tool_name: str
    ok: bool
    result: dict[str, object] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    correction_hint: str = ""
    retryable: bool = False
    state_changed: bool = False
    public_fallback_reply: str = ""
    lock_public_reply: bool = False
    pacing_events: list[GMToolPacingEvent] = field(default_factory=list)
    narrative_events: list[GMNarrativeEvent] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        tool_name: str,
        *,
        result: dict[str, object] | None = None,
        state_changed: bool = False,
        public_reply: str = "",
        lock_public_reply: bool = False,
        pacing_events: list[GMToolPacingEvent] | None = None,
        narrative_events: list[GMNarrativeEvent] | None = None,
    ) -> "GMToolReceipt":
        return cls(
            tool_name=tool_name,
            ok=True,
            result=dict(result or {}),
            state_changed=bool(state_changed),
            public_fallback_reply=str(public_reply or "").strip(),
            lock_public_reply=bool(lock_public_reply),
            pacing_events=list(pacing_events or []),
            narrative_events=list(narrative_events or []),
        )

    @classmethod
    def failure(
        cls,
        tool_name: str,
        error_code: str,
        message: str,
        correction_hint: str,
        *,
        retryable: bool = True,
        result: dict[str, object] | None = None,
        public_reply: str = "",
        lock_public_reply: bool = False,
    ) -> "GMToolReceipt":
        return cls(
            tool_name=tool_name,
            ok=False,
            result=dict(result or {}),
            error_code=str(error_code or "TOOL_REJECTED").strip(),
            message=str(message or "工具没有执行。"),
            correction_hint=str(correction_hint or "修正参数后重新调用。"),
            retryable=bool(retryable),
            state_changed=False,
            public_fallback_reply=str(public_reply or "").strip(),
            lock_public_reply=bool(lock_public_reply),
        )

    def normalize(self, *, expected_tool_name: str) -> "GMToolReceipt":
        self.tool_name = str(expected_tool_name or self.tool_name or "").strip()
        if not isinstance(self.result, dict):
            self.result = {}
        if not self.ok:
            self.state_changed = False
            self.error_code = str(self.error_code or "TOOL_REJECTED").strip()
            self.message = str(self.message or "工具没有执行。")
            if self.retryable and not str(self.correction_hint or "").strip():
                self.correction_hint = "修正参数后重新调用。"
        return self

    def to_dict(self) -> dict[str, object]:
        return json_safe_value(asdict(self))


GMToolHandler = Callable[[GMToolExecutionContext, dict[str, object]], GMToolReceipt]
GMToolFreshnessGuard = Callable[
    ["GMToolDefinition", dict[str, object], GMToolExecutionContext],
    bool,
]
GMToolAdmissionGuard = Callable[
    ["GMToolDefinition", dict[str, object], GMToolExecutionContext],
    "GMToolReceipt | None",
]


class GMToolMutationTransaction(Protocol):
    """Host-owned transaction around one validated mutating tool handler."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class GMToolTransactionFactory(Protocol):
    def __call__(
        self,
        definition: "GMToolDefinition",
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> GMToolMutationTransaction | None: ...


@dataclass(frozen=True)
class GMToolDefinition:
    name: str
    description: str
    handler: GMToolHandler
    parameters: tuple[GMToolParameter, ...] = ()
    side_effect: str = "read"
    max_successful_calls_per_message: int = 0
    # 只约束下一轮发给模型的结果副本。权威回执、审计日志和存档不截断。
    max_model_result_chars: int = 8000
    # 默认串行且非破坏性。只有基于冻结快照的纯读取工具才应显式开启并发。
    is_concurrency_safe: bool = False
    # 删除存档、覆盖地图等不可自然撤销的操作需要显式声明。
    is_destructive: bool = False
    # 同组工具可由编排器延迟到侧链执行；空字符串表示必须留在主事务。
    defer_group: str = ""
    # 动态提示裁剪只是一项成本优化。公开、安全、只读的查询可以在玩家
    # 明确呼叫GM时按模型选择补授，而无需把所有读取Schema常驻上下文。
    allow_addressed_dynamic_grant: bool = False

    def schema(self) -> dict[str, object]:
        properties = {
            parameter.name: parameter.schema()
            for parameter in self.parameters
            if parameter.source == "model"
        }
        properties["source_event_id"] = {
            "type": "string",
            "description": (
                "当前桌面轮次只有一条消息时省略；有多条消息时，"
                "写工具必须填写触发本次调用的current_turn事件ID。"
            ),
        }
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect,
            "execution": {
                "concurrency_safe": self.is_concurrency_safe,
                "destructive": self.is_destructive,
                "defer_group": self.defer_group,
                "max_model_result_chars": max(
                    0,
                    int(self.max_model_result_chars),
                ),
            },
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [
                    parameter.name
                    for parameter in self.parameters
                    if parameter.required and parameter.source == "model"
                ],
                "additionalProperties": False,
            },
        }


class GMToolRegistry:
    """Typed capability boundary between the GM agent and domain services."""

    def __init__(
        self,
        *,
        transaction_factory: GMToolTransactionFactory | None = None,
        admission_guard: GMToolAdmissionGuard | None = None,
    ) -> None:
        self._tools: dict[str, GMToolDefinition] = {}
        self._transaction_factory = transaction_factory
        self._admission_guard = admission_guard

    def set_transaction_factory(
        self,
        factory: GMToolTransactionFactory | None,
    ) -> None:
        self._transaction_factory = factory

    def set_admission_guard(
        self,
        guard: GMToolAdmissionGuard | None,
    ) -> None:
        self._admission_guard = guard

    def register(self, definition: GMToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"重复注册 GM 工具：{definition.name}")
        if definition.is_concurrency_safe and definition.side_effect != "read":
            raise ValueError(
                f"写工具不能声明为并发安全：{definition.name}"
            )
        if definition.is_destructive and definition.side_effect == "read":
            raise ValueError(
                f"只读工具不能声明为破坏性操作：{definition.name}"
            )
        if (
            definition.allow_addressed_dynamic_grant
            and definition.side_effect != "read"
        ):
            raise ValueError(
                f"只有只读工具可以允许直接呼叫时动态补授：{definition.name}"
            )
        if int(definition.max_model_result_chars) < 0:
            raise ValueError(
                f"工具结果预算不能为负数：{definition.name}"
            )
        self._tools[definition.name] = definition

    def schemas(self, names: set[str] | None = None) -> list[dict[str, object]]:
        return [
            self._tools[name].schema()
            for name in sorted(self._tools)
            if names is None or name in names
        ]

    def execution_metadata(self, name: str) -> dict[str, object]:
        definition = self._tools.get(str(name or "").strip())
        if definition is None:
            return {}
        return {
            "side_effect": definition.side_effect,
            "concurrency_safe": definition.is_concurrency_safe,
            "destructive": definition.is_destructive,
            "defer_group": definition.defer_group,
            "allow_addressed_dynamic_grant": bool(
                definition.allow_addressed_dynamic_grant
            ),
            "max_model_result_chars": max(
                0,
                int(definition.max_model_result_chars),
            ),
        }

    def successful_call_limit(self, name: str) -> int:
        definition = self._tools.get(str(name or "").strip())
        if definition is None:
            return 0
        return max(0, int(definition.max_successful_calls_per_message))

    def model_result_char_budget(self, name: str) -> int:
        definition = self._tools.get(str(name or "").strip())
        if definition is None:
            return 0
        return max(0, int(definition.max_model_result_chars))

    def is_read_only(self, name: str) -> bool:
        """Return whether a registered capability cannot mutate game state."""

        definition = self._tools.get(str(name or "").strip())
        return bool(definition is not None and definition.side_effect == "read")

    def allows_addressed_dynamic_grant(self, name: str) -> bool:
        """Return whether a public read may bypass prompt-schema narrowing."""

        definition = self._tools.get(str(name or "").strip())
        return bool(
            definition is not None
            and definition.side_effect == "read"
            and definition.allow_addressed_dynamic_grant
        )

    def side_effect(self, name: str) -> str:
        """Return the declared mutation class for one registered capability."""

        definition = self._tools.get(str(name or "").strip())
        return str(definition.side_effect) if definition is not None else ""

    def begin_batch_transaction(
        self,
        tool_names: list[str],
        context: GMToolExecutionContext,
    ) -> GMToolMutationTransaction | None:
        """Create one outer rollback envelope for an ordinary write batch.

        Every tool still owns its normal transaction. The outer snapshot only
        matters when a later call fails after a preparatory write already
        succeeded. State-replacement tools retain their dedicated filesystem
        semantics and therefore are never mixed into this envelope.
        """

        if self._transaction_factory is None:
            return None
        definitions = [
            self._tools[name]
            for raw_name in tool_names
            if (name := str(raw_name or "").strip()) in self._tools
        ]
        mutating = [item for item in definitions if item.side_effect != "read"]
        if not mutating or any(item.side_effect == "replace_state" for item in mutating):
            return None
        return self._transaction_factory(
            mutating[0],
            {"batch_tools": [item.name for item in definitions]},
            context,
        )

    def begin_message_transaction(
        self,
        name: str,
        arguments: object,
        context: GMToolExecutionContext,
    ) -> GMToolMutationTransaction | None:
        """Capture an outer snapshot for one mutating call in a GM message."""

        if self._transaction_factory is None:
            return None
        definition = self._tools.get(str(name or "").strip())
        if definition is None or definition.side_effect == "read":
            return None
        effective_arguments = dict(arguments) if isinstance(arguments, dict) else {}
        effective_arguments["_gm_transaction_scope"] = "message"
        return self._transaction_factory(
            definition,
            effective_arguments,
            context,
        )

    def canonical_fingerprint_arguments(
        self,
        name: str,
        arguments: object,
        context: GMToolExecutionContext,
    ) -> object:
        """Normalize provenance fields that execution resolves server-side.

        Models may explicitly echo ``source_event_id`` or omit it for a
        single-message turn.  Execution treats both forms identically, so
        batch de-duplication must do the same.  Multi-message turns retain
        their explicit event ids because each one can authorize a distinct
        player's mutation.
        """

        if not isinstance(arguments, dict):
            return arguments
        normalized = dict(arguments)
        if bool(context.metadata.get("system_gm_beat_request")):
            normalized.pop("source_event_id", None)
            return normalized
        raw_events = context.metadata.get("current_turn_events")
        events = (
            [item for item in raw_events if isinstance(item, dict)]
            if isinstance(raw_events, list)
            else []
        )
        if len(events) != 1:
            return normalized
        event_id = str(events[0].get("event_id") or "").strip()
        if event_id:
            normalized["source_event_id"] = event_id
        else:
            normalized.pop("source_event_id", None)
        return normalized

    def execute(
        self,
        name: str,
        arguments: object,
        context: GMToolExecutionContext,
        *,
        freshness_guard: GMToolFreshnessGuard | None = None,
        side_effect_lock: Any | None = None,
    ) -> GMToolReceipt:
        definition = self._tools.get(str(name or "").strip())
        if definition is None:
            return GMToolReceipt.failure(
                str(name or "").strip(),
                "UNKNOWN_TOOL",
                "没有这个工具。",
                "请从 available_tools 中重新选择。",
            )
        if not isinstance(arguments, dict):
            return GMToolReceipt.failure(
                definition.name,
                "INVALID_ARGUMENTS",
                "工具参数必须是 JSON 对象。",
                "重新提交 arguments 对象。",
            )
        model_arguments = dict(arguments)
        source_event_id = str(
            model_arguments.pop("source_event_id", "") or ""
        ).strip()
        context, source_error = self._source_bound_context(
            definition,
            context,
            source_event_id=source_event_id,
        )
        if source_error is not None:
            return source_error
        system_owned = {
            parameter.name
            for parameter in definition.parameters
            if parameter.source != "model"
        }
        supplied_system_owned = sorted(set(model_arguments) & system_owned)
        if supplied_system_owned:
            return GMToolReceipt.failure(
                definition.name,
                "SYSTEM_ARGUMENT_NOT_ALLOWED",
                "工具包含由系统自动提供、模型不得提交的参数："
                + "、".join(supplied_system_owned),
                "删除这些系统参数，仅提交工具schema中公开的字段后重试。",
            )
        effective_arguments = dict(model_arguments)
        for parameter in definition.parameters:
            if parameter.source == "current_message":
                effective_arguments[parameter.name] = str(
                    context.metadata.get("current_message") or ""
                )
        validation_error = self._validate_arguments(definition, model_arguments)
        if validation_error is not None:
            return validation_error
        # Read tools need the same campaign lock to observe one coherent
        # snapshot while another channel is mutating or loading that campaign.
        # They still skip freshness checks and rollback transactions.
        execution_lock = (
            side_effect_lock
            if side_effect_lock is not None
            else nullcontext()
        )
        with execution_lock:
            if definition.side_effect != "read" and freshness_guard is not None:
                try:
                    still_current = bool(
                        freshness_guard(definition, dict(effective_arguments), context)
                    )
                except Exception:
                    still_current = False
                if not still_current:
                    return GMToolReceipt.failure(
                        definition.name,
                        "STALE_AGENT_REQUEST",
                        "这次智能体请求已经被更新的桌面消息取代。",
                        "停止执行，不要重试、不要公开回复。",
                        retryable=False,
                    )
            if self._admission_guard is not None:
                try:
                    admission_error = self._admission_guard(
                        definition,
                        dict(effective_arguments),
                        context,
                    )
                except Exception as exc:
                    return GMToolReceipt.failure(
                        definition.name,
                        "TOOL_ADMISSION_CHECK_FAILED",
                        f"工具准入检查失败：{exc}",
                        "停止当前写入并检查待决窗口与准入策略。",
                        retryable=False,
                    )
                if admission_error is not None:
                    return admission_error.normalize(
                        expected_tool_name=definition.name
                    )
            transaction: GMToolMutationTransaction | None = None
            if definition.side_effect != "read" and self._transaction_factory is not None:
                try:
                    transaction = self._transaction_factory(
                        definition,
                        dict(effective_arguments),
                        context,
                    )
                except Exception as exc:
                    return GMToolReceipt.failure(
                        definition.name,
                        "TOOL_TRANSACTION_START_FAILED",
                        f"无法建立工具事务：{exc}",
                        "不要执行或声称成功；修复事务服务后重试。",
                        retryable=False,
                    )
            try:
                receipt = definition.handler(context, dict(effective_arguments))
            except Exception as exc:
                LOGGER.exception(
                    "GM tool execution failed tool=%s campaign=%s session=%s exception=%s",
                    definition.name,
                    context.campaign_id,
                    context.session_id,
                    type(exc).__name__,
                )
                rollback_error = self._rollback_transaction(transaction)
                return GMToolReceipt.failure(
                    definition.name,
                    "TOOL_ROLLBACK_FAILED" if rollback_error else "TOOL_EXECUTION_FAILED",
                    (
                        f"工具失败且回滚也失败：{rollback_error}"
                        if rollback_error
                        else str(exc)
                    ),
                    (
                        "停止当前团的写入并检查事务日志。"
                        if rollback_error
                        else "公开状态保持未完成；可以向玩家说明本次操作暂时失败。"
                    ),
                    retryable=False,
                )
            if not isinstance(receipt, GMToolReceipt):
                rollback_error = self._rollback_transaction(transaction)
                return GMToolReceipt.failure(
                    definition.name,
                    "TOOL_ROLLBACK_FAILED" if rollback_error else "INVALID_TOOL_RECEIPT",
                    (
                        f"工具回执无效且回滚失败：{rollback_error}"
                        if rollback_error
                        else "工具处理器没有返回合法回执。"
                    ),
                    "检查工具实现，不能向玩家声称操作成功。",
                    retryable=False,
                )
            receipt = receipt.normalize(expected_tool_name=definition.name)
            self._attach_source_provenance(
                receipt,
                definition=definition,
                context=context,
            )
            if not receipt.ok:
                rollback_error = self._rollback_transaction(transaction)
                if rollback_error:
                    return GMToolReceipt.failure(
                        definition.name,
                        "TOOL_ROLLBACK_FAILED",
                        f"失败回执产生后无法恢复事务：{rollback_error}",
                        "停止当前团的写入并检查事务日志。",
                        retryable=False,
                    )
                return receipt
            if transaction is not None:
                try:
                    state_change_setter = getattr(
                        transaction,
                        "set_state_changed",
                        None,
                    )
                    if callable(state_change_setter):
                        state_change_setter(bool(receipt.state_changed))
                    transaction.commit()
                except Exception as exc:
                    rollback_error = self._rollback_transaction(transaction)
                    return GMToolReceipt.failure(
                        definition.name,
                        "TOOL_COMMIT_FAILED",
                        f"工具事务无法提交：{exc}"
                        + (f"；回滚也失败：{rollback_error}" if rollback_error else ""),
                        "公开状态保持未完成；检查持久化服务后重试。",
                        retryable=False,
                    )
            return receipt

    @classmethod
    def _attach_source_provenance(
        cls,
        receipt: GMToolReceipt,
        *,
        definition: GMToolDefinition,
        context: GMToolExecutionContext,
    ) -> None:
        """Attach exact source evidence without promoting intent into fact."""

        provenance = cls._source_provenance(context)
        if provenance:
            receipt.result.setdefault("source_event", provenance)
        if (
            not receipt.ok
            or not receipt.state_changed
            or definition.side_effect == "read"
            or receipt.narrative_events
        ):
            return
        outcome = cls._narrative_outcome(receipt.pacing_events)
        raw_public_facts = receipt.result.get("public_facts")
        fact_items = raw_public_facts if isinstance(raw_public_facts, list) else []
        public_facts = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in fact_items
                if str(item or "").strip()
            )
        )
        receipt.narrative_events.append(
            GMNarrativeEvent(
                event_type=cls._narrative_event_type(definition.name),
                tool_name=definition.name,
                source_event_id=str(provenance.get("event_id") or ""),
                source_message_id=str(provenance.get("message_id") or ""),
                source_speaker=str(provenance.get("speaker") or context.speaker),
                declaration=str(provenance.get("text") or ""),
                outcome=outcome,
                public_facts=public_facts,
            )
        )

    @staticmethod
    def _source_provenance(
        context: GMToolExecutionContext,
    ) -> dict[str, str]:
        metadata = context.metadata
        event_id = str(metadata.get("source_event_id") or "").strip()
        message_id = str(metadata.get("source_message_id") or "").strip()
        speaker = str(metadata.get("source_speaker") or context.speaker).strip()
        text = str(metadata.get("current_message") or "").strip()
        if not event_id:
            raw_events = metadata.get("current_turn_events")
            events = (
                [item for item in raw_events if isinstance(item, dict)]
                if isinstance(raw_events, list)
                else []
            )
            if len(events) == 1:
                selected = events[0]
                event_id = str(selected.get("event_id") or "").strip()
                message_id = str(selected.get("message_id") or message_id).strip()
                speaker = str(selected.get("speaker") or speaker).strip()
                text = str(selected.get("text") or text).strip()
        if not any((event_id, message_id, text)):
            return {}
        return {
            "event_id": event_id,
            "message_id": message_id,
            "speaker": speaker,
            "text": text[:800],
        }

    @staticmethod
    def _narrative_outcome(events: list[GMToolPacingEvent]) -> str:
        values: list[str] = []
        for event in events:
            for value in (
                event.consequence,
                event.local_payoff,
                event.reveal,
                event.opposition_move,
                event.climax,
            ):
                clean = " ".join(str(value or "").split()).strip()
                if clean and clean not in values:
                    values.append(clean)
        return "；".join(values)[:800]

    @staticmethod
    def _narrative_event_type(tool_name: str) -> str:
        name = str(tool_name or "").strip()
        if "clock" in name:
            return "clock_change"
        if "npc" in name:
            return "npc_response"
        if any(marker in name for marker in ("check", "roll", "action", "combat")):
            return "action_resolution"
        if any(marker in name for marker in ("scene", "travel", "dungeon")):
            return "scene_change"
        if any(marker in name for marker in ("session_zero", "hero", "world", "map")):
            return "campaign_setup_change"
        return "state_change"

    @staticmethod
    def _source_bound_context(
        definition: GMToolDefinition,
        context: GMToolExecutionContext,
        *,
        source_event_id: str,
    ) -> tuple[GMToolExecutionContext, GMToolReceipt | None]:
        """Bind a tool call to the exact speaker and text that authorized it."""

        if bool(context.metadata.get("system_gm_beat_request")):
            # A heartbeat is authorized by the scheduler, not by a player
            # message.  Models sometimes echo an event id from recent chat;
            # never let that stale provenance turn a valid GM beat into a
            # player-authored mutation (or reject the beat outright).
            metadata = dict(context.metadata)
            for key in (
                "source_event_id",
                "source_message_id",
                "source_speaker",
                "source_speaker_id",
            ):
                metadata.pop(key, None)
            metadata["current_turn_events"] = []
            return replace(context, metadata=metadata), None

        raw_events = context.metadata.get("current_turn_events")
        events = (
            [dict(item) for item in raw_events if isinstance(item, dict)]
            if isinstance(raw_events, list)
            else []
        )
        if not events:
            if source_event_id:
                return context, GMToolReceipt.failure(
                    definition.name,
                    "SOURCE_EVENT_NOT_AVAILABLE",
                    "当前请求没有可绑定的桌面事件。",
                    "删除source_event_id后按单消息调用。",
                )
            return context, None

        if len(events) == 1:
            # A single-message transaction has exactly one possible source.
            # Bind it server-side even if the model unnecessarily echoed a
            # stale event id from recent context.  Multi-speaker batches still
            # require an exact explicit id, where attribution is semantic.
            selected = events[0]
            metadata = dict(context.metadata)
            metadata.update(
                {
                    "current_message": str(selected.get("text") or ""),
                    "source_event_id": str(selected.get("event_id") or ""),
                    "source_message_id": str(selected.get("message_id") or ""),
                    "source_speaker": str(
                        selected.get("speaker") or context.speaker
                    ),
                    "source_speaker_id": str(selected.get("speaker_id") or ""),
                }
            )
            directly_addressed = bool(
                selected.get("is_at_gm")
                or selected.get("is_reply_to_gm")
                or selected.get("is_named_gm")
            )
            return (
                replace(
                    context,
                    speaker=str(selected.get("speaker") or context.speaker),
                    directly_addressed=directly_addressed,
                    metadata=metadata,
                ),
                None,
            )

        if len(events) > 1 and definition.side_effect != "read" and not source_event_id:
            return context, GMToolReceipt.failure(
                definition.name,
                "SOURCE_EVENT_REQUIRED",
                "同一桌面轮次包含多位发言者，写操作必须绑定来源事件。",
                "从current_turn.events选择真正授权该操作的event_id，作为source_event_id重试。",
                result={
                    "allowed_source_events": [
                        {
                            "event_id": str(item.get("event_id") or ""),
                            "speaker": str(item.get("speaker") or ""),
                            "text": str(item.get("text") or "")[:240],
                        }
                        for item in events
                    ]
                },
            )

        selected = None
        if source_event_id:
            selected = next(
                (
                    item
                    for item in events
                    if str(item.get("event_id") or "") == source_event_id
                ),
                None,
            )
            if selected is None:
                return context, GMToolReceipt.failure(
                    definition.name,
                    "SOURCE_EVENT_INVALID",
                    "source_event_id不属于当前桌面轮次。",
                    "逐字使用current_turn.events中真实存在的event_id。",
                    result={
                        "allowed_source_event_ids": [
                            str(item.get("event_id") or "")
                            for item in events
                        ]
                    },
                )
        if selected is None:
            return context, None
        metadata = dict(context.metadata)
        metadata.update(
            {
                "current_message": str(selected.get("text") or ""),
                "source_event_id": str(selected.get("event_id") or ""),
                "source_message_id": str(selected.get("message_id") or ""),
                "source_speaker": str(selected.get("speaker") or context.speaker),
                "source_speaker_id": str(selected.get("speaker_id") or ""),
            }
        )
        directly_addressed = bool(
            selected.get("is_at_gm")
            or selected.get("is_reply_to_gm")
            or selected.get("is_named_gm")
        )
        return (
            replace(
                context,
                speaker=str(selected.get("speaker") or context.speaker),
                directly_addressed=directly_addressed,
                metadata=metadata,
            ),
            None,
        )

    @staticmethod
    def _rollback_transaction(
        transaction: GMToolMutationTransaction | None,
    ) -> str:
        if transaction is None:
            return ""
        try:
            transaction.rollback()
        except Exception as exc:
            return str(exc) or exc.__class__.__name__
        return ""

    @staticmethod
    def _validate_arguments(
        definition: GMToolDefinition,
        arguments: dict[str, object],
    ) -> GMToolReceipt | None:
        parameters = {
            parameter.name: parameter
            for parameter in definition.parameters
            if parameter.source == "model"
        }
        schema = {
            "allowed_arguments": list(parameters),
            "required_arguments": [
                parameter.name
                for parameter in parameters.values()
                if parameter.required
            ],
            "argument_schema": {
                parameter.name: parameter.schema()
                for parameter in parameters.values()
            },
        }
        unknown = sorted(set(arguments) - set(parameters))
        if unknown:
            return GMToolReceipt.failure(
                definition.name,
                "UNKNOWN_ARGUMENT",
                "工具包含未声明参数：" + "、".join(unknown),
                (
                    f"重新调用 {definition.name}，arguments只允许："
                    + ("、".join(parameters) if parameters else "空对象")
                    + "。保留原调用中其余合法字段；不要把删除错误字段理解为提交空对象。"
                    + (
                        "必填字段："
                        + "、".join(schema["required_arguments"])
                        + "。"
                        if schema["required_arguments"]
                        else ""
                    )
                ),
                result=schema,
            )
        missing = [
            parameter.name
            for parameter in parameters.values()
            if parameter.required and parameter.name not in arguments
        ]
        if missing:
            return GMToolReceipt.failure(
                definition.name,
                "MISSING_ARGUMENT",
                "工具缺少必填参数：" + "、".join(missing),
                (
                    f"重新调用 {definition.name} 并补齐："
                    + "、".join(missing)
                    + "。arguments只允许："
                    + ("、".join(parameters) if parameters else "空对象")
                    + "；保留上一轮已有的合法字段和值。"
                ),
                result=schema,
            )
        for key, value in arguments.items():
            parameter = parameters[key]
            if not GMToolRegistry._matches_kind(value, parameter.kind):
                return GMToolReceipt.failure(
                    definition.name,
                    "ARGUMENT_TYPE_MISMATCH",
                    f"参数 {key} 必须是 {parameter.kind}。",
                    f"按回执中的argument_schema重新调用 {definition.name}；保留其他合法字段和值。",
                    result=schema,
                )
            if parameter.enum and str(value) not in parameter.enum:
                return GMToolReceipt.failure(
                    definition.name,
                    "ARGUMENT_ENUM_MISMATCH",
                    f"参数 {key} 不在允许值中。",
                    "允许值：" + "、".join(parameter.enum),
                    result=schema,
                )
            nested_error = GMToolRegistry._validate_schema_value(
                value,
                parameter.schema(),
                path=key,
            )
            if nested_error:
                return GMToolReceipt.failure(
                    definition.name,
                    "ARGUMENT_SCHEMA_MISMATCH",
                    nested_error,
                    "按工具参数 schema 修正嵌套字段后重新提交。",
                )
        return None

    @classmethod
    def _validate_schema_value(
        cls,
        value: object,
        schema: dict[str, object],
        *,
        path: str,
    ) -> str:
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list):
            errors = [
                cls._validate_schema_value(value, item, path=path)
                for item in alternatives
                if isinstance(item, dict)
            ]
            if errors and all(errors):
                return f"参数 {path} 不符合任何允许的结构。"
            return ""

        expected = str(schema.get("type") or "")
        if expected and not cls._matches_kind(value, expected):
            return f"参数 {path} 必须是 {expected}。"
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            allowed = "、".join(str(item) for item in enum)
            return f"参数 {path} 不在允许值中；允许值：{allowed}。"
        if isinstance(value, str):
            minimum = schema.get("minLength")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"参数 {path} 长度不能少于 {minimum}。"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if isinstance(minimum, (int, float)) and value < minimum:
                return f"参数 {path} 不能小于 {minimum}。"
            if isinstance(maximum, (int, float)) and value > maximum:
                return f"参数 {path} 不能大于 {maximum}。"
        if isinstance(value, list):
            minimum = schema.get("minItems")
            maximum = schema.get("maxItems")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"参数 {path} 至少需要 {minimum} 项。"
            if isinstance(maximum, int) and len(value) > maximum:
                return f"参数 {path} 至多允许 {maximum} 项。"
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    error = cls._validate_schema_value(item, item_schema, path=f"{path}[{index}]")
                    if error:
                        return error
        if isinstance(value, dict):
            minimum = schema.get("minProperties")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"参数 {path} 至少需要 {minimum} 个字段。"
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            required = required if isinstance(required, list) else []
            missing = [name for name in required if name not in value]
            if missing:
                return f"参数 {path} 缺少字段：{'、'.join(str(item) for item in missing)}。"
            additional = schema.get("additionalProperties", True)
            unknown = [name for name in value if name not in properties]
            if additional is False and unknown:
                return f"参数 {path} 包含未声明字段：{'、'.join(str(item) for item in unknown)}。"
            for name, item in value.items():
                child_schema = properties.get(name)
                if child_schema is None and isinstance(additional, dict):
                    child_schema = additional
                if isinstance(child_schema, dict):
                    error = cls._validate_schema_value(item, child_schema, path=f"{path}.{name}")
                    if error:
                        return error
        return ""

    @staticmethod
    def _matches_kind(value: object, kind: str) -> bool:
        if kind == "string":
            return isinstance(value, str)
        if kind == "boolean":
            return isinstance(value, bool)
        if kind == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if kind == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if kind == "array":
            return isinstance(value, list)
        if kind == "object":
            return isinstance(value, dict)
        return False
