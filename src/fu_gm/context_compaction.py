from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StructuredCompactionResult:
    text: str
    strategy: str
    omitted_chars: int


class StructuredContextCompactor:
    """Compact one request view without changing authoritative history.

    FU-GM rebuilds structured state for every model request. When a provider
    rejects an oversized request, preserving JSON shape and the latest table
    transaction is safer than cutting arbitrary character ranges.
    """

    _ROOT_PRIORITY = (
        "current_message",
        "current_turn",
        "session",
        "request_context",
        "runtime_feedback",
        "current_state_summary",
        "available_tools",
        "history",
    )
    _NESTED_PRIORITY = (
        "id",
        "name",
        "type",
        "status",
        "owner",
        "speaker",
        "actor",
        "target",
        "text",
        "message",
        "description",
        "parameters",
        "required",
        "result",
        "error_code",
        "correction_hint",
    )
    _PROFILES = (
        ("structured-balanced", 900, 18, 56, 7),
        ("structured-tight", 480, 10, 36, 6),
        ("structured-minimal", 220, 5, 24, 5),
    )

    def compact(self, text: str, *, max_chars: int) -> StructuredCompactionResult:
        raw = str(text)
        if len(raw) <= max_chars:
            return StructuredCompactionResult(raw, "unchanged", 0)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return StructuredCompactionResult(raw, "not-json", 0)

        protected = self._protected_kernel(value)
        smallest_text = raw
        smallest_strategy = "json-too-large"

        for strategy, string_limit, list_limit, dict_limit, max_depth in self._PROFILES:
            projected = self._project(
                value,
                path=(),
                depth=0,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                max_depth=max_depth,
                max_chars=max_chars,
            )
            if isinstance(projected, dict):
                projected = {
                    "_fu_gm_context_compaction": {
                        "applied": True,
                        "strategy": strategy,
                        "instruction": (
                            "上下文折叠仅影响本次模型请求视图；"
                            "优先相信当前消息、当前回合、工具和最新回执。"
                        ),
                    },
                    **projected,
                }
                self._deep_overlay(projected, protected)
            rendered = json.dumps(
                projected,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(rendered) < len(smallest_text):
                smallest_text = rendered
                smallest_strategy = strategy
            if len(rendered) <= max_chars:
                return StructuredCompactionResult(
                    rendered,
                    strategy,
                    max(0, len(raw) - len(rendered)),
                )

        emergency = self._emergency_projection(value, max_chars=max_chars)
        self._deep_overlay(emergency, protected)
        rendered = json.dumps(emergency, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) < len(smallest_text):
            smallest_text = rendered
            smallest_strategy = "structured-emergency"
        if len(rendered) <= max_chars:
            return StructuredCompactionResult(
                rendered,
                "structured-emergency",
                max(0, len(raw) - len(rendered)),
            )
        minimum = self._absolute_minimum_projection(value, max_chars=max_chars)
        self._deep_overlay(minimum, protected)
        rendered = json.dumps(minimum, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) < len(smallest_text):
            smallest_text = rendered
            smallest_strategy = "structured-absolute-minimum"
        if len(rendered) <= max_chars:
            return StructuredCompactionResult(
                rendered,
                "structured-absolute-minimum",
                max(0, len(raw) - len(rendered)),
            )
        if len(smallest_text) < len(raw):
            return StructuredCompactionResult(
                smallest_text,
                f"{smallest_strategy}-protected-over-budget",
                max(0, len(raw) - len(smallest_text)),
            )
        return StructuredCompactionResult(raw, "json-too-large", 0)

    @classmethod
    def _protected_kernel(cls, value: Any) -> dict[str, Any]:
        """提取不能因供应商恢复重试而改变语义的请求核心。"""

        if not isinstance(value, dict):
            return {}
        kernel = {
            key: value[key]
            for key in (
                "current_message",
                "current_turn",
                "session",
                "request_context",
                "runtime_feedback",
            )
            if key in value
        }
        tools = value.get("available_tools")
        if isinstance(tools, list):
            kernel["available_tools"] = [
                cls._protected_tool_contract(item)
                for item in tools
                if isinstance(item, dict)
            ]
        recent = value.get("recent_messages")
        if isinstance(recent, list) and recent:
            kernel["recent_messages"] = recent[-4:]
        history = value.get("history")
        if isinstance(history, list) and history:
            protected_history = [
                item
                for index, item in enumerate(history)
                if index >= len(history) - 2 or cls._history_entry_is_blocking(item)
            ]
            if protected_history:
                kernel["history"] = protected_history
        state = value.get("current_state_summary")
        if isinstance(state, dict):
            protected_state = {
                key: state[key]
                for key in (
                    "current_campaign_id",
                    "message_campaign_id",
                    "gate_status",
                    "turn_participants",
                    "speaker_controlled_characters",
                    "observation",
                    "runtime",
                    "processes",
                    "clocks",
                )
                if key in state
            }
            for section, keys in (
                (
                    "scene",
                    {
                        "active",
                        "scene_id",
                        "name",
                        "location",
                        "participants",
                        "participant_locations",
                        "participant_positions",
                        "objective",
                        "current_pressure",
                        "public_facts",
                        "revealed_clues",
                        "private_situation",
                        "working_brief",
                    },
                ),
                (
                    "gameplay",
                    {
                        "speaker",
                        "controlled_characters",
                        "characters",
                        "pending_decisions",
                        "character_locations",
                        "character_positions",
                        "conflict",
                    },
                ),
                (
                    "npcs",
                    {"scene_id", "location", "present_npcs", "dialogue_authority"},
                ),
            ):
                source = state.get(section)
                if isinstance(source, dict):
                    protected_state[section] = {
                        key: source[key]
                        for key in keys
                        if key in source
                    }
            kernel["current_state_summary"] = protected_state
        return kernel

    @classmethod
    def _protected_tool_contract(cls, value: dict[str, Any]) -> dict[str, Any]:
        """保留工具名称和参数约束，只缩短不参与校验的说明文字。"""

        result = {
            key: item
            for key, item in value.items()
            if key != "description"
        }
        if "description" in value:
            result["description"] = cls._compact_string(
                str(value.get("description") or ""),
                limit=280,
            )
        parameters = value.get("parameters")
        if isinstance(parameters, (dict, list)):
            result["parameters"] = cls._compact_schema_descriptions(parameters)
        return result

    @classmethod
    def _compact_schema_descriptions(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    cls._compact_string(str(item or ""), limit=180)
                    if key == "description"
                    else cls._compact_schema_descriptions(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._compact_schema_descriptions(item) for item in value]
        return value

    @staticmethod
    def _history_entry_is_blocking(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        receipt = value.get("tool_receipt")
        if not isinstance(receipt, dict):
            return False
        if receipt.get("ok") is False:
            return True
        result = receipt.get("result")
        return isinstance(result, dict) and any(
            result.get(key) not in (None, "", [], {}, False)
            for key in (
                "required_followup_tools",
                "required_followup_calls",
                "pending_decision",
                "pending_decisions",
                "natural_resolution_pending",
            )
        )

    @classmethod
    def _deep_overlay(cls, target: dict[str, Any], protected: dict[str, Any]) -> None:
        for key, value in protected.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._deep_overlay(target[key], value)
            else:
                target[key] = value

    def _project(
        self,
        value: Any,
        *,
        path: tuple[str, ...],
        depth: int,
        string_limit: int,
        list_limit: int,
        dict_limit: int,
        max_depth: int,
        max_chars: int,
    ) -> Any:
        if isinstance(value, str):
            limit = self._string_limit(
                path,
                default=string_limit,
                max_chars=max_chars,
            )
            return self._compact_string(value, limit=limit)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= max_depth:
            return self._compact_string(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                limit=string_limit,
            )
        if isinstance(value, list):
            limit = self._list_limit(path, default=list_limit)
            selected = value[-limit:] if self._keep_latest(path) else value[:limit]
            return [
                self._project(
                    item,
                    path=(*path, "[]"),
                    depth=depth + 1,
                    string_limit=string_limit,
                    list_limit=list_limit,
                    dict_limit=dict_limit,
                    max_depth=max_depth,
                    max_chars=max_chars,
                )
                for item in selected
            ]
        if isinstance(value, dict):
            keys = self._ordered_keys(value, path=path)
            selected_keys = keys if not path else keys[:dict_limit]
            projected = {
                str(key): self._project(
                    value[key],
                    path=(*path, str(key)),
                    depth=depth + 1,
                    string_limit=string_limit,
                    list_limit=list_limit,
                    dict_limit=dict_limit,
                    max_depth=max_depth,
                    max_chars=max_chars,
                )
                for key in selected_keys
            }
            omitted = len(keys) - len(selected_keys)
            if omitted > 0:
                projected["_fu_gm_omitted_keys"] = omitted
            return projected
        return self._compact_string(str(value), limit=string_limit)

    def _emergency_projection(self, value: Any, *, max_chars: int) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {"value": value}
        result: dict[str, Any] = {
            "_fu_gm_context_compaction": {
                "applied": True,
                "strategy": "structured-emergency",
                "instruction": "上下文折叠后只保留当前事务所需的最小请求视图。",
            }
        }
        for key in (
            "current_message",
            "current_turn",
            "session",
            "request_context",
            "runtime_feedback",
        ):
            if key in source:
                result[key] = self._project(
                    source[key],
                    path=(key,),
                    depth=1,
                    string_limit=180,
                    list_limit=4,
                    dict_limit=16,
                    max_depth=4,
                    max_chars=max_chars,
                )
        if isinstance(source.get("available_tools"), list):
            result["available_tools"] = [
                self._tool_signature(item)
                for item in source["available_tools"][:24]
                if isinstance(item, dict)
            ]
        if isinstance(source.get("history"), list):
            result["history"] = self._project(
                source["history"][-3:],
                path=("history",),
                depth=1,
                string_limit=160,
                list_limit=3,
                dict_limit=16,
                max_depth=4,
                max_chars=max_chars,
            )
        if "current_state_summary" in source:
            state_budget = max(500, max_chars // 5)
            result["current_state_summary"] = self._compact_string(
                json.dumps(
                    source["current_state_summary"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                limit=state_budget,
            )
        return result

    def _absolute_minimum_projection(self, value: Any, *, max_chars: int) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {"value": value}
        result: dict[str, Any] = {
            "_fu_gm_context_compaction": {
                "applied": True,
                "strategy": "structured-absolute-minimum",
                "instruction": "只保留当前事务、合法工具签名和最新回执。",
            }
        }
        if "current_message" in source:
            result["current_message"] = self._compact_string(
                str(source["current_message"]),
                limit=max(500, min(2400, max_chars // 3)),
            )
        for key in (
            "current_turn",
            "session",
            "request_context",
            "runtime_feedback",
        ):
            if key in source:
                result[key] = self._project(
                    source[key],
                    path=(key,),
                    depth=1,
                    string_limit=120,
                    list_limit=3,
                    dict_limit=12,
                    max_depth=3,
                    max_chars=max_chars,
                )
        tools = source.get("available_tools")
        if isinstance(tools, list):
            result["available_tools"] = [
                self._minimal_tool_signature(tool)
                for tool in tools[:24]
                if isinstance(tool, dict)
            ]
        history = source.get("history")
        if isinstance(history, list):
            result["history"] = self._project(
                history[-2:],
                path=("history",),
                depth=1,
                string_limit=100,
                list_limit=2,
                dict_limit=10,
                max_depth=3,
                max_chars=max_chars,
            )
        if "current_state_summary" in source:
            result["current_state_summary"] = self._compact_string(
                json.dumps(
                    source["current_state_summary"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                limit=max(300, max_chars // 10),
            )
        return result

    @staticmethod
    def _tool_signature(tool: dict[str, Any]) -> dict[str, Any]:
        signature: dict[str, Any] = {
            "name": str(tool.get("name") or ""),
            "description": StructuredContextCompactor._compact_string(
                str(tool.get("description") or ""),
                limit=160,
            ),
        }
        if "side_effect" in tool:
            signature["side_effect"] = tool.get("side_effect")
        parameters = tool.get("parameters")
        if isinstance(parameters, dict):
            signature["parameters"] = {
                str(name): {
                    key: (
                        StructuredContextCompactor._compact_string(str(value), limit=120)
                        if key == "description"
                        else value
                    )
                    for key, value in parameter.items()
                    if key in {"type", "required", "enum", "description"}
                }
                if isinstance(parameter, dict)
                else parameter
                for name, parameter in parameters.items()
            }
        elif isinstance(parameters, list):
            signature["parameters"] = [
                {
                    key: (
                        StructuredContextCompactor._compact_string(
                            str(parameter.get(key) or ""),
                            limit=120,
                        )
                        if key == "description"
                        else parameter.get(key)
                    )
                    for key in ("name", "type", "required", "enum", "description")
                    if key in parameter
                }
                for parameter in parameters
                if isinstance(parameter, dict)
            ]
        return signature

    @staticmethod
    def _minimal_tool_signature(tool: dict[str, Any]) -> dict[str, Any]:
        signature: dict[str, Any] = {"name": str(tool.get("name") or "")}
        parameters = tool.get("parameters")
        if isinstance(parameters, dict):
            signature["parameters"] = {
                str(name): {
                    key: parameter.get(key)
                    for key in ("type", "required")
                    if isinstance(parameter, dict) and key in parameter
                }
                for name, parameter in parameters.items()
            }
        elif isinstance(parameters, list):
            signature["parameters"] = [
                {
                    key: parameter.get(key)
                    for key in ("name", "type", "required")
                    if key in parameter
                }
                for parameter in parameters
                if isinstance(parameter, dict)
            ]
        return signature

    @classmethod
    def _ordered_keys(cls, value: dict[Any, Any], *, path: tuple[str, ...]) -> list[Any]:
        priorities = cls._ROOT_PRIORITY if not path else cls._NESTED_PRIORITY
        rank = {name: index for index, name in enumerate(priorities)}
        return sorted(
            value.keys(),
            key=lambda key: (rank.get(str(key), len(rank)), str(key)),
        )

    @staticmethod
    def _keep_latest(path: tuple[str, ...]) -> bool:
        return bool(path and path[-1] in {"recent_messages", "history", "events", "receipts"})

    @staticmethod
    def _list_limit(path: tuple[str, ...], *, default: int) -> int:
        if path and path[-1] == "available_tools":
            return max(default, 24)
        if path and path[-1] in {"current_turn", "events"}:
            return max(default, 12)
        return default

    @staticmethod
    def _string_limit(path: tuple[str, ...], *, default: int, max_chars: int) -> int:
        if path == ("current_message",):
            return max(800, min(4000, max_chars // 3))
        if "current_turn" in path and path[-1:] == ("text",):
            return max(default, 900)
        if path and path[-1] in {"error_code", "name", "id", "type", "status"}:
            return max(default, 240)
        return default

    @staticmethod
    def _compact_string(text: str, *, limit: int) -> str:
        if len(text) <= limit:
            return text
        if limit < 80:
            return text[:limit]
        head = max(40, int(limit * 0.65))
        tail = max(20, limit - head - 22)
        return f"{text[:head]}...[省略{len(text) - head - tail}字]...{text[-tail:]}"
