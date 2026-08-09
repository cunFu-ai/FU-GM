from __future__ import annotations

import json
import tempfile
from unittest.mock import call, patch

from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.http_server import FUGMHttpService


CAMPAIGN_ID = "掷骰工具测试团"


def _context(message: str = "@时悠，掷骰决定第一幕") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=CAMPAIGN_ID,
        session_id="s0",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "gm_dynamic_capabilities_enabled": True,
        },
    )


class _ScriptedClient:
    class _Config:
        timeout_seconds = 5.0

    config = _Config()

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("掷骰智能体测试缺少模型响应。")
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def _prepare_first_act(service: FUGMHttpService) -> object:
    runtime = service._runtime(CAMPAIGN_ID)
    runtime.app.initialize_session_zero(participants=["阿凛", "南星"])
    manager = runtime.app.session_zero_manager
    manager.state.world.group_concept = "守护者"
    manager.generate_first_act_candidates(
        count=6,
        options=[1, 2, 3, 4, 5, 6],
    )
    return runtime


def test_roll_dice_schema_is_typed_and_limited_to_one_success_per_message() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        definition = service.gm_tool_registry._tools["roll_dice"]
        schema = definition.schema()["parameters"]["properties"]

    assert definition.side_effect == "write"
    assert definition.max_successful_calls_per_message == 1
    assert schema["dice_count"]["minimum"] == 1
    assert schema["die_size"]["maximum"] == 1000
    assert schema["choices"]["items"]["required"] == ["id", "label"]
    assert schema["selection_context"]["enum"] == ["none", "first_act"]


def test_plain_roll_uses_rules_engine_and_persists_the_rng_advance() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime(CAMPAIGN_ID)
        with patch.object(
            runtime.app.interceptor.rules_engine,
            "roll_die",
            side_effect=[3, 5],
        ) as roll_die:
            receipt = service.gm_tool_registry.execute(
                "roll_dice",
                {
                    "purpose": "决定今晚谁先守夜",
                    "dice_count": 2,
                    "die_size": 6,
                    "modifier": 1,
                },
                _context("@时悠，掷2d6+1决定谁先守夜"),
            )

        assert receipt.ok
        assert receipt.state_changed
        assert receipt.lock_public_reply
        assert receipt.result["rolls"] == [3, 5]
        assert receipt.result["total"] == 9
        assert receipt.result["notation"] == "2d6+1"
        assert "骰面为3、5，合计9" in receipt.public_fallback_reply
        assert runtime.last_saved_path
        roll_die.assert_has_calls([call(6), call(6)])


def test_custom_choice_mapping_is_fixed_before_the_roll() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime(CAMPAIGN_ID)
        choices = [
            {"id": "north", "label": "北门"},
            {"id": "east", "label": "东门"},
            {"id": "south", "label": "南门"},
        ]
        with patch.object(
            runtime.app.interceptor.rules_engine,
            "roll_die",
            return_value=2,
        ):
            receipt = service.gm_tool_registry.execute(
                "roll_dice",
                {
                    "purpose": "决定从哪座城门离开",
                    "choices": choices,
                },
                _context("@时悠，在三座城门里随机选一座"),
            )

        assert receipt.ok
        assert receipt.result["die_size"] == 3
        assert receipt.result["choice_map"] == choices
        assert receipt.result["selected_choice"] == choices[1]
        assert "1d3=2" in receipt.public_fallback_reply
        assert "1【北门】、2【东门】、3【南门】" in receipt.public_fallback_reply
        assert "【东门】" in receipt.public_fallback_reply


def test_choice_roll_rejects_a_die_that_does_not_match_the_table() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        receipt = service.gm_tool_registry.execute(
            "roll_dice",
            {
                "purpose": "决定道路",
                "die_size": 6,
                "choices": [
                    {"id": "a", "label": "旧路"},
                    {"id": "b", "label": "山路"},
                ],
            },
            _context(),
        )

    assert not receipt.ok
    assert receipt.error_code == "SELECTION_DIE_SIZE_MISMATCH"
    assert not receipt.state_changed


