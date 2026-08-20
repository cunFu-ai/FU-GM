from __future__ import annotations

import json
import time
from types import SimpleNamespace

from fu_gm.components.gm_agent_decision_requester import (
    GMToolAgentDecisionRequester,
)
from fu_gm.components.gm_agent_loop_state import (
    GMAgentLoopPhase,
    GMAgentLoopState,
    GMAgentTerminalReason,
)
from fu_gm.components.gm_agent_message_coordinator import (
    GMAgentMessageCoordinator,
)
from fu_gm.components.gm_live_run_monitor import (
    GMLiveRunMonitor,
    bind_live_run,
    reset_live_run,
)
from fu_gm.config import LLMConfig
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_execution import GMToolCallLedger
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.llm_client import ChatMessage, OpenAICompatibleClient


def _bound_monitor() -> tuple[GMLiveRunMonitor, str, object]:
    monitor = GMLiveRunMonitor()
    run_id = monitor.start_run(
        campaign_id="campaign",
        session_id="session",
        channel_id="channel",
        conversation_turn_id="turn",
        message_id="message",
        speaker="player",
        model="test-model",
        timeout_seconds=30,
        max_iterations=8,
        message="private player input",
    )
    return monitor, run_id, bind_live_run(monitor, run_id)


def _events(monitor: GMLiveRunMonitor) -> list[dict[str, object]]:
    snapshot = monitor.snapshot(include_private=True)
    assert snapshot["active_count"] == 1
    return list(snapshot["active_runs"][0]["events"])


def test_coordinator_wraps_one_run_and_monitor_failure_is_non_authoritative() -> None:
    monitor = GMLiveRunMonitor()
    host = SimpleNamespace(
        gm_tool_agent=SimpleNamespace(
            model="test-model",
            timeout_seconds=12,
            max_iterations=4,
        ),
        gm_live_run_monitor=monitor,
        _message_fields=lambda _payload: (
            "campaign",
            "session",
            "player",
            "hello",
            "channel",
        ),
        _truthy=lambda value: bool(value),
    )
    coordinator = GMAgentMessageCoordinator(host)
    coordinator._handle_bound = lambda *_args, **_kwargs: {
        "ok": True,
        "target": "fu_gm",
        "route": "gm_agent_reply",
        "tool_receipts": [],
    }

    response = coordinator.handle(
        {"message_id": "m1", "conversation_turn_id": "t1"},
        gate=SimpleNamespace(status="adventure"),
        is_private=False,
        explicitly_addressed=True,
        recent_context="",
    )

    assert response is not None and response["route"] == "gm_agent_reply"
    snapshot = monitor.snapshot(include_private=True)
    assert snapshot["active_count"] == 0
    assert len(snapshot["recent_runs"]) == 1
    run = snapshot["recent_runs"][0]
    assert run["terminal_reason"] == "gm_agent_reply"
    assert [item["kind"] for item in run["events"]].count("run_started") == 1

    class _BrokenMonitor:
        def start_run(self, **_kwargs):
            raise RuntimeError("dashboard unavailable")

    host.gm_live_run_monitor = _BrokenMonitor()
    assert coordinator.handle(
        {"message_id": "m2"},
        gate=SimpleNamespace(status="adventure"),
        is_private=False,
        explicitly_addressed=True,
        recent_context="",
    )["route"] == "gm_agent_reply"


def test_loop_requester_and_tool_ledger_emit_complete_live_events() -> None:
    monitor, _run_id, token = _bound_monitor()
    try:
        state = GMAgentLoopState(timeout_seconds=30)
        state.enter(GMAgentLoopPhase.REQUESTING_MODEL, iteration=2)

        raw = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "inspect_state",
                "arguments": {"scope": "scene"},
            },
            ensure_ascii=False,
        )

        class _Client:
            config = SimpleNamespace(response_format_enabled=True)

            def create_chat_completion(self, **_kwargs) -> str:
                return raw

        requester = GMToolAgentDecisionRequester(
            _Client(),
            model="test-model",
        )
        decision = requester.request(
            [ChatMessage(role="user", content="secret input")],
            iteration=2,
            deadline=time.monotonic() + 5,
            trace=[],
        )
        assert decision["tool_name"] == "inspect_state"

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_state",
                description="read",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_state",
                    result={"scope": "scene", "value": 7},
                ),
                side_effect="read",
            )
        )
        ledger = GMToolCallLedger(
            registry=registry,
            context=GMToolExecutionContext(
                campaign_id="campaign",
                session_id="session",
                channel_id="channel",
                speaker="player",
                gate_status="adventure",
            ),
            state_summary={},
        )
        event = ledger.execute("inspect_state", {})
        assert event.receipt is not None and event.receipt.ok

        state.finish(GMAgentTerminalReason.COMPLETED)
        events = _events(monitor)
        kinds = [item["kind"] for item in events]
        assert "agent_loop_phase" in kinds
        assert "model_request_started" in kinds
        assert "model_response_raw" in kinds
        assert "model_decision_parsed" in kinds
        assert "tool_call_started" in kinds
        assert "tool_receipt" in kinds
        raw_event = next(item for item in events if item["kind"] == "model_response_raw")
        assert raw_event["details"]["raw_output"] == raw
        receipt_event = next(item for item in events if item["kind"] == "tool_receipt")
        assert receipt_event["details"]["tool_receipt"]["result"]["value"] == 7
    finally:
        reset_live_run(token)


