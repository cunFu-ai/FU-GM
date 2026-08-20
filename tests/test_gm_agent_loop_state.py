from __future__ import annotations

import json

import pytest

from fu_gm.components.gm_agent_loop_state import (
    GMAgentLoopPhase,
    GMAgentLoopState,
    GMAgentTerminalReason,
)
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry


class _FinalDecisionClient:
    def create_chat_completion(self, **_kwargs) -> str:
        return json.dumps(
            {
                "decision": "final",
                "message_kind": "gm_request",
                "audience": "gm",
                "reply": "在。",
            },
            ensure_ascii=False,
        )


def test_loop_state_records_phases_and_terminal_reason() -> None:
    state = GMAgentLoopState(timeout_seconds=12)
    state.enter(GMAgentLoopPhase.OBSERVING_STATE, iteration=1)
    state.enter(GMAgentLoopPhase.REQUESTING_MODEL, iteration=1)
    state.finish(GMAgentTerminalReason.COMPLETED)

    payload = state.to_dict()
    assert payload["phase"] == "finished"
    assert payload["terminal_reason"] == "completed"
    assert payload["iteration"] == 1
    assert [event["phase"] for event in payload["events"]] == [
        "created",
        "observing_state",
        "requesting_model",
        "finished",
    ]


def test_loop_state_infers_non_success_terminals() -> None:
    assert (
        GMAgentLoopState.infer_terminal_reason(
            GMToolAgentOutcome(handled=True, terminal_action="silent")
        )
        == GMAgentTerminalReason.SILENT
    )
    assert (
        GMAgentLoopState.infer_terminal_reason(
            GMToolAgentOutcome(
                handled=True,
                error="wall-clock budget timeout",
                mode="gm_agent_unavailable",
            )
        )
        == GMAgentTerminalReason.DEADLINE
    )


def test_finalize_exception_is_recorded_before_propagation() -> None:
    agent = LLMGMToolAgent(
        _FinalDecisionClient(),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="loop-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
    )

    def fail_finalize(_outcome, *, context, transaction, **_kwargs):
        del context, transaction
        raise RuntimeError("commit exploded")

    agent._finalize_message_transaction = fail_finalize

    with pytest.raises(RuntimeError, match="commit exploded"):
        agent.run(
            "时悠，在吗？",
            recent_context="",
            context=context,
            state_summary={},
        )

    assert agent.last_loop_state["terminal_reason"] == "exception"
    assert agent.last_loop_state["events"][-1]["details"]["during"] == (
        "finalizing_transaction"
    )
    assert context.metadata["_gm_agent_loop_diagnostics"]["terminal_reason"] == (
        "exception"
    )
