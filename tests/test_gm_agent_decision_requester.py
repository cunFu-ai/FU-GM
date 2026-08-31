from __future__ import annotations

import json

import pytest

from fu_gm.components.gm_agent_decision_requester import (
    GMToolAgentDecisionRequester,
)
from fu_gm.llm_client import ChatMessage, LLMEmptyResponseError
from fu_gm.gm_tool_protocol import GMToolDecisionProtocolError


class ScriptedClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return str(response)


class ConfiguredScriptedClient(ScriptedClient):
    def __init__(self, responses: list[object], *, response_format_enabled: bool) -> None:
        super().__init__(responses)
        self.config = type(
            "Config",
            (),
            {"response_format_enabled": response_format_enabled},
        )()


class TelemetryScriptedClient(ScriptedClient):
    def __init__(self, responses: list[object], *, finish_reasons: list[str]) -> None:
        super().__init__(responses)
        self.finish_reasons = list(finish_reasons)
        self.recent_calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        response = super().create_chat_completion(**kwargs)
        self.recent_calls.append(
            {
                "operation": kwargs.get("operation"),
                "finish_reason": self.finish_reasons.pop(0),
            }
        )
        return response


def test_requester_repairs_syntax_without_receiving_campaign_dependencies() -> None:
    client = ScriptedClient(
        [
            "not-json",
            json.dumps({"decision": "silent", "reason": "玩家彼此讨论。"}),
        ]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="fake",
        parse_retries=1,
    )
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system"), ChatMessage(role="user", content="request")],
        iteration=2,
        deadline=999999999.0,
        trace=trace,
    )

    assert decision["decision"] == "silent"
    assert trace[0]["phase"] == "parse_recovery"
    assert client.calls[1]["operation"] == "gm_tool_agent.iteration_2.parse_retry_1"
    repair_messages = client.calls[1]["messages"]
    assert [message.role for message in repair_messages] == ["system", "user"]
    assert "不是玩家消息" in repair_messages[0].content


def test_requester_raises_after_bounded_syntax_retries() -> None:
    requester = GMToolAgentDecisionRequester(
        ScriptedClient(["bad", "still-bad"]),
        model="fake",
        parse_retries=1,
    )

    with pytest.raises(ValueError):
        requester.request(
            [ChatMessage(role="system", content="system")],
            iteration=1,
            deadline=999999999.0,
            trace=[],
        )


