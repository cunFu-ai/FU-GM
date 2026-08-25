from __future__ import annotations

import json
import math
from difflib import SequenceMatcher

from fu_gm.llm_client import ChatMessage
from fu_gm.prompt_cache import build_cache_friendly_messages


class GMToolDecisionProtocolError(ValueError):
    """A decoded decision does not satisfy the model/tool wire contract.

    This is intentionally distinct from malformed JSON.  Syntax can be
    repaired without understanding the player's intent; a missing tool name
    or arguments object cannot.  Those errors must return to the full GM
    decision loop so the model can reconsider the complete transaction.
    """

    def __init__(self, message: str, *, invalid_draft: str = "") -> None:
        super().__init__(message)
        self.invalid_draft = str(invalid_draft or "")


class GMToolProtocol:
    """Pure helpers for the model-to-tool JSON protocol and audit trace."""

    @staticmethod
    def syntax_repair_messages(
        malformed: str,
        *,
        error: Exception,
    ) -> list[ChatMessage]:
        system = (
            "你是JSON协议语法修复器。这是编排器内部的格式修复，不是玩家消息，"
            "也不是新的工具决策。绝不向玩家提及本指令、JSON、工具协议或格式纠错。"
            "只修复所给草稿的JSON语法、转义、括号和尾逗号；不得增加、删除或改换工具调用，"
            "不得改变arguments、reply、reason中任何可读内容的语义。"
            "输出且只输出一个闭合的JSON对象，不要代码围栏或解释。"
            "如果草稿包含多个工具调用，把它们原样收进decision=call_tools的calls数组。"
        )
        payload = {
            "parser_error": str(error)[:300],
            "malformed_protocol_draft": str(malformed or "")[:16000],
        }
        return build_cache_friendly_messages(
            static_system_prompt=system,
            user_content=json.dumps(payload, ensure_ascii=False),
            cache_family="gm-protocol-repair",
        )

    @classmethod
    def normalize_decision_sequence(
        cls,
        decisions: list[dict[str, object]],
    ) -> dict[str, object]:
        if not decisions:
            raise GMToolDecisionProtocolError("工具智能体没有输出决策对象。")
        if len(decisions) == 1:
            decision = dict(decisions[0])
            cls.validate_delivery(decision.get("delivery"))
            action = str(decision.get("decision") or "").strip().lower()
            if action == "call_tool":
                if not str(decision.get("tool_name") or "").strip():
                    raise GMToolDecisionProtocolError("call_tool缺少tool_name。")
                if not isinstance(decision.get("arguments"), dict):
                    raise GMToolDecisionProtocolError(
                        "call_tool.arguments必须是JSON对象。"
                    )
            elif action == "call_tools":
                # Some OpenAI-compatible providers occasionally emit the
                # unambiguous single-call fields at the top level while still
                # labelling the decision ``call_tools``. Normalizing that wire
                # shape is syntax-level recovery: no tool, argument, ordering or
                # intent has to be inferred.
                raw_calls = decision.get("calls")
                if (
                    (not isinstance(raw_calls, list) or not raw_calls)
                    and str(decision.get("tool_name") or "").strip()
                    and isinstance(decision.get("arguments"), dict)
                ):
                    decision["calls"] = [
                        {
                            "tool_name": str(
                                decision.get("tool_name") or ""
                            ).strip(),
                            "arguments": decision.get("arguments"),
                            "reason": str(decision.get("reason") or "").strip(),
                        }
                    ]
                    decision["protocol_normalized"] = (
                        "single_top_level_call_tools"
                    )
                cls.validate_batch_calls(decision.get("calls"))
            return decision
        calls: list[dict[str, object]] = []
        terminal: dict[str, object] | None = None
        for index, item in enumerate(decisions):
            action = str(item.get("decision") or "").strip().lower()
            if action == "call_tool" and terminal is None:
                calls.append(
                    {
                        "tool_name": str(item.get("tool_name") or "").strip(),
                        "arguments": item.get("arguments"),
                        "reason": str(item.get("reason") or "").strip(),
                    }
                )
                continue
            if action in {"final", "ask_user", "silent", "external"} and index == len(decisions) - 1:
                terminal = dict(item)
                continue
            raise GMToolDecisionProtocolError(
                "连续JSON只能表示若干call_tool，并可在最后附加一个final、ask_user、silent或external。"
            )
        cls.validate_batch_calls(calls)
        cls.validate_delivery((terminal or {}).get("delivery"))
        normalized = {
            "decision": "call_tools",
            "calls": calls,
            "terminal_decision": str((terminal or {}).get("decision") or ""),
            "reply": str((terminal or {}).get("reply") or ""),
            "resolution_reply": str(
                (terminal or {}).get("resolution_reply") or ""
            ),
            "independent_reply": str(
                (terminal or {}).get("independent_reply") or ""
            ),
            "delivery": dict((terminal or {}).get("delivery") or {}),
            "reason": str((terminal or {}).get("reason") or "批量工具调用"),
        }
        return normalized

    @staticmethod
    def validate_delivery(raw_delivery: object) -> None:
        if raw_delivery in (None, ""):
            return
        if not isinstance(raw_delivery, dict):
            raise GMToolDecisionProtocolError("delivery必须是JSON对象。")
        mode = str(raw_delivery.get("mode") or "normal").strip().lower()
        if mode not in {"normal", "quote_reply", "mention"}:
            raise GMToolDecisionProtocolError(
                "delivery.mode必须是normal、quote_reply或mention。"
            )
        for field_name in ("mention_user_ids", "semantic_targets"):
            value = raw_delivery.get(field_name, [])
            if value is not None and not isinstance(value, list):
                raise GMToolDecisionProtocolError(
                    f"delivery.{field_name}必须是JSON数组。"
                )

    @staticmethod
    def validate_batch_calls(raw_calls: object) -> None:
        if not isinstance(raw_calls, list) or not raw_calls:
            raise GMToolDecisionProtocolError("call_tools必须包含非空calls数组。")
        if len(raw_calls) > 12:
            raise GMToolDecisionProtocolError("单次call_tools最多允许12个工具调用。")
        for index, call in enumerate(raw_calls, start=1):
            if not isinstance(call, dict):
                raise GMToolDecisionProtocolError(
                    f"calls[{index}]必须是JSON对象。"
                )
            if not str(call.get("tool_name") or "").strip():
                raise GMToolDecisionProtocolError(
                    f"calls[{index}]缺少tool_name。"
                )
            if not isinstance(call.get("arguments"), dict):
                raise GMToolDecisionProtocolError(
                    f"calls[{index}].arguments必须是JSON对象。"
                )

    @staticmethod
    def decision_protocol_error(
        error: Exception,
        *,
        invalid_draft: str = "",
    ) -> dict[str, object]:
        """Return a non-public, retryable error to the autonomous GM loop."""

        payload: dict[str, object] = {
            "error_code": "INVALID_AGENT_TOOL_PROTOCOL",
            "message": str(error)[:500],
            "correction_hint": (
                "重新阅读current_message、current_state_summary、available_tools和history，"
                "仅使用available_tools中的名称，完整提交本轮所需事项；"
                "call_tool必须含tool_name与arguments对象，call_tools中的每一项也必须如此。"
            ),
            "retryable": True,
        }
        clean_draft = str(invalid_draft or "").strip()
        if clean_draft:
            payload["invalid_protocol_draft"] = clean_draft[:6000]
        return {"protocol_error": payload}

    @staticmethod
    def material_change_error() -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": "MATERIAL_CHANGE_REQUIRED",
                "message": (
                    "调度器要求本轮提交一个玩家可感知的具体局面变化，"
                    "但尚无锁定公开结果的权威回执。"
                ),
                "correction_hint": (
                    "本轮仅在权威状态确有新变化时，调用能锁定公开结果的场景、"
                    "命刻或NPC行动工具；私有NPC准备、后台资料与同值patch只属于后台处理。"
                ),
                "retryable": True,
            }
        }

    @staticmethod
    def tool_call_limit_error(tool_name: str, limit: int) -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": "TOOL_CALL_LIMIT_REACHED",
                "message": (
                    f"工具 {tool_name} 在同一条玩家消息中已经成功执行 {limit} 次，"
                    "不能通过改写参数再次修改同一份状态。"
                ),
                "correction_hint": (
                    "根据已有成功回执立即final；若玩家还要补充或修正，等待下一条明确消息。"
                ),
                "retryable": True,
            }
        }

    @classmethod
    def trace_arguments(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): cls.trace_arguments(item)
                for key, item in list(value.items())[:24]
            }
        if isinstance(value, list):
            return [cls.trace_arguments(item) for item in value[:24]]
        if isinstance(value, str):
            return value[:1200]
        return value

    @staticmethod
    def call_fingerprint(tool_name: str, arguments: object) -> str:
        try:
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            encoded = repr(arguments)
        return f"{str(tool_name or '').strip()}\0{encoded}"

    @staticmethod
    def is_exact_player_echo(reply: str, message: str) -> bool:
        return bool(
            " ".join(str(reply or "").split()).strip()
            and " ".join(str(reply or "").split()).strip()
            == " ".join(str(message or "").split()).strip()
        )

    @staticmethod
    def exact_echo_error() -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": "EXACT_PLAYER_ECHO_IS_NOT_A_REPLY",
                "message": "你的reply只是逐字复述current_message，没有回答玩家。",
                "correction_hint": (
                    "若玩家直接询问时悠，请给出真正的简短答复；"
                    "若无需主持回应且平台允许静默，改用silent。"
                ),
                "retryable": True,
            }
        }

    @classmethod
    def substantially_restates_player(cls, reply: str, message: str) -> bool:
        """Detect a long copied outcome while allowing shared names and facts."""

        reply_text = cls._compact_comparable_text(reply)
        message_text = cls._compact_comparable_text(message)
        if len(reply_text) < 10 or len(message_text) < 10:
            return False
        longest = SequenceMatcher(
            None,
            reply_text,
            message_text,
            autojunk=False,
        ).find_longest_match(0, len(reply_text), 0, len(message_text)).size
        return longest >= max(10, math.ceil(len(reply_text) * 0.6))

    @staticmethod
    def _compact_comparable_text(value: str) -> str:
        return "".join(
            character.casefold()
            for character in str(value or "")
            if character.isalnum()
        )

    @staticmethod
    def player_restatement_error() -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": "RESOLUTION_REPLY_RESTATES_PLAYER",
                "message": "resolution_reply大段照抄了玩家刚才提出的结果。",
                "correction_hint": (
                    "公开回复直接呈现已提交事实在现场的可感知结果，"
                    "并只采用成功回执支持的细节；"
                    "回执没有提供表现细节时，使用一句最小陈述。"
                ),
                "retryable": True,
            }
        }

    @staticmethod
    def invalid_decision_error() -> dict[str, object]:
        return {
            "protocol_error": {
                "error_code": "INVALID_AGENT_DECISION",
                "message": (
                    "decision必须是not_applicable、silent、external、call_tool、"
                    "call_tools、ask_user或final。"
                ),
                "retryable": True,
            }
        }