def test_provider_events_expose_only_assistant_content_and_safe_metadata() -> None:
    class _Transport:
        def post_json(self, url, headers, payload, timeout):
            del url, headers, payload, timeout
            return {
                "choices": [
                    {
                        "message": {
                            "content": "visible assistant output",
                            "reasoning_content": "hidden reasoning",
                        }
                    }
                ]
            }

    config = LLMConfig(
        api_base_url=(
            "https://gateway.test/v1/chat/completions?access_token=query-secret"
        ),
        api_key="api-key-secret",
        action_model="test-model",
        expressor_model="test-model",
        prompt_cache_enabled=False,
        reactive_recovery_enabled=False,
    )
    client = OpenAICompatibleClient(config, transport=_Transport())
    monitor, _run_id, token = _bound_monitor()
    try:
        content = client.create_chat_completion(
            model="test-model",
            messages=[
                ChatMessage(role="system", content="system prompt secret"),
                ChatMessage(role="user", content="user prompt secret"),
            ],
            operation="test.provider",
        )
        assert content == "visible assistant output"
        events = _events(monitor)
        kinds = [item["kind"] for item in events]
        assert "provider_attempt_started" in kinds
        assert "provider_attempt_finished" in kinds
        assert "provider_assistant_content" in kinds
        serialized = json.dumps(events, ensure_ascii=False)
        assert "visible assistant output" in serialized
        assert "system prompt secret" not in serialized
        assert "user prompt secret" not in serialized
        assert "api-key-secret" not in serialized
        assert "query-secret" not in serialized
        assert "hidden reasoning" not in serialized
    finally:
        reset_live_run(token)


def test_live_timeline_preserves_model_tool_receipt_model_loop_order() -> None:
    first_raw = json.dumps(
        {
            "decision": "call_tool",
            "message_kind": "gm_request",
            "audience": "gm",
            "tool_name": "inspect_state",
            "arguments": {},
        },
        ensure_ascii=False,
    )
    second_raw = json.dumps(
        {
            "decision": "final",
            "message_kind": "gm_request",
            "audience": "gm",
            "reply": "钟楼的公开状态已经核对完毕。",
        },
        ensure_ascii=False,
    )

    class _ScriptedClient:
        config = SimpleNamespace(response_format_enabled=True)

        def __init__(self) -> None:
            self.responses = [first_raw, second_raw]

        def create_chat_completion(self, **_kwargs) -> str:
            return self.responses.pop(0)

    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="inspect_state",
            description="读取钟楼公开状态。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "inspect_state",
                result={"state": "quiet"},
            ),
            side_effect="read",
        )
    )
    agent = LLMGMToolAgent(
        _ScriptedClient(),
        model="test-model",
        registry=registry,
        max_iterations=4,
    )
    monitor, _run_id, token = _bound_monitor()
    try:
        outcome = agent.run(
            "时悠，帮我核对钟楼。",
            recent_context="",
            context=GMToolExecutionContext(
                campaign_id="campaign",
                session_id="session",
                channel_id="channel",
                speaker="player",
                gate_status="adventure",
                directly_addressed=True,
            ),
            state_summary={},
        )
        assert outcome.reply == "钟楼的公开状态已经核对完毕。"
        events = _events(monitor)
        raw_indexes = [
            index
            for index, event in enumerate(events)
            if event["kind"] == "model_response_raw"
        ]
        tool_start = next(
            index
            for index, event in enumerate(events)
            if event["kind"] == "tool_call_started"
        )
        receipt = next(
            index
            for index, event in enumerate(events)
            if event["kind"] == "tool_receipt"
        )
        assert len(raw_indexes) == 2
        assert raw_indexes[0] < tool_start < receipt < raw_indexes[1]
        assert events[raw_indexes[0]]["details"]["raw_output"] == first_raw
        assert events[raw_indexes[1]]["details"]["raw_output"] == second_raw
    finally:
        reset_live_run(token)