def test_readable_protocol_error_returns_to_full_agent_without_syntax_guessing() -> None:
    raw = json.dumps(
        {
            "decision": "call_tools",
            "calls": [
                {"tool_name": "commit_world", "arguments": {}},
                {"arguments": {"kind": "line"}},
            ],
        },
        ensure_ascii=False,
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    with pytest.raises(GMToolDecisionProtocolError) as raised:
        requester.request(
            [ChatMessage(role="system", content="full decision context")],
            iteration=2,
            deadline=999999999.0,
            trace=trace,
        )

    assert "calls[2]缺少tool_name" in str(raised.value)
    assert raised.value.invalid_draft == raw
    assert len(client.calls) == 1
    assert trace[-1]["phase"] == "decision_protocol_rejection"
    assert trace[-1]["protocol_error"] == "INVALID_AGENT_TOOL_PROTOCOL"
    assert "calls[2]缺少tool_name" in trace[-1]["error"]
    assert trace[-1]["invalid_draft_preview"] == raw[:2000]


def test_unambiguous_top_level_single_call_tools_is_normalized_without_retry() -> None:
    raw = json.dumps(
        {
            "decision": "call_tools",
            "tool_name": "decide_collective_response",
            "arguments": {
                "collective_name": "双方巡逻队",
                "addressed_actor": "伊大石",
            },
            "reason": "回应停火请求",
        },
        ensure_ascii=False,
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(client, model="fake")

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["protocol_normalized"] == "single_top_level_call_tools"
    assert decision["calls"] == [
        {
            "tool_name": "decide_collective_response",
            "arguments": {
                "collective_name": "双方巡逻队",
                "addressed_actor": "伊大石",
            },
            "reason": "回应停火请求",
        }
    ]
    assert len(client.calls) == 1


def test_trailing_container_closure_is_repaired_locally_before_normalization() -> None:
    raw = json.dumps(
        {
            "decision": "call_tools",
            "tool_name": "propose_session_zero_update",
            "arguments": {"summary": "钟声王国"},
            "reason": "保存待定提案",
        },
        ensure_ascii=False,
    )[:-1]
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=trace,
    )

    assert len(client.calls) == 1
    assert decision["protocol_normalized"] == "single_top_level_call_tools"
    assert decision["calls"][0]["tool_name"] == "propose_session_zero_update"
    assert trace == [
        {
            "iteration": 1,
            "phase": "local_json_closure_repair",
            "appended_chars": 1,
        }
    ]


def test_outer_fields_misnested_in_semantics_are_promoted_without_retry() -> None:
    raw = (
        '{"decision":"call_tools","message_semantics":'
        '{"version":"1","events":[],"message_kind":"state_contribution",'
        '"audience":"table","tool_name":"propose_session_zero_update",'
        '"arguments":{"summary":"钟声王国"},"reason":"保存待定提案"}'
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=trace,
    )

    assert len(client.calls) == 1
    assert decision["message_semantics"] == {"version": "1", "events": []}
    assert decision["message_kind"] == "state_contribution"
    assert decision["calls"][0] == {
        "tool_name": "propose_session_zero_update",
        "arguments": {"summary": "钟声王国"},
        "reason": "保存待定提案",
    }
    assert decision["protocol_normalized"] == (
        "misnested_outer_fields+single_top_level_call_tools"
    )
    assert trace[0]["phase"] == "local_json_closure_repair"


def test_redundant_semantics_root_reason_does_not_reject_valid_decision() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": {
                        "version": "1",
                        "events": [],
                        "reason": "这是逐事件语义的整体摘要。",
                    },
                    "message_kind": "state_contribution",
                    "audience": "table",
                    "tool_name": "propose_session_zero_update",
                    "arguments": {"summary": "钟声王国"},
                    "reason": "保存玩家仍在征求意见的提案。",
                },
                ensure_ascii=False,
            )
        ]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=trace,
    )

    assert len(client.calls) == 1
    assert decision["message_semantics"] == {"version": "1", "events": []}
    assert decision["reason"] == "保存玩家仍在征求意见的提案。"
    assert decision["protocol_normalized"] == (
        "redundant_semantics_reason_discarded"
    )


def test_premature_root_close_before_reason_is_reopened_without_retry() -> None:
    raw = (
        '{"decision":"call_tool","tool_name":"commit_scene_response",'
        '"arguments":{"public_reply":"门已经打开。",'
        '"public_facts":["门已经打开。"]}},'
        '"reason":"提交可见变化。"}'
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=trace,
    )

    assert len(client.calls) == 1
    assert decision["decision"] == "call_tool"
    assert decision["arguments"]["public_facts"] == ["门已经打开。"]
    assert decision["reason"] == "提交可见变化。"
    assert trace == [
        {
            "iteration": 1,
            "phase": "local_json_root_reopen",
            "removed_chars": 1,
        }
    ]


def test_empty_call_tools_with_explicit_terminal_is_unwrapped_without_retry() -> None:
    raw = json.dumps(
        {
            "decision": "call_tools",
            "calls": [],
            "terminal_decision": "silent",
            "reply": "",
            "reason": "玩家仍在讨论，主持人保持静默。",
        },
        ensure_ascii=False,
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(client, model="fake")

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["decision"] == "silent"
    assert decision["protocol_normalized"] == "empty_batch_terminal"
    assert "calls" not in decision
    assert len(client.calls) == 1


def test_empty_call_tool_with_explicit_silent_is_unwrapped_without_retry() -> None:
    raw = json.dumps(
        {
            "decision": "call_tool",
            "message_kind": "discussion",
            "audience": "table",
            "tool_name": "",
            "arguments": {},
            "calls": [],
            "terminal_decision": "silent",
            "reply": "",
            "reason": "玩家仍在桌内讨论，主持人保持静默。",
        },
        ensure_ascii=False,
    )
    client = ScriptedClient([raw])
    requester = GMToolAgentDecisionRequester(client, model="fake")
    trace: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=3,
        deadline=999999999.0,
        trace=trace,
    )

    assert decision["decision"] == "silent"
    assert decision["protocol_normalized"] == "empty_single_call_terminal"
    assert "tool_name" not in decision
    assert "arguments" not in decision
    assert "calls" not in decision
    assert trace == [
        {
            "iteration": 3,
            "phase": "structural_protocol_normalization",
            "normalization": "empty_single_call_terminal",
        }
    ]
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "conflicting_fields",
    [
        {"arguments": {"public_reply": "门锁转动。"}},
        {"calls": [{"tool_name": "commit_scene_response", "arguments": {}}]},
        {"reply": "这句不应与silent并存。"},
    ],
)
def test_empty_call_tool_terminal_repair_rejects_conflicting_payload(
    conflicting_fields: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "decision": "call_tool",
        "tool_name": "",
        "arguments": {},
        "calls": [],
        "terminal_decision": "silent",
        "reply": "",
    }
    payload.update(conflicting_fields)
    raw = json.dumps(payload, ensure_ascii=False)
    requester = GMToolAgentDecisionRequester(ScriptedClient([raw]), model="fake")

    with pytest.raises(GMToolDecisionProtocolError):
        requester.request(
            [ChatMessage(role="system", content="system")],
            iteration=1,
            deadline=999999999.0,
            trace=[],
        )


