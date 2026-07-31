from __future__ import annotations

from fu_gm.components.gm_agent_failure_policy import GMToolAgentFailurePolicy
from fu_gm.gm_tool_contracts import GMToolReceipt


def test_active_group_failure_is_owned_but_silent() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[],
        trace=[],
        error="provider down",
        must_decide=True,
        must_reply=False,
    )

    assert outcome.handled
    assert outcome.target == "silent"
    assert outcome.reply == ""


def test_open_provider_circuit_asks_player_to_wait_instead_of_retrying_immediately() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[],
        trace=[],
        error="LLM provider circuit is open for model 'model'; retry after 30.0s",
        must_decide=True,
        must_reply=True,
    )

    assert outcome.mode == "gm_agent_unavailable"
    assert "请稍后再试" in outcome.reply
    assert "麻烦再说一次" not in outcome.reply


def test_committed_state_wins_over_provider_failure() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[
            GMToolReceipt(
                tool_name="save_campaign",
                ok=True,
                state_changed=True,
                public_fallback_reply="存好了。",
            )
        ],
        trace=[],
        error="provider down",
        must_decide=True,
        must_reply=True,
    )

    assert outcome.target == "fu_gm"
    assert outcome.reply == "存好了。"
