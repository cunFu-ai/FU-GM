from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol


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
    ) -> "GMToolReceipt":
        return cls(
            tool_name=tool_name,
            ok=True,
            result=dict(result or {}),
            state_changed=bool(state_changed),
            public_fallback_reply=str(public_reply or "").strip(),
            lock_public_reply=bool(lock_public_reply),
            pacing_events=list(pacing_events or []),
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
        return asdict(self)


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

    def schema(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect,
            "parameters": {
                "type": "object",
                "properties": {
                    parameter.name: parameter.schema()
                    for parameter in self.parameters
                    if parameter.source == "model"
                },
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
        self._tools[definition.name] = definition

    def schemas(self, names: set[str] | None = None) -> list[dict[str, object]]:
        return [
            definition.schema()
            for name, definition in self._tools.items()
            if names is None or name in names
        ]

    def successful_call_limit(self, name: str) -> int:
        definition = self._tools.get(str(name or "").strip())
        if definition is None:
            return 0
        return max(0, int(definition.max_successful_calls_per_message))

    def is_read_only(self, name: str) -> bool:
        """Return whether a registered capability cannot mutate game state."""

        definition = self._tools.get(str(name or "").strip())
        return bool(definition is not None and definition.side_effect == "read")

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
        return self._transaction_factory(
            definition,
            effective_arguments,
            context,
        )

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
                        else "不要声称工具已经成功；可以向玩家说明暂时未能完成。"
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
                    transaction.commit()
                except Exception as exc:
                    rollback_error = self._rollback_transaction(transaction)
                    return GMToolReceipt.failure(
                        definition.name,
                        "TOOL_COMMIT_FAILED",
                        f"工具事务无法提交：{exc}"
                        + (f"；回滚也失败：{rollback_error}" if rollback_error else ""),
                        "不要声称工具成功；检查持久化服务后重试。",
                        retryable=False,
                    )
            return receipt

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