def test_real_call_tool_with_terminal_silent_is_not_unwrapped() -> None:
    raw = json.dumps(
        {
            "decision": "call_tool",
            "tool_name": "commit_scene_response",
            "arguments": {},
            "terminal_decision": "silent",
            "reply": "",
        },
        ensure_ascii=False,
    )
    requester = GMToolAgentDecisionRequester(
        ScriptedClient([raw]),
        model="fake",
    )

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["decision"] == "call_tool"
    assert decision["tool_name"] == "commit_scene_response"
    assert "protocol_normalized" not in decision


def test_syntax_repair_can_use_a_dedicated_nonsemantic_model() -> None:
    client = ScriptedClient(
        [
            '{"decision":"silent" "reason":"缺少逗号"}',
            json.dumps({"decision": "silent", "reason": "无需回应。"}),
        ]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=1,
    )

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["decision"] == "silent"
    assert client.calls[0]["model"] == "semantic-model"
    assert client.calls[1]["model"] == "syntax-model"
    repair_messages = client.calls[1]["messages"]
    assert repair_messages[0].cache_breakpoint is True
    assert repair_messages[0].cache_family == "gm-protocol-repair"


def test_requester_repairs_the_latest_draft_on_each_bounded_retry() -> None:
    client = ScriptedClient(
        [
            '{"decision":"final" "reply":"第一稿"}',
            '{"decision":"final","reply":"第二稿" "reason":"缺少逗号"}',
            json.dumps({"decision": "final", "reply": "第二稿", "reason": "修复完成"}, ensure_ascii=False),
        ]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="fake",
        parse_retries=2,
    )

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["reply"] == "第二稿"
    first_repair = json.loads(client.calls[1]["messages"][1].content)
    second_repair = json.loads(client.calls[2]["messages"][1].content)
    assert first_repair["malformed_protocol_draft"] == '{"decision":"final" "reply":"第一稿"}'
    assert second_repair["malformed_protocol_draft"] == (
        '{"decision":"final","reply":"第二稿" "reason":"缺少逗号"}'
    )


def test_length_limited_json_skips_lossy_syntax_repair() -> None:
    malformed = '{"decision":"call_tool","arguments":{"notes":"' + ("很长" * 300)
    client = TelemetryScriptedClient(
        [malformed],
        finish_reasons=["length"],
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="semantic-model",
        repair_model="syntax-model",
        parse_retries=3,
    )
    trace: list[dict[str, object]] = []

    with pytest.raises(GMToolDecisionProtocolError, match="输出长度上限") as raised:
        requester.request(
            [ChatMessage(role="system", content="full decision context")],
            iteration=2,
            deadline=999999999.0,
            trace=trace,
        )

    assert raised.value.invalid_draft == ""
    assert len(client.calls) == 1
    assert trace[-1]["phase"] == "length_limited_protocol_output"
    assert requester.last_protocol_diagnostics["finish_reason"] == "length"
    assert requester.last_protocol_diagnostics["raw_output"] == malformed