def test_first_act_roll_requires_the_exact_selected_candidate_to_be_committed() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = _prepare_first_act(service)
        context = _context()
        candidates = runtime.app.session_zero_manager.state.world.first_act_candidates
        selected = candidates[3]
        with patch.object(
            runtime.app.interceptor.rules_engine,
            "roll_die",
            return_value=4,
        ):
            receipt = service.gm_tool_registry.execute(
                "roll_dice",
                {
                    "purpose": "从当前六个候选中决定第一幕",
                    "selection_context": "first_act",
                },
                context,
            )

        assert receipt.ok
        assert receipt.result["selected_choice"] == {
            "id": selected.candidate_id,
            "label": selected.title,
        }
        assert receipt.result["required_followup_tools"] == [
            "commit_session_zero_update"
        ]
        required_call = receipt.result["required_followup_calls"][0]
        assert required_call["arguments"] == {
            "updates": {"selected_first_act_id": selected.candidate_id}
        }
        assert selected.questions[0] in receipt.public_fallback_reply

        GMToolReceiptPolicy.apply_context(
            context,
            {},
            receipt,
            tool_arguments={
                "purpose": "从当前六个候选中决定第一幕",
                "selection_context": "first_act",
            },
        )
        assert not GMToolReceiptPolicy.followup_call_matches(
            [receipt],
            tool_name="commit_session_zero_update",
            arguments={
                "updates": {"selected_first_act_id": candidates[1].candidate_id}
            },
        )
        assert GMToolReceiptPolicy.followup_call_matches(
            [receipt],
            tool_name="commit_session_zero_update",
            arguments=dict(required_call["arguments"]),
        )

        commit = service.gm_tool_registry.execute(
            "commit_session_zero_update",
            dict(required_call["arguments"]),
            context,
        )
        assert commit.ok
        GMToolReceiptPolicy.apply_context(
            context,
            {},
            commit,
            tool_arguments=dict(required_call["arguments"]),
        )
        assert (
            runtime.app.session_zero_manager.state.world.selected_first_act_id
            == selected.candidate_id
        )
        assert GMToolReceiptPolicy.required_followup_tools([receipt, commit]) is None

        context.metadata["current_message"] = "我们改主意了，第一幕改成第二个候选"
        changed = service.gm_tool_registry.execute(
            "commit_session_zero_update",
            {
                "updates": {
                    "selected_first_act_id": candidates[1].candidate_id,
                }
            },
            context,
        )
        assert changed.ok
        assert (
            runtime.app.session_zero_manager.state.world.selected_first_act_id
            == candidates[1].candidate_id
        )


def test_agent_cannot_replace_the_first_act_selected_by_the_die() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = _prepare_first_act(service)
        candidates = runtime.app.session_zero_manager.state.world.first_act_candidates
        selected = candidates[3]
        client = _ScriptedClient(
            [
                {
                    "decision": "call_tool",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "tool_name": "discover_capabilities",
                    "arguments": {
                        "domain": "table",
                        "reason": "玩家要求公开掷骰选择第一幕。",
                    },
                },
                {
                    "decision": "call_tool",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "tool_name": "roll_dice",
                    "arguments": {
                        "purpose": "从当前候选中决定第一幕",
                        "selection_context": "first_act",
                    },
                },
                {
                    "decision": "call_tool",
                    "audience": "gm",
                    "tool_name": "commit_session_zero_update",
                    "arguments": {
                        "updates": {
                            "selected_first_act_id": candidates[1].candidate_id,
                        }
                    },
                },
                {
                    "decision": "call_tool",
                    "audience": "gm",
                    "tool_name": "commit_session_zero_update",
                    "arguments": {
                        "updates": {
                            "selected_first_act_id": selected.candidate_id,
                        }
                    },
                },
                {
                    "decision": "final",
                    "audience": "gm",
                    "reply": "模型试图自行改写结果也不能生效。",
                },
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=service.gm_tool_registry,
        )
        context = _context()
        with patch.object(
            runtime.app.interceptor.rules_engine,
            "roll_die",
            return_value=4,
        ):
            outcome = agent.run(
                "@时悠，帮我们掷骰决定第一幕",
                recent_context="桌上已经列出了六个第一幕候选。",
                context=context,
                state_summary=(
                    service.gm_agent_message_coordinator.state_builder.build(
                        context
                    )
                ),
                state_summary_provider=lambda: (
                    service.gm_agent_message_coordinator.state_builder.build(
                        context
                    )
                ),
            )

        assert outcome.handled
        assert f"1d6=4" in outcome.reply
        assert f"【{selected.title}】" in outcome.reply
        assert selected.questions[0] in outcome.reply
        assert "模型试图自行改写" not in outcome.reply
        assert (
            runtime.app.session_zero_manager.state.world.selected_first_act_id
            == selected.candidate_id
        )
        assert any(
            item.get("protocol_error")
            == "REQUIRED_FOLLOWUP_ARGUMENT_MISMATCH"
            for item in outcome.trace
        )
