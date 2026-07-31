from __future__ import annotations

import time
from typing import Any

from fu_gm.gm_tool_protocol import (
    GMToolDecisionProtocolError,
    GMToolProtocol,
)
from fu_gm.llm_client import ChatMessage, LLMEmptyResponseError
from fu_gm.llm_utils import extract_json_object_sequence


class GMToolAgentDecisionRequester:
    """Own provider I/O and syntax-only recovery for one agent decision.

    This boundary deliberately knows nothing about campaign state or tool side
    effects. It may repair malformed JSON syntax, but it cannot reinterpret the
    player's message or alter a proposed tool call.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        repair_model: str = "",
        protocol: type[GMToolProtocol] = GMToolProtocol,
        parse_retries: int = 1,
        empty_response_retries: int = 1,
        max_output_tokens: int = 4096,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.repair_model = str(repair_model or model or "").strip()
        self.protocol = protocol
        self.parse_retries = max(0, int(parse_retries))
        self.empty_response_retries = max(0, int(empty_response_retries))
        self.max_output_tokens = max(512, int(max_output_tokens))

    def request(
        self,
        messages: list[ChatMessage],
        *,
        iteration: int,
        deadline: float,
        trace: list[dict[str, object]],
    ) -> dict[str, object]:
        active_messages = list(messages)
        malformed_protocol_draft = ""
        parse_error: Exception | None = None
        empty_attempts = 0
        for parse_attempt in range(self.parse_retries + 1):
            while True:
                operation = (
                    f"gm_tool_agent.iteration_{iteration}"
                    if parse_attempt == 0
                    else (
                        f"gm_tool_agent.iteration_{iteration}"
                        f".parse_retry_{parse_attempt}"
                    )
                )
                if empty_attempts:
                    operation = f"{operation}.empty_retry_{empty_attempts}"
                try:
                    raw = self.client.create_chat_completion(
                        model=(
                            self.model
                            if parse_attempt == 0
                            else self.repair_model
                        ),
                        messages=active_messages,
                        temperature=0.0,
                        response_format={"type": "json_object"},
                        max_tokens=self.max_output_tokens,
                        deadline=deadline,
                        operation=operation,
                    )
                    break
                except LLMEmptyResponseError as exc:
                    if (
                        empty_attempts >= self.empty_response_retries
                        or time.monotonic() >= deadline
                    ):
                        raise
                    empty_attempts += 1
                    trace.append(
                        {
                            "iteration": iteration,
                            "phase": "provider_empty_recovery",
                            "attempt": empty_attempts,
                            "error": str(exc)[:300],
                        }
                    )
            try:
                decisions = extract_json_object_sequence(raw)
            except (TypeError, ValueError) as exc:
                parse_error = exc
                # Continue from the latest syntax-only repair instead of
                # asking every retry to reproduce the same broken draft.
                # No repaired value can execute before protocol, schema and
                # semantic validation all succeed.
                malformed_protocol_draft = str(raw)
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "parse_recovery",
                        "attempt": parse_attempt + 1,
                        "error": str(exc)[:300],
                    }
                )
                if parse_attempt >= self.parse_retries:
                    raise GMToolDecisionProtocolError(
                        "工具智能体输出的JSON语法仍不完整；请根据原始消息、当前状态和工具回执重新生成完整决策。",
                        invalid_draft=malformed_protocol_draft,
                    ) from exc
                active_messages = self.protocol.syntax_repair_messages(
                    malformed_protocol_draft,
                    error=exc,
                )
                continue
            try:
                return self.protocol.normalize_decision_sequence(decisions)
            except GMToolDecisionProtocolError as exc:
                # The JSON is readable, but repairing an omitted tool name or
                # arguments object would require guessing intent.  Return the
                # exact contract error to the full GM loop instead, where the
                # original message, state, tools and receipts are available.
                rejected = GMToolDecisionProtocolError(
                    str(exc),
                    invalid_draft=str(raw),
                )
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "decision_protocol_rejection",
                        "error": str(exc)[:300],
                    }
                )
                raise rejected from exc
        raise parse_error or ValueError("未找到合法 JSON 对象。")