def test_requester_retries_one_empty_provider_cycle_before_parsing() -> None:
    client = ScriptedClient(
        [
            LLMEmptyResponseError("empty"),
            json.dumps({"decision": "silent", "reason": "无需回应。"}),
        ]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="fake",
        parse_retries=0,
        empty_response_retries=1,
    )
    trace: list[dict[str, object]] = []
    runtime_feedback_issues: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=3,
        deadline=999999999.0,
        trace=trace,
        runtime_feedback_issues=runtime_feedback_issues,
    )

    assert decision["decision"] == "silent"
    assert trace == [
        {
            "iteration": 3,
            "phase": "provider_empty_recovery",
            "attempt": 1,
            "error": "empty",
        }
    ]
    assert client.calls[1]["operation"] == "gm_tool_agent.iteration_3.empty_retry_1"
    assert runtime_feedback_issues == [
        {
            "code": "EMPTY_RESPONSE_RECOVERED",
            "phase": "requesting_model",
            "severity": "warning",
            "retryable": False,
            "correction_hint": "模型请求已从空响应恢复；当前步骤可继续处理。",
            "recovery_action": "none",
        }
    ]


def test_requester_whitelists_provider_recovery_diagnostics() -> None:
    class DiagnosticClient(ScriptedClient):
        def consume_call_diagnostics(self) -> dict[str, object]:
            return {
                "recovery_codes": [
                    "PROVIDER_RECOVERED",
                    "CONTEXT_COMPACTED",
                    "UNTRUSTED_CUSTOM_CODE",
                ],
                "attempt_count": 2,
                "endpoint": "https://secret.example?api_key=SECRET",
                "error": "SECRET provider body",
            }

    client = DiagnosticClient(
        [json.dumps({"decision": "silent", "reason": "无需回应。"})]
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="fake",
        parse_retries=0,
    )
    trace: list[dict[str, object]] = []
    runtime_feedback_issues: list[dict[str, object]] = []

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=trace,
        runtime_feedback_issues=runtime_feedback_issues,
    )

    assert decision["decision"] == "silent"
    assert [item["code"] for item in runtime_feedback_issues] == [
        "PROVIDER_RECOVERED",
        "CONTEXT_COMPACTED",
    ]
    serialized = json.dumps(
        {"trace": trace, "issues": runtime_feedback_issues},
        ensure_ascii=False,
    )
    assert "secret.example" not in serialized
    assert "SECRET" not in serialized


def test_requester_can_rely_on_protocol_validation_without_forced_json_mode() -> None:
    client = ConfiguredScriptedClient(
        [json.dumps({"decision": "silent", "reason": "玩家彼此讨论。"})],
        response_format_enabled=False,
    )
    requester = GMToolAgentDecisionRequester(
        client,
        model="reasoning-model",
        parse_retries=0,
    )

    decision = requester.request(
        [ChatMessage(role="system", content="system")],
        iteration=1,
        deadline=999999999.0,
        trace=[],
    )

    assert decision["decision"] == "silent"
    assert client.calls[0]["response_format"] is None


def test_requester_empty_provider_retry_is_bounded() -> None:
    requester = GMToolAgentDecisionRequester(
        ScriptedClient(
            [LLMEmptyResponseError("empty-1"), LLMEmptyResponseError("empty-2")]
        ),
        model="fake",
        parse_retries=0,
        empty_response_retries=1,
    )

    with pytest.raises(LLMEmptyResponseError, match="empty-2"):
        requester.request(
            [ChatMessage(role="system", content="system")],
            iteration=1,
            deadline=999999999.0,
            trace=[],
        )


def test_requester_does_not_duplicate_client_owned_empty_recovery_by_default() -> None:
    client = ScriptedClient([LLMEmptyResponseError("client recovery exhausted")])
    requester = GMToolAgentDecisionRequester(
        client,
        model="fake",
        parse_retries=0,
    )

    with pytest.raises(LLMEmptyResponseError, match="client recovery exhausted"):
        requester.request(
            [ChatMessage(role="system", content="system")],
            iteration=1,
            deadline=999999999.0,
            trace=[],
        )

    assert len(client.calls) == 1
    assert client.calls[0]["max_recovery_retries"] == 1
    assert client.calls[0]["retry_without_response_format_on_empty"] is True
    assert client.calls[0]["thinking_enabled"] is False
