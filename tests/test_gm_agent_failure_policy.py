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


def test_provider_timeout_is_named_explicitly() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[],
        trace=[],
        error="LLM operation exceeded its wall-clock budget",
        must_decide=True,
        must_reply=True,
    )

    assert outcome.mode == "gm_agent_unavailable"
    assert "模型调用超时" in outcome.reply
    assert "没有记入或结算" in outcome.reply


def test_rule_rejection_followed_by_timeout_reports_both_causes() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[
            GMToolReceipt.failure(
                "resolve_rule_window",
                "RULE_ACTION_REJECTED",
                "机会参数不合法。",
                "修正参数后重试。",
            )
        ],
        trace=[],
        error="provider handshake timed out",
        must_decide=True,
        must_reply=True,
    )

    assert "机会参数不合法" in outcome.reply
    assert "模型调用又超时" in outcome.reply
    assert "不是你的行动失败" in outcome.reply


def test_tool_retry_exhausted_reports_last_concrete_rule_reason() -> None:
    outcome = GMToolAgentFailurePolicy.tool_retry_exhausted(
        receipts=[
            GMToolReceipt.failure(
                "resolve_rule_window",
                "GM_OPPORTUNITY_ITEM_REQUIRED",
                "机会【失物】需要明确角色物品，或当前场景中受影响的物件。",
                "角色物品填写target与item_name；现场物件填写scene_object与description。",
            )
        ],
        trace=[],
        must_reply=True,
        error="工具连续三次未通过校验。",
    )

    assert "机会【失物】需要明确" in outcome.reply
    assert "待决选择仍然保留" in outcome.reply
    assert "规则工具连续校验失败" not in outcome.reply


def test_tool_retry_exhausted_uses_locked_player_clarification() -> None:
    outcome = GMToolAgentFailurePolicy.tool_retry_exhausted(
        receipts=[
            GMToolReceipt(
                tool_name="resolve_rule_window",
                ok=False,
                error_code="OPPORTUNITY_TARGET_REQUIRED",
                message="机会【揭示】还没有选择生物目标。",
                retryable=True,
                public_fallback_reply="你想对哪一个生物使用【揭示】？",
                lock_public_reply=True,
            )
        ],
        trace=[],
        must_reply=True,
        error="工具连续三次未通过校验。",
    )

    assert outcome.reply == "你想对哪一个生物使用【揭示】？"


def test_timeout_after_mixed_rule_commit_reports_partial_success() -> None:
    outcome = GMToolAgentFailurePolicy.provider_failure(
        receipts=[
            GMToolReceipt(
                tool_name="resolve_rule_window",
                ok=True,
                state_changed=True,
                result={"mixed_message_followup_pending": True},
                public_fallback_reply="机会【失物】：牢门已经可以推开。",
            )
        ],
        trace=[],
        error="LLM operation exceeded its wall-clock budget",
        must_decide=True,
        must_reply=True,
    )

    assert outcome.mode == "gm_agent_partial"
    assert "牢门已经可以推开" in outcome.reply
    assert "规则选择已经结算" in outcome.reply
    assert "模型调用超时" in outcome.reply
    assert "没有记入或结算" not in outcome.reply


def test_exhausted_mixed_followup_names_protocol_failure() -> None:
    receipt = GMToolReceipt(
        tool_name="resolve_rule_window",
        ok=True,
        state_changed=True,
        result={"mixed_message_followup_pending": True},
        public_fallback_reply="机会已经结算。",
    )

    outcome = GMToolAgentFailurePolicy.exhausted(
        receipts=[receipt],
        trace=[],
        must_decide=True,
        must_reply=True,
    )

    assert outcome.mode == "gm_agent_partial"
    assert "模型没有按收尾协议完成回答" in outcome.reply
