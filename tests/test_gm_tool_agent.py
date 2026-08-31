import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from fu_gm.gm_tool_agent import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
    LLMGMToolAgent,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.components.gm_reply_grounding_verifier import GMReplyGroundingVerifier
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_message_integrity import GMMessageIntegrityValidator
from fu_gm.components.gm_message_semantics import GMMessageSemantics
from fu_gm.models import HeroDraft
from fu_gm.models import Character, SceneType


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        return self.responses.pop(0)


class DiagnosticScriptedClient(ScriptedClient):
    def __init__(
        self,
        responses: list[str],
        diagnostics_by_call: list[dict[str, object]],
    ) -> None:
        super().__init__(responses)
        self.diagnostics_by_call = list(diagnostics_by_call)
        self.pending_diagnostics: dict[str, object] = {}

    def create_chat_completion(self, **kwargs) -> str:
        call_index = len(self.calls)
        response = super().create_chat_completion(**kwargs)
        self.pending_diagnostics = (
            dict(self.diagnostics_by_call[call_index])
            if call_index < len(self.diagnostics_by_call)
            else {}
        )
        return response

    def consume_call_diagnostics(self) -> dict[str, object]:
        payload = dict(self.pending_diagnostics)
        self.pending_diagnostics = {}
        return payload


class TransportFailureClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        raise TimeoutError("The read operation timed out")


def test_state_contribution_needing_a_tool_is_not_an_explicit_gm_address() -> None:
    assert (
        LLMGMToolAgent._decision_semantically_addresses_gm(
            {
                "message_kind": "state_contribution",
                "audience": "gm",
                "decision": "call_tool",
            }
        )
        is False
    )


def test_semantic_gm_request_still_creates_a_reply_duty() -> None:
    assert (
        LLMGMToolAgent._decision_semantically_addresses_gm(
            {
                "message_kind": "gm_request",
                "audience": "gm",
                "decision": "call_tool",
            }
        )
        is True
    )


def test_tool_semantics_mismatch_is_repaired_before_initial_freeze() -> None:
    event_id = "event-world-detail"
    message = "钟鸣公国的底层以科技替代被禁止的魔法。"
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "message_semantics_contract_required": True,
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    call = {
        "decision": "call_tool",
        "tool_name": "create_world_setting",
        "arguments": {
            "category": "custom_world_settings",
            "name": "钟鸣公国魔法与科技地位",
            "value": "钟鸣公国的底层以科技替代被禁止的魔法。",
            "source_event_id": event_id,
        },
        "message_semantics": {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": [],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "none",
                    "state_scope": "none",
                    "state_intents": [],
                    "responds_to_event_id": "",
                    "reason": "玩家贡献地方设定。",
                }
            ],
        },
    }
    history: list[dict[str, object]] = []
    step: dict[str, object] = {}

    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state={},
        history=history,
        step=step,
        is_system_beat=False,
    )

    assert retry is True
    assert step["protocol_error"] == "MESSAGE_STATE_INTENT_REQUIRED"
    assert "_gm_message_semantics" not in context.metadata
    assert "_gm_provisional_message_semantics" in context.metadata

    call["message_semantics"]["events"][0]["state_intents"] = [
        {
            "operation": "contribute",
            "scope": "world",
            "subject": "custom_world_settings",
            "target": "钟鸣公国",
            "summary": "钟鸣公国底层以科技替代禁魔",
        }
    ]
    call["message_semantics"]["events"][0]["state_scope"] = "world"
    repaired_step: dict[str, object] = {}
    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state={},
        history=history,
        step=repaired_step,
        is_system_beat=False,
    )

    assert retry is False
    assert repaired_step["message_semantics_source"] == "initial_decision"
    frozen = context.metadata["_gm_message_semantics"]
    assert frozen["events"][0]["state_intents"][0]["subject"] == (
        "custom_world_settings"
    )


def test_pending_rule_window_answer_repairs_committed_semantics_before_freeze() -> None:
    event_id = "event-opportunity"
    window_id = "window-opportunity"
    message = "我把这次机会用于【优势】，目标是【伊莉雅】。"
    context = GMToolExecutionContext(
        campaign_id="window-test",
        session_id="s1",
        channel_id="group",
        speaker="阿凛",
        gate_status="adventure",
        metadata={
            "message_semantics_contract_required": True,
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    call = {
        "decision": "call_tool",
        "tool_name": "resolve_rule_window",
        "arguments": {
            "window_id": window_id,
            "choice": "优势",
            "source_event_id": event_id,
        },
        "message_semantics": {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "gm",
                    "targets": ["时悠"],
                    "dialogue_act": "answer",
                    "action_commitment": "committed",
                    "response_expectation": "gm",
                    "responds_to_event_id": "",
                    "reason": "玩家选择大成功机会的效果与目标。",
                }
            ],
        },
    }
    observed_state = {
        "turn_participants": {
            "controlled_characters_by_speaker": {"阿凛": ["伊莉雅"]}
        },
        "processes": {
            "decisions": {
                "pending": [
                    {
                        "window_id": window_id,
                        "kind": "critical_opportunity",
                        "owner": "伊莉雅",
                        "allowed_speakers": ["阿凛"],
                    }
                ]
            }
        },
    }
    history: list[dict[str, object]] = []
    step: dict[str, object] = {}

    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state=observed_state,
        history=history,
        step=step,
        is_system_beat=False,
    )

    assert retry is True
    assert step["protocol_error"] == (
        "RULE_WINDOW_ANSWER_COMMITMENT_REPAIR_REQUIRED"
    )
    assert "_gm_message_semantics" not in context.metadata
    assert "_gm_provisional_message_semantics" not in context.metadata

    call["message_semantics"]["events"][0]["action_commitment"] = "answer"
    repaired_step: dict[str, object] = {}
    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state=observed_state,
        history=history,
        step=repaired_step,
        is_system_beat=False,
    )

    assert retry is False
    assert context.metadata["_gm_message_semantics"]["events"][0][
        "action_commitment"
    ] == "answer"


def test_tentative_hero_field_can_be_repaired_before_initial_freeze() -> None:
    event_id = "event-hero-theme"
    message = "洛岚的主题我想设定为赎罪。"
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="白河",
        gate_status="session_zero",
        metadata={
            "message_semantics_contract_required": True,
            "current_turn_events": [
                {"event_id": event_id, "speaker": "白河", "text": message}
            ],
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    call = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "hero_name": "洛岚",
            "patch": {"theme": "赎罪"},
            "source_event_id": event_id,
        },
        "message_semantics": {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "白河",
                    "relation": "table",
                    "targets": [],
                    "dialogue_act": "state_contribution",
                    "action_commitment": "none",
                    "state_scope": "hero",
                    "state_intents": [
                        {
                            "operation": "propose",
                            "scope": "hero",
                            "subject": "hero_theme",
                            "target": "洛岚",
                            "summary": "洛岚的主题是赎罪",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "把明确的角色字段误判为暂定候选。",
                }
            ],
        },
    }
    history: list[dict[str, object]] = []
    step: dict[str, object] = {}

    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state={},
        history=history,
        step=step,
        is_system_beat=False,
    )

    assert retry is True
    assert step["protocol_error"] == "MESSAGE_STATE_INTENT_TOOL_MISMATCH"
    assert "_gm_message_semantics" not in context.metadata
    assert "_gm_provisional_message_semantics" in context.metadata

    call["message_semantics"]["events"][0]["state_intents"][0][
        "operation"
    ] = "contribute"
    call["message_semantics"]["events"][0]["reason"] = (
        "所属玩家明确给出自己的角色主题。"
    )
    repaired_step: dict[str, object] = {}
    retry = agent._freeze_message_semantics(
        decision=call,
        context=context,
        observed_state={},
        history=history,
        step=repaired_step,
        is_system_beat=False,
    )

    assert retry is False
    frozen = context.metadata["_gm_message_semantics"]
    assert frozen["events"][0]["state_intents"][0]["operation"] == (
        "contribute"
    )


def test_frozen_proposal_semantics_clear_lexical_confirmation_false_positive() -> None:
    event_id = "event-world-proposal"
    message = (
        "我赞成先定世界第一印象和大陆形态，这样后续设定都有依托。"
        "我有个初步想法，不知道大家觉得怎么样：这个世界或许是一片大陆。"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    assert plan.proposal_confirmation_subjects
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "text": message,
                }
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "table",
                        "targets": ["阿凛"],
                        "dialogue_act": "proposal",
                        "action_commitment": "tentative",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "world_map",
                                "summary": "提议先确定大陆形态",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "提出新的世界轮廓并征求同伴意见。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].proposal_confirmation_subjects == ()
    assert reconciled[0].proposal_subjects == ("world_map",)


def test_compound_state_intents_filter_confirmations_and_keep_new_proposal() -> None:
    event_id = "event-compound-world-intent"
    message = (
        "我同意南星补钟鸣公国的威胁，回声枯竭这个设定很棒。"
        "至于北边王国，我有个想法：钟声王国可以更庄严，大家觉得呢？"
    )
    state_summary = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-threat",
                    "summary": "回声枯竭",
                    "scope_subjects": ["world_threats"],
                },
                {
                    "id": "proposal-kingdom",
                    "summary": "钟鸣公国",
                    "scope_subjects": ["kingdoms"],
                },
            ]
        }
    }
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        state_summary=state_summary,
    )
    assert set(plan.proposal_confirmation_subjects) == {
        "kingdoms",
        "world_threats",
    }
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "阿凛",
                        "relation": "table",
                        "targets": ["南星", "时悠"],
                        "dialogue_act": "agreement",
                        "action_commitment": "none",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "confirm",
                                "scope": "world",
                                "subject": "world_threats",
                                "proposal_id": "proposal-threat",
                                "summary": "确认回声枯竭",
                            },
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "kingdoms",
                                "summary": "提议钟声王国",
                            },
                        ],
                        "responds_to_event_id": "",
                        "reason": "确认旧威胁并另提一个新王国。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].proposal_confirmation_subjects == ("world_threats",)
    assert reconciled[0].proposal_confirmations[0].proposal_ids == (
        "proposal-threat",
    )
    assert reconciled[0].proposal_subjects == ("kingdoms",)
    assert (
        GMMessageIntegrityValidator.validate_decision(
            reconciled[0],
            {
                "decision": "call_tools",
                "calls": [
                    {
                        "tool_name": "confirm_session_zero_proposal",
                        "arguments": {"proposal_id": "proposal-threat"},
                    },
                    {
                        "tool_name": "propose_session_zero_update",
                        "arguments": {
                            "summary": "提议钟声王国",
                            "world_operations": [
                                {
                                    "operation": "create",
                                    "category": "kingdoms",
                                    "value": "钟声王国",
                                }
                            ],
                        },
                    },
                ],
            },
        )
        is None
    )


def test_exact_world_subject_confirmation_cannot_absorb_other_exact_proposal() -> None:
    event_id = "event-confirm-magic-propose-local-detail"
    message = (
        "我同意魔法被严格控制、科技作为民间工具的方向；不过钟鸣公国的"
        "御魂师或许依赖获准魔法，底层改用科技，大家觉得呢？"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        state_summary={
            "session_zero": {
                "pending_proposals": [
                    {
                        "id": "proposal-magic-tech",
                        "summary": "魔法受严格控制，科技是民间工具",
                        "scope_categories": ["magic_tech_role"],
                    }
                ]
            }
        },
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "table",
                        "targets": ["白河"],
                        "dialogue_act": "agreement",
                        "action_commitment": "none",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "confirm",
                                "scope": "world",
                                "subject": "magic_tech_role",
                                "proposal_id": "proposal-magic-tech",
                                "summary": "确认魔法与科技地位",
                            },
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "custom_world_settings",
                                "target": "钟鸣公国",
                                "summary": "提议钟鸣公国的阶层使用不同技术",
                            },
                        ],
                        "responds_to_event_id": "",
                        "reason": "确认原方向并另提仍待讨论的地方设定。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=context,
    )

    assert changed == (event_id,)
    reconciled_plan = reconciled[0]
    assert reconciled_plan.proposal_subjects == ("custom_world_settings",)
    assert reconciled_plan.proposal_confirmations[0].subject == "magic_tech_role"
    issue = GMMessageIntegrityValidator.validate_decision(
        reconciled_plan,
        {
            "decision": "call_tool",
            "tool_name": "confirm_session_zero_proposal",
            "arguments": {
                "proposal_id": "proposal-magic-tech",
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "magic_tech_role",
                        "value": "魔法被严格控制，科技作为民间工具。",
                        "visibility": "public",
                    },
                    {
                        "operation": "create",
                        "category": "custom_world_settings",
                        "name": "钟鸣公国魔法与科技地位",
                        "value": "御魂师依赖获准魔法，底层改用科技。",
                        "visibility": "public",
                    },
                ],
            },
        },
    )
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONFIRMATION_ABSORBS_NEW_PROPOSAL"
    assert set(issue.required_repair_tools) == {
        "confirm_session_zero_proposal",
        "propose_session_zero_update",
    }


def test_semantic_skip_replaces_lexical_guess_and_requires_topic_receipt() -> None:
    event_id = "event-semantic-skip-mystery"
    message = "这一轮我先留白，继续听大家的。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        speaker="南星",
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "gm",
                        "targets": ["时悠"],
                        "dialogue_act": "answer",
                        "action_commitment": "answer",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "skip",
                                "scope": "world",
                                "subject": "mysteries",
                                "summary": "南星选择不贡献当前世界奥秘项",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "结合当前点名问题，玩家明确结束自己的奥秘贡献项。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=context,
    )

    assert changed == (event_id,)
    assert reconciled[0].skipped_world_categories == ("mysteries",)
    assert reconciled[0].world_categories == ()


def test_same_subject_confirmation_cannot_absorb_a_separate_new_proposal() -> None:
    event_id = "event-confirm-and-propose-map"
    message = (
        "我赞成阿凛刚才的大陆轮廓；不过我另有一个还没定的地貌想法，"
        "中央或许可以再加一道裂谷，大家觉得呢？"
    )
    state_summary = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-map-old",
                    "summary": "阿凛提出的大陆轮廓",
                    "scope_subjects": ["world_map"],
                }
            ]
        }
    }
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        state_summary=state_summary,
    )
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="白河",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "白河", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "白河",
                        "relation": "table",
                        "targets": ["阿凛", "南星"],
                        "dialogue_act": "proposal",
                        "action_commitment": "tentative",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "confirm",
                                "scope": "world",
                                "subject": "world_map",
                                "proposal_id": "proposal-map-old",
                                "summary": "确认阿凛的大陆轮廓",
                            },
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "world_map",
                                "target": "中央裂谷",
                                "summary": "另提中央裂谷作为待讨论地貌",
                            },
                        ],
                        "responds_to_event_id": "",
                        "reason": "确认旧轮廓，同时另提尚待讨论的裂谷。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, _changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )
    reconciled_plan = reconciled[0]

    assert reconciled_plan.proposal_subjects == ("world_map",)
    assert reconciled_plan.proposal_confirmations[0].replacement_required is False
    issue = GMMessageIntegrityValidator.validate_decision(
        reconciled_plan,
        {
            "decision": "call_tool",
            "tool_name": "confirm_session_zero_proposal",
            "arguments": {
                "proposal_id": "proposal-map-old",
                "replacement_world_operations": [
                    {
                        "operation": "create",
                        "category": "map_locations",
                        "name": "中央裂谷",
                        "value": "大陆中央有一道裂谷。",
                        "visibility": "public",
                    }
                ],
            },
        },
    )
    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONFIRMATION_ABSORBS_NEW_PROPOSAL"
    assert set(issue.required_repair_tools) == {
        "confirm_session_zero_proposal",
        "propose_session_zero_update",
    }


def test_receipt_followup_allows_only_python_signed_integrity_repairs_to_interleave() -> None:
    registry = GMToolRegistry()
    for tool_name in (
        "create_world_setting",
        "propose_session_zero_update",
        "unrelated_write",
    ):
        registry.register(
            GMToolDefinition(
                name=tool_name,
                description=tool_name,
                handler=lambda _context, _arguments, name=tool_name: (
                    GMToolReceipt.success(name, state_changed=True)
                ),
                side_effect="write",
            )
        )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=registry,
    )
    tool_context = execution_context()
    tool_context.gate_status = "session_zero"
    tool_context.metadata[agent._MESSAGE_INTEGRITY_METADATA_KEY] = {
        "error_code": "SESSION_ZERO_PROPOSAL_INCOMPLETE",
        "required_repair_tools": ["propose_session_zero_update"],
    }
    receipts = [
        GMToolReceipt(
            tool_name="confirm_session_zero_proposal",
            ok=True,
            state_changed=True,
            result={
                "required_followup_tools": ["create_world_setting"],
                "required_followup_calls": [
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {"value": "旧提案事实"},
                    }
                ],
            },
        )
    ]

    visible = {
        str(item.get("name") or "")
        for item in agent._available_tool_schemas(
            tool_context,
            receipts=receipts,
        )
    }
    assert visible == {
        "create_world_setting",
        "propose_session_zero_update",
    }

    history: list[dict[str, object]] = []
    retry, outcome = agent._enforce_receipt_followup(
        decision={
            "decision": "call_tool",
            "tool_name": "propose_session_zero_update",
            "arguments": {},
        },
        action="call_tool",
        context=tool_context,
        receipts=receipts,
        history=history,
        step={},
        trace=[],
    )
    assert retry is False
    assert outcome is None

    history = []
    retry, outcome = agent._enforce_receipt_followup(
        decision={
            "decision": "call_tool",
            "tool_name": "unrelated_write",
            "arguments": {},
        },
        action="call_tool",
        context=tool_context,
        receipts=receipts,
        history=history,
        step={},
        trace=[],
    )
    assert retry is True
    assert outcome is None
    assert history[-1]["protocol_error"]["error_code"] == (
        "REQUIRED_FOLLOWUP_TOOL_MISMATCH"
    )


def test_world_proposal_semantics_remove_conflicting_formal_contribution() -> None:
    event_id = "event-proposed-kingdom"
    message = (
        "那我来补一个王国吧——钟声王国北边还有个雾港联邦，"
        "由几个沿海城邦组成。大家觉得如何？"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    assert plan.world_categories == ("kingdoms",)
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "table",
                        "targets": ["阿凛"],
                        "dialogue_act": "proposal",
                        "action_commitment": "tentative",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "kingdoms",
                                "summary": "提议新增雾港联邦",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "提出新王国并征求同伴意见。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].world_categories == ()
    assert reconciled[0].proposal_subjects == ("kingdoms",)


def test_faction_proposal_semantics_remain_distinct_from_kingdoms() -> None:
    event_id = "event-proposed-faction"
    message = (
        "我来补一个组织：静默会表面研究鸣石，私下收集各地铃铛。"
        "大家觉得呢？"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "table",
                        "targets": ["阿凛"],
                        "dialogue_act": "proposal",
                        "action_commitment": "tentative",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "factions",
                                "summary": "提议静默会作为幕后组织",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "提出组织并征求同伴意见。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].proposal_subjects == ("factions",)
    assert reconciled[0].world_categories == ()


def test_location_proposal_semantics_override_lexical_kingdom_false_positive() -> None:
    event_id = "event-proposed-misty-inner-sea"
    message = (
        "南星这个群岛方向很有画面感！我补充一点：可以有一片被薄雾常年"
        "笼罩的内海，群岛散布其中，有些岛屿只有特定季节才浮现，像是被"
        "潮汐和风决定。这样既能体现日常感，又能藏下神秘遗迹的线索。"
        "大家觉得呢？"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    assert plan.world_categories == ("kingdoms",)
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "阿凛",
                        "relation": "table",
                        "targets": ["南星"],
                        "dialogue_act": "proposal",
                        "action_commitment": "tentative",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "propose",
                                "scope": "world",
                                "subject": "major_locations",
                                "target": "薄雾内海",
                                "summary": "提议在群岛间加入薄雾内海",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "提出地点设定并征求同伴意见。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].world_categories == ()
    assert reconciled[0].proposal_subjects == ("major_locations",)


def test_no_state_intents_clear_lexical_world_write_obligations() -> None:
    event_id = "event-world-discussion-only"
    message = (
        "我补充一点：王国和群岛这个方向挺有意思，不过这只是评论，"
        "你们继续聊。"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    assert plan.world_categories == ("kingdoms",)
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "阿凛",
                        "relation": "table",
                        "targets": ["南星"],
                        "dialogue_act": "discussion",
                        "action_commitment": "none",
                        "state_scope": "none",
                        "state_intents": [],
                        "responds_to_event_id": "",
                        "reason": "评价同伴讨论，没有提交或提议新设定。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].world_categories == ()
    assert reconciled[0].proposal_subjects == ()


def test_safety_skip_semantics_clear_lexical_false_declaration() -> None:
    event_id = "event-no-more-safety-boundaries"
    message = "我这边没有要补充的界限或帷幕。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    assert plan.safety_declarations
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="白河",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "白河", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "白河",
                        "relation": "gm",
                        "targets": ["时悠"],
                        "dialogue_act": "answer",
                        "action_commitment": "answer",
                        "state_scope": "safety",
                        "state_intents": [
                            {
                                "operation": "skip",
                                "scope": "safety",
                                "subject": "safety_boundary",
                                "summary": "本轮没有新增界限或帷幕",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "回答安全准则提问，明确没有新增内容。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )
    missing_receipt_issue = GMMessageIntegrityValidator.validate_terminal(
        reconciled[0],
        [],
    )
    completion_receipt = GMToolReceipt.success(
        "mark_session_zero_topic_complete",
        result={"topic": "safety", "source_event_id": event_id},
        state_changed=True,
    )

    assert changed == (event_id,)
    assert reconciled[0].safety_declarations == ()
    assert missing_receipt_issue is not None
    assert missing_receipt_issue.error_code == "SESSION_ZERO_TOPIC_SKIP_INCOMPLETE"
    assert missing_receipt_issue.required_repair_tools == (
        "mark_session_zero_topic_complete",
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            reconciled[0],
            [completion_receipt],
        )
        is None
    )


def test_safety_contribution_semantics_keep_concrete_receipt_obligation() -> None:
    event_id = "event-explicit-safety-veil"
    message = "帷幕：过于残酷的身体伤害细节。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "gm",
                        "targets": ["时悠"],
                        "dialogue_act": "state_contribution",
                        "action_commitment": "committed",
                        "state_scope": "safety",
                        "state_intents": [
                            {
                                "operation": "contribute",
                                "scope": "safety",
                                "subject": "safety_boundary",
                                "summary": "残酷身体伤害细节作为帷幕",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "玩家明确声明一条帷幕。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )
    issue = GMMessageIntegrityValidator.validate_terminal(reconciled[0], [])

    assert changed == ()
    assert reconciled[0].safety_declarations == plan.safety_declarations
    assert issue is not None
    assert issue.error_code == "SAFETY_BOUNDARY_INCOMPLETE"


def test_player_skill_advice_does_not_create_a_hero_write_obligation() -> None:
    event_id = "event-player-skill-advice"
    message = (
        "白河，洛岚的技能和装备听起来很扎实，能修能打。不过你选的"
        "‘碎骨’和‘破防打击’会不会太偏攻击？她要是想边修齿轮边战斗，"
        "也许可以留个位置给修理或制造相关的技能？当然，这只是我的直觉，"
        "你看着调。"
    )
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        speaker="阿凛",
    )
    assert plan.hero_fields == ("skills",)
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "阿凛",
                        "relation": "player",
                        "targets": ["白河"],
                        "dialogue_act": "discussion",
                        "action_commitment": "none",
                        "state_scope": "none",
                        "state_intents": [],
                        "responds_to_event_id": "",
                        "reason": "向另一名玩家建议其角色技能，没有替对方定稿。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].hero_fields == ()
    assert GMMessageIntegrityValidator.validate_terminal(reconciled[0], []) is None


def test_exact_hero_semantics_become_receipt_field_obligations() -> None:
    event_id = "event-hero-theme"
    message = "她最怕有人被彻底遗忘，所以总想替别人守住名字。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
        speaker="南星",
    )
    assert plan.hero_fields == ()
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "南星", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "南星",
                        "relation": "gm",
                        "targets": ["时悠"],
                        "dialogue_act": "answer",
                        "action_commitment": "answer",
                        "state_scope": "hero",
                        "state_intents": [
                            {
                                "operation": "contribute",
                                "scope": "hero",
                                "subject": "hero_theme",
                                "target": "赛璃",
                                "summary": "不让任何人被彻底遗忘。",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "回答主持人对角色核心驱动的提问。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].hero_fields == ("theme",)


def test_committed_faction_semantics_require_a_political_community_write() -> None:
    event_id = "event-committed-faction"
    message = "就定静默会吧，它负责收集各地失传的铃声。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        source_event_id=event_id,
    )
    tool_context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={
            "current_turn_events": [
                {"event_id": event_id, "speaker": "阿凛", "text": message}
            ],
            "_gm_message_semantics": {
                "version": "1",
                "events": [
                    {
                        "event_id": event_id,
                        "speaker": "阿凛",
                        "relation": "gm",
                        "targets": ["时悠"],
                        "dialogue_act": "state_contribution",
                        "action_commitment": "committed",
                        "state_scope": "world",
                        "state_intents": [
                            {
                                "operation": "contribute",
                                "scope": "world",
                                "subject": "factions",
                                "target": "静默会",
                                "summary": "静默会负责收集失传铃声",
                            }
                        ],
                        "responds_to_event_id": "",
                        "reason": "正式提交一个组织设定。",
                    }
                ],
            },
        },
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )

    reconciled, changed = agent._reconcile_integrity_plans_with_message_semantics(
        (plan,),
        context=tool_context,
    )

    assert changed == (event_id,)
    assert reconciled[0].world_categories == ("kingdoms",)


def test_semantic_confirm_must_target_an_existing_pending_proposal() -> None:
    event_id = "event-confirm-already-formal-history"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "阿凛",
            "text": "我赞成把大寂潮作为重大历史事件；另外我建议补一段后果。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "historical_events",
                            "target": "大寂潮",
                            "summary": "赞成既有历史事件并补充后果",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "赞成上一位玩家。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-unrelated",
                    "summary": "将归潮祭设为共同传统",
                    "world_operations": [
                        {
                            "operation": "create",
                            "category": "custom_world_settings",
                            "name": "归潮祭",
                            "visibility": "public",
                        }
                    ],
                }
            ]
        }
    }

    error = agent._session_zero_semantics_grounding_error(
        semantics,
        context=context,
        observed_state=observed_state,
    )

    assert error is not None
    assert error.code == "MESSAGE_CONFIRM_TARGET_NOT_PENDING"
    assert "归潮祭" in error.correction_hint


def test_semantic_confirm_accepts_matching_pending_target() -> None:
    event_id = "event-confirm-pending-history"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "阿凛",
            "text": "我赞成把大寂潮作为重大历史事件。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "historical_events",
                            "target": "大寂潮",
                            "summary": "赞成待定的大寂潮历史事件",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "确认现存待定提案。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-history",
                    "summary": "把大寂潮作为群岛的重大历史事件",
                    "world_operations": [
                        {
                            "operation": "create",
                            "category": "historical_events",
                            "value": "大寂潮令所有钟声静默三日",
                            "visibility": "public",
                        }
                    ],
                }
            ]
        }
    }

    assert (
        agent._session_zero_semantics_grounding_error(
            semantics,
            context=context,
            observed_state=observed_state,
        )
        is None
    )


def test_semantic_confirm_rejects_own_pending_proposal_in_multiplayer() -> None:
    event_id = "event-self-confirm-tone"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "阿凛",
            "text": "我的想法和南星的不冲突，我提个综合版本，大家觉得呢？",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星", "白河"],
                    "dialogue_act": "proposal",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "tone_preferences",
                            "target": "阿凛原先的基调",
                            "proposal_id": "proposal-own-tone",
                            "summary": "确认自己的旧基调",
                        },
                        {
                            "operation": "propose",
                            "scope": "world",
                            "subject": "tone_preferences",
                            "target": "综合基调",
                            "summary": "提出综合基调并征求全桌意见",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "错误地把自己的旧提案也算作全桌确认。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "participants": ["阿凛", "南星", "白河"],
            "pending_proposals": [
                {
                    "id": "proposal-own-tone",
                    "speaker": "阿凛",
                    "summary": "希望感较强的史诗奇幻",
                    "scope_categories": ["tone_preferences"],
                    "subject_keys": [
                        {
                            "category": "tone_preferences",
                            "visibility": "public",
                            "name": "",
                            "singleton": True,
                        }
                    ],
                }
            ],
        }
    }

    error = agent._session_zero_semantics_grounding_error(
        semantics,
        context=context,
        observed_state=observed_state,
    )

    assert error is not None
    assert error.code == "MESSAGE_CONFIRM_OWN_PROPOSAL"
    assert "superseded_proposal_ids" in error.correction_hint


def test_semantic_freeze_drops_only_own_confirmation_from_compound_message() -> None:
    event_id = "event-confirm-other-and-restate-own"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "白河",
            "text": "钟声国度挺好，第七采掘城就作为边境重镇吧。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "白河",
                    "relation": "table",
                    "targets": ["阿凛", "南星"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "kingdoms",
                            "target": "钟声国度",
                            "proposal_id": "proposal-bell",
                            "summary": "赞成阿凛提出的钟声国度",
                        },
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "kingdoms",
                            "target": "第七采掘城",
                            "proposal_id": "proposal-own-mine",
                            "summary": "重申自己的采掘城提案",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "同句赞成他人方案并重申自己的旧提案。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="白河",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "participants": ["阿凛", "南星", "白河"],
            "pending_proposals": [
                {
                    "id": "proposal-bell",
                    "speaker": "阿凛",
                    "summary": "内海北岸的钟声国度",
                    "scope_categories": ["kingdoms"],
                },
                {
                    "id": "proposal-own-mine",
                    "speaker": "白河",
                    "summary": "第七采掘城是边境重镇",
                    "scope_categories": ["kingdoms"],
                },
            ],
        }
    }

    normalized, ignored = agent._normalize_session_zero_self_confirmations(
        semantics,
        context=context,
        observed_state=observed_state,
    )

    assert [item.proposal_id for item in normalized.events[0].state_intents] == [
        "proposal-bell"
    ]
    assert normalized.events[0].state_scope == "world"
    assert ignored == [
        {
            "event_id": event_id,
            "proposal_id": "proposal-own-mine",
            "subject": "kingdoms",
        }
    ]
    assert (
        agent._session_zero_semantics_grounding_error(
            normalized,
            context=context,
            observed_state=observed_state,
        )
        is None
    )


def test_semantic_world_shape_confirm_does_not_bind_pending_map_location() -> None:
    event_id = "event-restate-formal-world-shape"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "阿凛",
            "text": (
                "那大陆形态就定了：群岛大陆，季风环绕，魔法稀而不怪。"
                "接下来聊国家贡献或历史事件？"
            ),
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星"],
                    "dialogue_act": "proposal",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "world_shape",
                            "target": "群岛大陆",
                            "summary": "确认既成的群岛大陆形态",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "错误地把既成事实再次解释为待确认提案。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "world_canon": {"world_shape": "被季风环绕的群岛大陆"},
            "pending_proposals": [
                {
                    "id": "proposal-border-inn",
                    "speaker": "南星",
                    "summary": "在王国边境设置一座古老驿站",
                    "subject_keys": [
                        {
                            "category": "map_locations",
                            "visibility": "public",
                            "name": "边境驿站",
                        }
                    ],
                    "world_operations": [
                        {
                            "operation": "create",
                            "category": "map_locations",
                            "name": "边境驿站",
                            "visibility": "public",
                        }
                    ],
                }
            ],
        }
    }

    error = agent._session_zero_semantics_grounding_error(
        semantics,
        context=context,
        observed_state=observed_state,
    )

    assert error is not None
    assert error.code == "MESSAGE_CONFIRM_TARGET_NOT_PENDING"
    assert "古老驿站" in error.correction_hint


def test_semantic_confirm_uses_proposal_id_when_human_label_is_not_a_substring(
) -> None:
    event_id = "event-confirm-memory-bell"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "南星",
            "text": "这个呼应很棒，我赞成加入这个事件。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "南星",
                    "relation": "table",
                    "targets": ["阿凛"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "historical_events",
                            "target": "流浪钟匠的记忆钟事件",
                            "proposal_id": "proposal-memory-bell",
                            "summary": "赞成加入流浪钟匠留下记忆钟的事件",
                        }
                    ],
                    "responds_to_event_id": "",
                    "reason": "确认上一位玩家的待定历史事件。",
                }
            ],
        },
        source_events=source_events,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="南星",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )
    observed_state = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-other-history",
                    "summary": "把静默之夜设为重大历史事件",
                    "world_operations": [
                        {
                            "operation": "create",
                            "category": "historical_events",
                            "value": "静默之夜令记忆钟全部失声。",
                            "visibility": "public",
                        }
                    ],
                },
                {
                    "id": "proposal-memory-bell",
                    "summary": "流浪钟匠留下一枚没有刻字的记忆钟",
                    "world_operations": [
                        {
                            "operation": "create",
                            "category": "historical_events",
                            "value": "流浪钟匠留下的无字钟在深夜传出孩子的求救声。",
                            "visibility": "public",
                        }
                    ],
                },
            ]
        }
    }

    assert (
        agent._session_zero_semantics_grounding_error(
            semantics,
            context=context,
            observed_state=observed_state,
        )
        is None
    )


def test_semantic_confirm_prefers_newer_composite_over_older_parts() -> None:
    event_id = "event-confirm-composite-kingdoms"
    source_events = [
        {
            "event_id": event_id,
            "speaker": "阿凛",
            "text": "听起来不错，就按这个关系定。接着聊魔法和科技吧。",
        }
    ]
    semantics = GMMessageSemantics.parse(
        {
            "version": "1",
            "events": [
                {
                    "event_id": event_id,
                    "speaker": "阿凛",
                    "relation": "table",
                    "targets": ["南星"],
                    "dialogue_act": "agreement",
                    "action_commitment": "none",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "kingdoms",
                            "target": "钟鸣公国",
                            "proposal_id": "proposal-bell",
                            "summary": "确认钟鸣公国",
                        },
                        {
                            "operation": "confirm",
                            "scope": "world",
                            "subject": "kingdoms",
                            "target": "白花碑驿站",
                            "proposal_id": "proposal-station",
                            "summary": "确认白花碑驿站",
                        },
                    ],
                    "responds_to_event_id": "",
                    "reason": "错误地把最新组合方案拆回两个旧稿。",
                }
            ],
        },
        source_events=source_events,
    )
    observed_state = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-bell",
                    "speaker": "南星",
                    "summary": "钟鸣公国位于北部山脚",
                    "scope_categories": ["kingdoms"],
                    "subject_keys": [
                        {
                            "category": "kingdoms",
                            "visibility": "public",
                            "name": "钟鸣公国",
                        }
                    ],
                },
                {
                    "id": "proposal-station",
                    "speaker": "阿凛",
                    "summary": "白花碑驿站是故事起点",
                    "scope_categories": ["kingdoms"],
                    "subject_keys": [
                        {
                            "category": "kingdoms",
                            "visibility": "public",
                            "name": "白花碑驿站",
                        }
                    ],
                },
                {
                    "id": "proposal-composite",
                    "speaker": "南星",
                    "summary": "钟鸣公国更北，白花碑驿站是交汇点",
                    "scope_categories": ["kingdoms"],
                    "subject_keys": [
                        {
                            "category": "kingdoms",
                            "visibility": "public",
                            "name": "钟鸣公国",
                        },
                        {
                            "category": "kingdoms",
                            "visibility": "public",
                            "name": "白花碑驿站",
                        },
                    ],
                },
            ]
        }
    }
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = GMToolExecutionContext(
        campaign_id="scope-test",
        session_id="s0",
        channel_id="group",
        speaker="阿凛",
        gate_status="session_zero",
        metadata={"current_turn_events": source_events},
    )

    error = agent._session_zero_semantics_grounding_error(
        semantics,
        context=context,
        observed_state=observed_state,
    )

    assert error is not None
    assert error.code == "MESSAGE_CONFIRM_NEWER_COMPOSITE_PENDING"
    assert "proposal-composite" in error.correction_hint


def test_public_reply_detects_only_known_internal_proposal_ids() -> None:
    leaked = LLMGMToolAgent._exposed_internal_proposal_ids(
        "你要确认 proposal-map-b 还是上一版？",
        observed_state={
            "session_zero": {
                "pending_proposals": [
                    {"id": "proposal-map-a", "summary": "环形大陆"},
                    {"id": "proposal-map-b", "summary": "碎裂群岛"},
                ]
            }
        },
        receipts=[],
    )

    assert leaked == ("proposal-map-b",)
    assert not LLMGMToolAgent._exposed_internal_proposal_ids(
        "你赞成环形大陆，还是碎裂群岛？",
        observed_state={
            "pending_proposals": [{"id": "proposal-map-a"}]
        },
        receipts=[],
    )


def test_final_publish_boundary_redacts_internal_proposal_ids_from_all_parts() -> None:
    receipt = GMToolReceipt.success(
        "propose_session_zero_update",
        result={"proposal_id": "proposal-secret-123"},
        state_changed=True,
    )
    outcome = GMToolAgentOutcome(
        handled=True,
        reply="请确认 proposal-secret-123。",
        reply_parts=["第一段", "proposal-secret-123 已保存"],
        receipts=[receipt],
    )

    sanitized = LLMGMToolAgent._redact_internal_identifiers_at_publish_boundary(
        outcome
    )

    assert "proposal-secret-123" not in sanitized.reply
    assert all("proposal-secret-123" not in part for part in sanitized.reply_parts)
    assert receipt.result["proposal_id"] == "proposal-secret-123"
    assert sanitized.trace[-1]["decision"] == "public_safety_redaction"


def test_semantically_reviewed_session_zero_proposal_marks_receipt_complete() -> None:
    receipt = GMToolReceipt.success(
        "propose_session_zero_update",
        result={"proposal": {"summary": "待定森林提案"}},
        state_changed=True,
    )

    LLMGMToolAgent._annotate_semantically_complete_proposals(
        [receipt],
        step={
            "tool_proposal_grounding": [
                {
                    "tool_name": "propose_session_zero_update",
                    "valid": True,
                }
            ]
        },
    )

    assert receipt.result["semantic_source_complete"] is True


def test_standalone_safety_receipts_lock_short_non_repeating_confirmation() -> None:
    receipts = [
        GMToolReceipt(
            tool_name="record_safety_boundary",
            ok=True,
            state_changed=True,
            public_fallback_reply="ok，已记录这条界限。",
        ),
        GMToolReceipt(
            tool_name="record_safety_boundary",
            ok=True,
            state_changed=True,
            public_fallback_reply="ok，已记录这条帷幕。",
        ),
    ]

    LLMGMToolAgent._mark_standalone_safety_confirmation(
        receipts,
        mixed_message=False,
    )

    assert all(receipt.lock_public_reply for receipt in receipts)
    assert GMToolReceiptPolicy.authoritative_reply(receipts) == (
        "ok，已记录这条界限。\nok，已记录这条帷幕。"
    )


def test_safety_receipt_does_not_lock_when_message_has_another_question() -> None:
    receipt = GMToolReceipt(
        tool_name="record_safety_boundary",
        ok=True,
        state_changed=True,
        public_fallback_reply="ok，已记录这条界限。",
    )

    LLMGMToolAgent._mark_standalone_safety_confirmation(
        [receipt],
        mixed_message=True,
    )

    assert receipt.lock_public_reply is False


class FailureReplyObligationVerifier:
    def __init__(self, *, requires_gm_reply: bool) -> None:
        self.requires_gm_reply = requires_gm_reply
        self.calls: list[dict[str, object]] = []

    def verify_silence_responsibility(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            requires_gm_reply=self.requires_gm_reply,
            category=(
                "npc_or_world_interaction"
                if self.requires_gm_reply
                else "player_discussion"
            ),
            reason=(
                "玩家已经对牢门执行开锁行动。"
                if self.requires_gm_reply
                else "这是玩家之间的讨论。"
            ),
        )


class ReceiptAwareSessionZeroCompletionVerifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def verify_silence_responsibility(self, **kwargs):
        self.calls.append(dict(kwargs))
        receipts = list(kwargs.get("completed_receipts") or [])
        complete = bool(receipts) and all(
            receipt.result.get("silent_commit_allowed") is True
            and receipt.result.get("source_message_already_public") is True
            and receipt.result.get("completion_scope") == "source_statement"
            for receipt in receipts
            if receipt.ok and receipt.state_changed
        )
        return SimpleNamespace(
            requires_gm_reply=not complete,
            category="direct_gm_request",
            reason=(
                "当前回答已经写入；其余角色字段属于后续桌面轮次。"
                if complete
                else "本轮仍有独立主持事项。"
            ),
        )


def execution_context(*, campaign_id: str = "agent-test", speaker: str = "阿凛") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
    )


def test_empty_final_receives_explicit_protocol_feedback_before_retry() -> None:
    client = ScriptedClient(
        [
            json.dumps(
                {"decision": "final", "reply": "", "reason": "需要回应。"},
                ensure_ascii=False,
            ),
            json.dumps(
                {"decision": "final", "reply": "我在。", "reason": "直接回应。"},
                ensure_ascii=False,
            ),
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=GMToolRegistry(),
    )

    outcome = agent.run(
        "悠老师？",
        recent_context="",
        context=execution_context(),
        state_summary={},
    )

    assert outcome.reply == "我在。"
    assert len(client.calls) == 2
    retry_context = "\n".join(
        item.content for item in client.calls[1]["messages"]
    )
    assert "TERMINAL_REPLY_REQUIRED" in retry_context


def semantic_context(
    events: list[dict[str, object]],
    *,
    speaker: str = "村夫",
) -> GMToolExecutionContext:
    context = execution_context(speaker=speaker)
    context.directly_addressed = False
    context.metadata.update(
        {
            "current_turn_events": events,
            "message_semantics_contract_required": True,
        }
    )
    return context


def test_adventure_post_tool_reply_reviews_committed_state_bearing_action() -> None:
    event_id = "event-farewell-and-move"
    context = semantic_context(
        [
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "霍恩先生，我们现在就去荒坡看看。",
            }
        ],
        speaker="南星",
    )
    context.metadata[LLMGMToolAgent._MESSAGE_SEMANTICS_METADATA_KEY] = {
        "version": "1",
        "events": [
            {
                "event_id": event_id,
                "speaker": "南星",
                "relation": "npc",
                "targets": ["老钟匠霍恩"],
                "dialogue_act": "roleplay_speech",
                "action_commitment": "committed",
                "state_scope": "scene",
                "state_intents": [
                    {
                        "operation": "contribute",
                        "scope": "scene",
                        "subject": "scene_fact",
                        "summary": "赛璃决定立即前往东边荒坡调查。",
                    }
                ],
                "responds_to_event_id": "",
                "reason": "告别中同时声明了实际移动。",
            }
        ],
    }
    agent = object.__new__(LLMGMToolAgent)

    assert agent._should_review_post_tool_public_reply(
        decision={"message_kind": "npc_or_world_interaction", "audience": "npc"},
        context=context,
    )


def test_adventure_post_tool_reply_skips_plain_npc_speech_without_state_intent() -> None:
    event_id = "event-plain-npc-speech"
    context = semantic_context(
        [
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "霍恩先生，谢谢您。",
            }
        ],
        speaker="南星",
    )
    context.metadata[LLMGMToolAgent._MESSAGE_SEMANTICS_METADATA_KEY] = {
        "version": "1",
        "events": [
            {
                "event_id": event_id,
                "speaker": "南星",
                "relation": "npc",
                "targets": ["老钟匠霍恩"],
                "dialogue_act": "roleplay_speech",
                "action_commitment": "committed",
                "responds_to_event_id": "",
                "reason": "只向NPC致谢。",
            }
        ],
    }
    agent = object.__new__(LLMGMToolAgent)

    assert not agent._should_review_post_tool_public_reply(
        decision={"message_kind": "npc_or_world_interaction", "audience": "npc"},
        context=context,
    )


def test_player_agreement_cannot_resolve_an_unrelated_rule_window() -> None:
    executed: list[str] = []
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="resolve_rule_window",
            description="resolve",
            handler=lambda _context, _arguments: (
                executed.append("resolved")
                or GMToolReceipt.success(
                    "resolve_rule_window",
                    state_changed=True,
                    public_reply="已结算。",
                    lock_public_reply=True,
                )
            ),
            side_effect="write",
        )
    )
    events = [
        {
            "event_id": "event-loading",
            "speaker": "loading",
            "text": "要不我也试试看看能有多少人",
        },
        {"event_id": "event-villager", "speaker": "村夫", "text": "行"},
    ]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-loading",
                "speaker": "loading",
                "relation": "player",
                "targets": ["村夫"],
                "dialogue_act": "proposal",
                "action_commitment": "tentative",
                "responds_to_event_id": "",
                "reason": "向队友提出观察方案。",
            },
            {
                "event_id": "event-villager",
                "speaker": "村夫",
                "relation": "player",
                "targets": ["loading"],
                "dialogue_act": "agreement",
                "action_commitment": "none",
                "responds_to_event_id": "event-loading",
                "reason": "同意队友刚提出的方案。",
            },
        ],
    }
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": semantics,
                    "message_kind": "discussion",
                    "audience": "players",
                    "tool_name": "resolve_rule_window",
                    "arguments": {"source_event_id": "event-villager"},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "silent",
                    "message_kind": "discussion",
                    "audience": "players",
                    "reason": "玩家正在彼此商量。",
                },
                ensure_ascii=False,
            ),
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=registry)

    outcome = agent.run(
        "loading提出观察人数，村夫回答行。",
        recent_context="",
        context=semantic_context(events),
        state_summary={},
    )

    assert executed == []
    assert outcome.target == "silent"
    assert outcome.message_semantics == semantics
    assert outcome.trace[0]["protocol_error"] == (
        "RULE_WINDOW_NOT_ANSWERED_BY_SOURCE_MESSAGE"
    )


def test_committed_action_blocked_by_pending_window_returns_existing_prompt() -> None:
    event_id = "event-open-door"
    context = semantic_context(
        [
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "text": "我把碎片按进锁孔，试试能不能转动它。",
            }
        ],
        speaker="阿凛",
    )
    context.metadata[LLMGMToolAgent._MESSAGE_SEMANTICS_METADATA_KEY] = {
        "version": "1",
        "events": [
            {
                "event_id": event_id,
                "speaker": "阿凛",
                "relation": "gm",
                "targets": ["时悠"],
                "dialogue_act": "action_declaration",
                "action_commitment": "committed",
                "responds_to_event_id": "",
                "reason": "玩家开始执行新的开门动作。",
            }
        ],
    }
    decision = {
        "decision": "call_tool",
        "tool_name": "commit_scene_fixture_action",
        "arguments": {"source_event_id": event_id},
    }
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    receipt = GMToolReceipt.failure(
        "commit_scene_fixture_action",
        "BLOCKING_DECISION_PENDING",
        "当前仍有必须先回答的规则选择。",
        "先处理待决窗口。",
        result={
            "pending_windows": [
                {"window_id": "critical-1", "kind": "critical_opportunity"}
            ]
        },
    )

    assert agent._decision_commits_new_action(
        decision=decision,
        context=context,
    )
    outcome = agent._pending_decision_prompt_outcome(
        observed_state={
            "gameplay": {
                "pending_decisions": [
                    {
                        "window_id": "critical-1",
                        "kind": "critical_opportunity",
                        "prompt": "这次大成功带来一个机会，你想要怎么使用它？",
                    }
                ]
            }
        },
        blocking_receipt=receipt,
        receipts=[receipt],
        trace=[],
    )

    assert outcome.mode == "gm_agent_ask_user"
    assert outcome.terminal_action == "ask_user"
    assert outcome.reply == "这次大成功带来一个机会，你想要怎么使用它？"


def test_frozen_player_discussion_skips_second_silence_model_call() -> None:
    events = [
        {
            "event_id": "event-plan",
            "speaker": "村夫",
            "text": "你先和卡尔说话，我在旁边看看情况。",
        }
    ]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-plan",
                "speaker": "村夫",
                "relation": "player",
                "targets": ["loading"],
                "dialogue_act": "proposal",
                "action_commitment": "tentative",
                "responds_to_event_id": "",
                "reason": "玩家向队友提出暂定分工。",
            }
        ],
    }
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "silent",
                    "message_semantics": semantics,
                    "message_kind": "discussion",
                    "audience": "players",
                    "reason": "玩家正在商量分工。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = FailureReplyObligationVerifier(requires_gm_reply=True)
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=GMToolRegistry(),
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        events[0]["text"],
        recent_context="loading正在询问队友的打算。",
        context=semantic_context(events),
        state_summary={},
    )

    assert outcome.target == "silent"
    assert verifier.calls == []
    assert outcome.trace[0]["silence_responsibility"]["model_call_skipped"]


def test_independent_review_suppresses_core_reply_to_unaddressed_table_speculation(
    ) -> None:
    event_id = "event-lock-speculation"
    events = [
        {
            "event_id": event_id,
            "speaker": "白河",
            "text": "锁孔发烫，也许不是靠转动，而是需要注入魔力？",
        }
    ]
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "final",
                    "message_semantics": {
                        "version": "1",
                        "events": [
                            {
                                "event_id": event_id,
                                "speaker": "白河",
                                "relation": "gm",
                                "targets": ["时悠"],
                                "dialogue_act": "question",
                                "action_commitment": "none",
                                "response_expectation": "gm",
                                "responds_to_event_id": "",
                                "reason": "核心模型误以为玩家正在询问主持人。",
                            }
                        ],
                    },
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "reply": "这个猜测很有道理，要进行检定吗？",
                    "reason": "回应玩家关于机关原理的提问。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = FailureReplyObligationVerifier(requires_gm_reply=False)
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=GMToolRegistry(),
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        events[0]["text"],
        recent_context="队友刚尝试拔出卡住的碎片，众人仍在讨论办法。",
        context=semantic_context(events, speaker="白河"),
        state_summary={},
    )

    assert outcome.target == "silent"
    assert outcome.reply == ""
    assert len(verifier.calls) == 1
    assert verifier.calls[0]["proposed_public_reply"] == ""
    assert verifier.calls[0]["proposed_message_kind"] == ""
    assert verifier.calls[0]["proposed_audience"] == ""
    assert verifier.calls[0]["proposed_delivery"] == {
        "transport_directly_addressed": False,
        "transport_is_private": False,
    }
    review = outcome.trace[0]["reply_responsibility"]
    assert review["requires_gm_reply"] is False
    assert review["category"] == "player_discussion"
    normalization = outcome.trace[0]["semantic_normalization"]
    assert normalization["source"] == (
        "independent_reply_responsibility_review"
    )
    assert normalization["effective"] == {
        "message_kind": "discussion",
        "audience": "players",
    }


def test_frozen_committed_action_cannot_be_silenced_without_receipt() -> None:
    event_id = "event-go-library"
    context = semantic_context(
        [
            {
                "event_id": event_id,
                "speaker": "南星",
                "text": "好，那我们一起去图书馆。",
            }
        ],
        speaker="南星",
    )
    context.metadata[LLMGMToolAgent._MESSAGE_SEMANTICS_METADATA_KEY] = {
        "version": "1",
        "events": [
            {
                "event_id": event_id,
                "speaker": "南星",
                "relation": "table",
                "targets": ["阿凛", "白河"],
                "dialogue_act": "agreement",
                "action_commitment": "committed",
                "response_expectation": "table",
                "state_scope": "scene",
                "state_intents": [
                    {
                        "operation": "contribute",
                        "scope": "scene",
                        "subject": "scene_fact",
                        "target": "静默图书馆",
                        "summary": "赛璃立即随队前往静默图书馆。",
                    }
                ],
                "responds_to_event_id": "",
                "reason": "同意并落实移动。",
            }
        ],
    }
    verifier = FailureReplyObligationVerifier(requires_gm_reply=False)
    agent = object.__new__(LLMGMToolAgent)
    agent.reply_grounding_verifier = verifier
    history: list[dict[str, object]] = []
    step: dict[str, object] = {}

    blocked = agent._silence_responsibility_requires_reply(
        action="silent",
        decision={"decision": "silent", "reason": "玩家正在讨论。"},
        context=context,
        current_message="好，那我们一起去图书馆。",
        recent_context="队友已经同意出发。",
        receipts=[],
        history=history,
        trace=[],
        step=step,
        deadline=999999999.0,
        is_system_beat=False,
    )

    assert blocked is True
    assert verifier.calls == []
    assert history[-1]["protocol_error"]["error_code"] == (
        "COMMITTED_ACTION_UNRESOLVED"
    )
    assert step["silence_responsibility"]["event_ids"] == [event_id]


def test_frozen_gm_question_keeps_independent_silence_review() -> None:
    events = [
        {
            "event_id": "event-question",
            "speaker": "村夫",
            "text": "时悠，现场一共有多少人？",
        }
    ]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-question",
                "speaker": "村夫",
                "relation": "gm",
                "targets": ["时悠"],
                "dialogue_act": "question",
                "action_commitment": "none",
                "responds_to_event_id": "",
                "reason": "玩家直接向主持人询问现场事实。",
            }
        ],
    }
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "silent",
                    "message_semantics": semantics,
                    "message_kind": "discussion",
                    "audience": "players",
                    "reason": "误判为桌面讨论。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = FailureReplyObligationVerifier(requires_gm_reply=False)
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=GMToolRegistry(),
        reply_grounding_verifier=verifier,
    )

    outcome = agent.run(
        events[0]["text"],
        recent_context="双方巡逻队正在路口对峙。",
        context=semantic_context(events),
        state_summary={},
    )

    assert outcome.target == "silent"
    assert len(verifier.calls) == 1
    assert not outcome.trace[0]["silence_responsibility"].get(
        "model_call_skipped"
    )


def test_frozen_session_zero_answer_and_source_receipt_skip_completion_review(
    ) -> None:
    events = [
        {
            "event_id": "event-tone",
            "speaker": "阿凛",
            "text": "我希望故事危险但始终保留希望。",
        }
    ]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-tone",
                "speaker": "阿凛",
                "relation": "gm",
                "targets": ["时悠"],
                    "dialogue_act": "answer",
                    "action_commitment": "answer",
                    "state_scope": "world",
                    "state_intents": [
                        {
                            "operation": "contribute",
                            "scope": "world",
                            "subject": "playstyle_themes",
                            "target": "",
                            "summary": "故事危险但始终保留希望",
                        }
                    ],
                    "responds_to_event_id": "",
                "reason": "回答主持人的基调邀请。",
            }
        ],
    }
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="登记明确的世界设定贡献。",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "create_world_setting",
                        result={
                            "category": "playstyle_themes",
                            "operation": "create",
                            "visibility": "public",
                            "authority": "player_confirmed",
                            "silent_commit_allowed": True,
                        "source_message_already_public": True,
                        "source_event": {"event_id": "event-tone"},
                },
                state_changed=True,
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": semantics,
                    "message_kind": "state_contribution",
                    "has_independent_followup": False,
                    "audience": "gm",
                    "tool_name": "create_world_setting",
                    "arguments": {},
                    "reason": "登记玩家明确回答的基调。",
                },
                ensure_ascii=False,
            )
        ]
    )
    verifier = ReceiptAwareSessionZeroCompletionVerifier()
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=verifier,
    )
    context = semantic_context(events, speaker="阿凛")
    context.gate_status = "session_zero"

    outcome = agent.run(
        events[0]["text"],
        recent_context="时悠询问大家希望怎样的故事基调。",
        context=context,
        state_summary={},
    )

    assert outcome.target == "silent"
    assert verifier.calls == []
    assert outcome.trace[0]["post_tool_completion_model_call_skipped"]


def test_session_zero_proposal_cannot_take_source_receipt_fast_path() -> None:
    events = [
        {
            "event_id": "event-proposal",
            "speaker": "阿凛",
            "text": "国家先叫索朗帝国，历史事件请时悠帮我们想一个。",
        }
    ]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-proposal",
                "speaker": "阿凛",
                "relation": "gm",
                "targets": ["时悠"],
                "dialogue_act": "proposal",
                "action_commitment": "none",
                "responds_to_event_id": "",
                "reason": "既提出国家名称，也把历史事件创作委托给主持人。",
            }
        ],
    }
    receipt = GMToolReceipt.success(
        "create_world_setting",
        result={
            "silent_commit_allowed": True,
            "source_message_already_public": True,
            "source_event": {"event_id": "event-proposal"},
        },
        state_changed=True,
    )
    agent = LLMGMToolAgent(
        ScriptedClient([]),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = semantic_context(events, speaker="阿凛")
    context.gate_status = "session_zero"
    context.metadata[agent._MESSAGE_SEMANTICS_METADATA_KEY] = semantics

    can_skip = agent._frozen_semantics_and_receipts_prove_complete_statement(
        decision={
            "message_kind": "state_contribution",
            "has_independent_followup": False,
        },
        context=context,
        completed_receipts=[receipt],
    )

    assert can_skip is False


def test_frozen_semantics_ignores_rewrite_and_still_blocks_player_action() -> None:
    executed: list[str] = []
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="perform_character_action",
            description="act",
            handler=lambda _context, _arguments: (
                executed.append("acted")
                or GMToolReceipt.success(
                    "perform_character_action",
                    state_changed=True,
                    public_reply="行动完成。",
                    lock_public_reply=True,
                )
            ),
            side_effect="write",
        )
    )
    events = [
        {
            "event_id": "event-plan",
            "speaker": "村夫",
            "text": "想打你再去和他交谈，我偷偷给他来个偷袭",
        }
    ]
    tentative = {
        "version": "1",
        "events": [
            {
                "event_id": "event-plan",
                "speaker": "村夫",
                "relation": "player",
                "targets": ["loading"],
                "dialogue_act": "proposal",
                "action_commitment": "tentative",
                "responds_to_event_id": "",
                "reason": "向队友讨论可能采用的偷袭方案。",
            }
        ],
    }
    rewritten = json.loads(json.dumps(tentative, ensure_ascii=False))
    rewritten["events"][0].update(
        {
            "relation": "table",
            "dialogue_act": "action_declaration",
            "action_commitment": "committed",
            "reason": "改判为已经执行偷袭。",
        }
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": tentative,
                    "message_kind": "discussion",
                    "audience": "players",
                    "tool_name": "perform_character_action",
                    "arguments": {},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": rewritten,
                    "message_kind": "performed_action",
                    "audience": "table",
                    "tool_name": "perform_character_action",
                    "arguments": {},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "silent",
                    "message_kind": "discussion",
                    "audience": "players",
                },
                ensure_ascii=False,
            ),
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        max_iterations=3,
    )

    outcome = agent.run(
        events[0]["text"],
        recent_context="",
        context=semantic_context(events),
        state_summary={},
    )

    assert executed == []
    assert outcome.target == "silent"
    assert [step.get("protocol_error") for step in outcome.trace[:2]] == [
        "PLAYER_ACTION_NOT_COMMITTED",
        "PLAYER_ACTION_NOT_COMMITTED",
    ]
    assert outcome.trace[1]["message_semantics_model_drift_ignored"] is True
    assert outcome.trace[1]["message_semantics"] == tentative


def test_explicit_gm_answer_still_resolves_rule_window() -> None:
    executed: list[str] = []
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="resolve_rule_window",
            description="resolve",
            handler=lambda _context, _arguments: (
                executed.append("resolved")
                or GMToolReceipt.success(
                    "resolve_rule_window",
                    state_changed=True,
                    public_reply="检定继续。",
                    lock_public_reply=True,
                )
            ),
            side_effect="write",
        )
    )
    events = [{"event_id": "event-roll", "speaker": "村夫", "text": "投"}]
    semantics = {
        "version": "1",
        "events": [
            {
                "event_id": "event-roll",
                "speaker": "村夫",
                "relation": "gm",
                "targets": ["时悠"],
                "dialogue_act": "answer",
                "action_commitment": "answer",
                "responds_to_event_id": "",
                "reason": "回答主持人是否投骰的提问。",
            }
        ],
    }
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "decision": "call_tool",
                    "message_semantics": semantics,
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "tool_name": "resolve_rule_window",
                    "arguments": {},
                },
                ensure_ascii=False,
            )
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=registry)

    outcome = agent.run(
        "投",
        recent_context="时悠：要投吗？",
        context=semantic_context(events),
        state_summary={},
    )

    assert executed == ["resolved"]
    assert outcome.reply == "检定继续。"


class GMToolRegistryTests(unittest.TestCase):
    def test_review_before_confirm_reads_draft_without_forced_confirmation(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_hero_drafts",
                description="读取角色草稿。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_hero_drafts",
                    result={"drafts": [{"hero_name": "伊大石"}]},
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="confirm_hero_draft",
                description="确认角色草稿。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "confirm_hero_draft",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "get_hero_drafts",
                        "arguments": {},
                        "reason": "玩家要先查看草稿。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "伊大石的角色草稿如下。",
                        "reason": "已经读取并展示，等待玩家之后确认。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context(speaker="测试玩家乙")
        context.gate_status = "session_zero"

        outcome = agent.run(
            "提供给我角色草稿，我确认一下好正式建卡",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.reply, "伊大石的角色草稿如下。")
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["get_hero_drafts"],
        )
        self.assertFalse(outcome.state_changed)

    def test_start_adventure_locked_receipt_finishes_in_one_core_iteration(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_adventure",
                description="原子开启第一章与首场。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "start_adventure",
                    result={
                        "required_followup_resolved": True,
                        "required_followup_tools": [],
                        "scene": {"scene_id": "scene-1"},
                    },
                    state_changed=True,
                    public_reply="牢门封印忽然暗了一瞬。你们现在怎么做？",
                    lock_public_reply=True,
                ),
                parameters=(
                    GMToolParameter(
                        "reason",
                        "string",
                        "玩家接受第一章邀请的说明。",
                        required=True,
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "start_adventure",
                        "arguments": {"reason": "玩家接受开章邀请"},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.gate_status = "session_zero"
        context.metadata.update(
            {
                "adventure_opening_flow_mode": "optimized",
                "_gm_chapter_one_invited_ready": True,
                "conversation_anchor": {
                    "anchor_id": "session-zero:chapter-one-invitation",
                    "kind": "chapter_one_invitation",
                    "status": "awaiting_semantic_reply",
                    "question": "时悠已经询问是否现在进入第一章。",
                    "accepted_action": "start_adventure",
                    "interpretation": "结合最近聊天判断短答的含义。",
                    "blocking": False,
                    "player_visible": False,
                },
            }
        )

        outcome = agent.run(
            "嗯",
            recent_context="第零章已经完成，GM刚询问是否进入第一章。",
            context=context,
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "牢门封印忽然暗了一瞬。你们现在怎么做？",
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["start_adventure"],
        )
        self.assertEqual(len(client.calls), 1)
        request = json.loads(client.calls[0]["messages"][1].content)
        anchor = request["request_context"]["conversation_anchor"]
        self.assertEqual(anchor["kind"], "chapter_one_invitation")
        self.assertFalse(anchor["blocking"])

    def test_exact_gate_status_reply_uses_local_grounding(self) -> None:
        class MustNotRun:
            def verify(self, **_kwargs):
                raise AssertionError("精确权威状态句不应调用模型审校。")

        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "第一章已经开始了。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=MustNotRun(),
        )

        outcome = agent.run(
            "现在已经进入第一章了吗？",
            recent_context="",
            context=execution_context(),
            state_summary={
                "processes": {"session": {"gate_status": "adventure"}}
            },
        )

        self.assertEqual(outcome.reply, "第一章已经开始了。")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            outcome.trace[0]["reply_grounding"],
            {
                "valid": True,
                "category": "local_authoritative_exact",
                "source": "gate_status",
                "unsupported_claims": [],
            },
        )

    def test_gate_status_reply_with_extra_claim_falls_back_to_model_review(
        self,
    ) -> None:
        class CapturingVerifier:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def verify(self, **kwargs):
                self.calls.append(dict(kwargs))
                return SimpleNamespace(
                    valid=True,
                    category="grounded",
                    unsupported_claims=(),
                    correction_hint="",
                )

        verifier = CapturingVerifier()
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "第一章已经开始了，守卫也已经倒下。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=verifier,
        )

        outcome = agent.run(
            "现在已经进入第一章了吗？",
            recent_context="",
            context=execution_context(),
            state_summary={
                "processes": {"session": {"gate_status": "adventure"}}
            },
        )

        self.assertEqual(
            outcome.reply,
            "第一章已经开始了，守卫也已经倒下。",
        )
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(
            verifier.calls[0]["proposed_reply"],
            "第一章已经开始了，守卫也已经倒下。",
        )

    def test_whitelisted_state_claim_uses_local_grounding(self) -> None:
        class MustNotRun:
            def verify(self, **_kwargs):
                raise AssertionError("精确结构化状态引用不应调用模型审校。")

        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "当前场景位于【卡里巴村监狱】。",
                        "claims": [
                            {
                                "type": "state_reference",
                                "path": "scene.location",
                                "expected": "卡里巴村监狱",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=MustNotRun(),
        )

        outcome = agent.run(
            "我们现在在哪里？",
            recent_context="",
            context=execution_context(),
            state_summary={
                "scene": {"location": "卡里巴村监狱"},
                "processes": {"session": {"gate_status": "adventure"}},
            },
        )

        self.assertEqual(outcome.reply, "当前场景位于【卡里巴村监狱】。")
        self.assertEqual(
            outcome.trace[0]["reply_grounding"]["source"],
            "state_reference:scene.location",
        )

    def test_state_claim_mismatch_falls_back_to_model_review(self) -> None:
        class CapturingVerifier:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def verify(self, **kwargs):
                self.calls.append(dict(kwargs))
                return SimpleNamespace(
                    valid=True,
                    category="grounded",
                    unsupported_claims=(),
                    correction_hint="",
                )

        verifier = CapturingVerifier()
        decision = {
            "decision": "final",
            "message_kind": "gm_request",
            "claims": [
                {
                    "type": "state_reference",
                    "path": "scene.location",
                    "expected": "错误地点",
                }
            ],
        }
        source = LLMGMToolAgent._locally_proven_exact_reply(
            reply="当前场景位于【错误地点】。",
            decision=decision,
            observed_state={"scene": {"location": "卡里巴村监狱"}},
            receipts=[],
            risk_tier="observe",
        )

        self.assertEqual(source, "")

    def test_exact_success_receipt_public_reply_uses_local_grounding(self) -> None:
        receipt = GMToolReceipt.success(
            "record_note",
            state_changed=True,
            public_reply="已经记下这项约定。",
        )

        source = LLMGMToolAgent._locally_proven_exact_reply(
            reply="已经记下这项约定。",
            decision={
                "decision": "final",
                "message_kind": "state_contribution",
            },
            observed_state={},
            receipts=[receipt],
            risk_tier="state_change",
        )
        altered = LLMGMToolAgent._locally_proven_exact_reply(
            reply="已经记下这项约定，守卫也同意了。",
            decision={
                "decision": "final",
                "message_kind": "state_contribution",
            },
            observed_state={},
            receipts=[receipt],
            risk_tier="state_change",
        )

        self.assertEqual(source, "successful_receipt_public_reply")
        self.assertEqual(altered, "")

    def test_initial_agent_request_marks_core_and_phase_cache_layers(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "在。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(core_client, model="fake", registry=GMToolRegistry())

        outcome = agent.run(
            "时悠，在吗？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "在。")
        messages = core_client.calls[0]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].cache_family, "gm-agent")
        self.assertTrue(messages[0].cache_breakpoint)
        self.assertEqual(len(messages[0].cache_breakpoint_offsets), 2)
        self.assertLess(
            messages[0].cache_breakpoint_offsets[0],
            messages[0].cache_breakpoint_offsets[1],
        )
        user_message = messages[-1]
        self.assertEqual(user_message.role, "user")
        self.assertTrue(user_message.cache_breakpoint)
        self.assertEqual(len(user_message.cache_breakpoint_offsets), 2)
        tool_boundary, state_boundary = user_message.cache_breakpoint_offsets
        self.assertLess(tool_boundary, state_boundary)
        self.assertIn('"available_tools"', user_message.content[:tool_boundary])
        self.assertIn(
            '"current_state_summary"',
            user_message.content[tool_boundary:state_boundary],
        )
        self.assertNotIn('"current_message"', user_message.content[:state_boundary])
        request = json.loads(user_message.content)
        self.assertNotIn("runtime_feedback", request)
        self.assertEqual(len(core_client.calls), 1)

    def test_state_refresh_failure_is_bounded_feedback_in_the_same_request(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "我会沿用当前可确认的状态。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())

        outcome = agent.run(
            "时悠，现在是什么情况？",
            recent_context="",
            context=execution_context(),
            state_summary={"known_state": "保留的权威状态"},
            state_summary_provider=lambda: (_ for _ in ()).throw(
                RuntimeError("SECRET_STATE_PROVIDER_DETAIL")
            ),
        )

        self.assertEqual(outcome.reply, "我会沿用当前可确认的状态。")
        self.assertEqual(len(client.calls), 1)
        request = json.loads(client.calls[0]["messages"][-1].content)
        self.assertEqual(
            request["current_state_summary"]["known_state"],
            "保留的权威状态",
        )
        issue = request["runtime_feedback"]["issues"][0]
        self.assertEqual(issue["code"], "STATE_REFRESH_FAILED")
        self.assertFalse(issue["retryable"])
        serialized_request = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("SECRET_STATE_PROVIDER_DETAIL", serialized_request)
        self.assertNotIn("SECRET_STATE_PROVIDER_DETAIL", json.dumps(outcome.trace))

    def test_recovered_provider_feedback_is_one_shot_and_never_adds_a_loop(self) -> None:
        invalid_decision = json.dumps(
            {
                "decision": "call_tools",
                "calls": [
                    {"tool_name": "missing_tool", "arguments": {}},
                    {"arguments": {}},
                ],
            },
            ensure_ascii=False,
        )
        client = DiagnosticScriptedClient(
            [
                invalid_decision,
                invalid_decision,
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "已经收束。",
                    },
                    ensure_ascii=False,
                ),
            ],
            [
                {
                    "recovery_codes": ["PROVIDER_RECOVERED"],
                    "attempt_count": 2,
                },
                {},
                {},
            ],
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            max_iterations=3,
            parse_retries=0,
        )

        outcome = agent.run(
            "时悠，处理这件事。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "已经收束。")
        self.assertEqual(len(client.calls), 3)
        first = json.loads(client.calls[0]["messages"][-1].content)
        second = json.loads(client.calls[1]["messages"][-1].content)
        third = json.loads(client.calls[2]["messages"][-1].content)
        self.assertNotIn("runtime_feedback", first)
        self.assertEqual(
            [item["code"] for item in second["runtime_feedback"]["issues"]],
            ["PROVIDER_RECOVERED"],
        )
        self.assertFalse(second["runtime_feedback"]["issues"][0]["retryable"])
        # The recovered-provider issue is one-shot.  The last permitted
        # request still receives its independently derived near-limit budget.
        self.assertEqual(third["runtime_feedback"]["issues"], [])
        self.assertEqual(
            third["runtime_feedback"]["budget"]["status"],
            "near_limit",
        )

    def test_mixed_message_continues_after_rule_window_to_answer_question(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_rule_window",
                    result={
                        "window_id": "window-1",
                        "action_type": "TriggerOpportunity",
                        "effect": "失物",
                    },
                    state_changed=True,
                    public_reply="机会【失物】：牢门已被岁月腐蚀，轻轻一推就能打开。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": True,
                        "audience": "gm",
                        "tool_name": "resolve_rule_window",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "",
                        "resolution_reply": (
                            "锈透的锁舌发出一声脆响，牢门随即向外晃开一道缝。"
                            "嘿，它比看守先投降了。"
                        ),
                        "independent_reply": (
                            "你和艾丽妮在相邻的两间石牢，不在同一间。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我选择机会：失物，牢门已经腐蚀。顺便问我和艾丽妮在同一间吗？",
            recent_context="",
            context=execution_context(),
            state_summary={"current_scene": {"positions": {"A": "石牢1", "B": "石牢2"}}},
        )

        self.assertEqual(len(client.calls), 2)
        self.assertIn("锁舌发出一声脆响", outcome.reply)
        self.assertNotIn("机会【失物】", outcome.reply)
        self.assertNotIn("牢门已被岁月腐蚀，轻轻一推就能打开", outcome.reply)
        self.assertIn("相邻的两间石牢", outcome.reply)
        self.assertEqual(
            outcome.reply_parts,
            [
                "你和艾丽妮在相邻的两间石牢，不在同一间。",
                "锈透的锁舌发出一声脆响，牢门随即向外晃开一道缝。"
                "嘿，它比看守先投降了。",
            ],
        )
        self.assertTrue(outcome.reply.startswith("你和艾丽妮在相邻"))
        self.assertTrue(outcome.state_changed)
        self.assertFalse(outcome.receipts[0].lock_public_reply)
        self.assertTrue(
            outcome.receipts[0].result["natural_resolution_pending"]
        )
        self.assertTrue(
            outcome.receipts[0].result["mixed_message_followup_pending"]
        )

    def test_mixed_message_retries_when_model_omits_independent_reply(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_rule_window",
                    result={
                        "window_id": "window-1",
                        "action_type": "TriggerOpportunity",
                        "effect": "失物",
                    },
                    state_changed=True,
                    public_reply="机会【失物】：牢门已经可以推开。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "mixed",
                        "has_independent_followup": True,
                        "audience": "gm",
                        "tool_name": "resolve_rule_window",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "机会已经结算。",
                        "resolution_reply": "锈蚀的门轴轻轻一晃，门缝松开了。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "",
                        "resolution_reply": "锈蚀的门轴轻轻一晃，门缝松开了。",
                        "independent_reply": "你们在相邻牢房，不是同一间。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我选择失物让牢门腐蚀。顺便问我们在同一间吗？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 3)
        self.assertIn("门缝松开了", outcome.reply)
        self.assertNotIn("机会【失物】", outcome.reply)
        self.assertIn("相邻牢房", outcome.reply)
        self.assertEqual(
            outcome.reply_parts,
            [
                "你们在相邻牢房，不是同一间。",
                "锈蚀的门轴轻轻一晃，门缝松开了。",
            ],
        )
        self.assertTrue(
            any(
                step.get("protocol_error")
                == "INDEPENDENT_FOLLOWUP_REPLY_REQUIRED"
                for step in outcome.trace
            )
        )

    def test_narrative_opportunity_uses_natural_resolution_without_followup(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_rule_window",
                    result={
                        "window_id": "window-1",
                        "action_type": "TriggerOpportunity",
                        "effect": "失物",
                    },
                    state_changed=True,
                    public_reply="机会【失物】：牢门已经腐蚀，可以推开。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "gm",
                        "tool_name": "resolve_rule_window",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "",
                        "resolution_reply": (
                            "门轴抖下一层红锈，锁舌随即松脱，牢门慢慢敞开了。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我把这次机会用作失物，让牢门腐蚀到能够推开。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            outcome.reply,
            "门轴抖下一层红锈，锁舌随即松脱，牢门慢慢敞开了。",
        )
        self.assertNotIn("机会【失物】", outcome.reply)

    def test_gm_owned_narrative_opportunity_also_uses_natural_resolution(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="处理GM的大失败机会。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_gm_opportunity",
                    result={
                        "window_id": "window-gm-1",
                        "action_type": "TriggerOpportunity",
                        "opportunity_effect": "转折",
                    },
                    state_changed=True,
                    public_reply="机会【转折】：巡夜人突然出现在楼梯口。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "gm",
                        "tool_name": "resolve_gm_opportunity",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "resolution_reply": "楼梯上传来急促的鞋跟声，巡夜人的灯火猛地切进牢区。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅的大失败需要由GM处理机会。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 2)
        self.assertIn("鞋跟声", outcome.reply)
        self.assertNotIn("机会【转折】", outcome.reply)
        self.assertTrue(outcome.receipts[0].result["natural_resolution_pending"])

    def test_natural_resolution_uses_authoritative_fallback_after_one_omission(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="处理GM的大失败机会。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_gm_opportunity",
                    result={
                        "window_id": "window-gm-2",
                        "action_type": "TriggerOpportunity",
                        "opportunity_effect": "情报",
                    },
                    state_changed=True,
                    public_reply=(
                        "机会【情报】：提灯照出铜索通往洗衣槽维护口。"
                    ),
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "gm",
                        "tool_name": "resolve_gm_opportunity",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "短廊看守的大失败由GM处理机会。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(outcome.reply, "提灯照出铜索通往洗衣槽维护口。")
        self.assertNotIn("机会【情报】", outcome.reply)
        self.assertTrue(
            any(
                step.get("natural_resolution_fallback_used") is True
                for step in outcome.trace
            )
        )

    def test_natural_resolution_retries_when_it_restates_player(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_rule_window",
                    result={
                        "window_id": "window-1",
                        "action_type": "TriggerOpportunity",
                        "effect": "失物",
                    },
                    state_changed=True,
                    public_reply="机会【失物】：牢门已经被岁月腐蚀，只要轻轻一推就能打开。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "gm",
                        "tool_name": "resolve_rule_window",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "resolution_reply": (
                            "牢门已经被岁月腐蚀，只要轻轻一推就能打开。"
                        ),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "resolution_reply": (
                            "锈蚀的锁舌啪地掉在石地上，门扇自己晃开了一掌宽。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我选择机会：失物，牢门已经被岁月腐蚀，只要轻轻一推就能打开。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 3)
        self.assertIn("锁舌啪地掉在石地上", outcome.reply)
        self.assertTrue(
            any(
                step.get("protocol_error") == "RESOLUTION_REPLY_RESTATES_PLAYER"
                for step in outcome.trace
            )
        )
        retry_request = json.loads(client.calls[2]["messages"][-1].content)
        correction_hint = retry_request["history"][-1]["protocol_error"][
            "correction_hint"
        ]
        self.assertIn("成功回执支持的细节", correction_hint)
        self.assertIn("最小陈述", correction_hint)
        self.assertNotIn("新的声音", correction_hint)
        self.assertNotIn("新的动作", correction_hint)

    def test_mechanical_opportunity_keeps_exact_rules_result(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "resolve_rule_window",
                    result={
                        "window_id": "window-1",
                        "action_type": "TriggerOpportunity",
                        "effect": "优势",
                    },
                    state_changed=True,
                    public_reply="机会【优势】：伊莉雅的下一次相关检定获得 +4 修正。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "gm",
                        "tool_name": "resolve_rule_window",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我把机会用作优势。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            outcome.reply,
            "机会【优势】：伊莉雅的下一次相关检定获得 +4 修正。",
        )
        self.assertNotIn(
            "natural_resolution_pending",
            outcome.receipts[0].result,
        )

    def test_repeated_rule_action_rejection_stops_after_three_attempts(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="resolve_rule_window",
                description="处理待决规则窗口。",
                handler=lambda _context, _arguments: GMToolReceipt.failure(
                    "resolve_rule_window",
                    "RULE_ACTION_REJECTED",
                    "机会参数不符合规则。",
                    "修正机会参数后重试。",
                ),
                side_effect="write",
            )
        )
        repeated_call = json.dumps(
            {
                "decision": "call_tool",
                "message_kind": "rule_choice",
                "audience": "table",
                "tool_name": "resolve_rule_window",
                "arguments": {},
            },
            ensure_ascii=False,
        )
        client = ScriptedClient([repeated_call, repeated_call, repeated_call])
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我选择这个机会。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 3)
        self.assertIn("机会参数不符合规则", outcome.reply)
        self.assertIn("待决选择仍然保留", outcome.reply)
        self.assertFalse(outcome.state_changed)

    def test_post_tool_request_uses_its_own_phase_cache_family(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_state",
                description="读取当前状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_state",
                    result={"status": "ok"},
                ),
                side_effect="read",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "inspect_state",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "当前状态正常。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        persona = "# 完整GM人格\n工具之后仍用同一个公开声音。"
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            gm_personality_prompt=persona,
        )

        outcome = agent.run(
            "看看当前状态。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "当前状态正常。")
        self.assertEqual(core_client.calls[0]["messages"][0].cache_family, "gm-agent")
        post_message = core_client.calls[1]["messages"][0]
        self.assertEqual(post_message.cache_family, "gm-agent")
        self.assertEqual(post_message.content.count(persona), 1)
        self.assertIn("工具事务收尾层", post_message.content)
        self.assertNotIn("当前阶段：冒险场景", post_message.content)
        self.assertEqual(len(post_message.cache_breakpoint_offsets), 2)
        self.assertLess(
            post_message.cache_breakpoint_offsets[0],
            post_message.cache_breakpoint_offsets[1],
        )
        post_user_message = core_client.calls[1]["messages"][1]
        self.assertTrue(post_user_message.cache_breakpoint)
        self.assertEqual(len(post_user_message.cache_breakpoint_offsets), 2)

    def test_tool_prefix_survives_state_changes_while_state_prefix_does_not(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_state",
                description="读取当前状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_state",
                    result={"status": "ok"},
                ),
            )
        )
        agent = LLMGMToolAgent(ScriptedClient([]), model="fake", registry=registry)
        context = execution_context()

        first = agent._build_decision_messages(
            current_message="第一条消息",
            recent_context="",
            context=context,
            observed_state={"clock": 1},
            receipts=[],
            history=[],
        )[1]
        second = agent._build_decision_messages(
            current_message="第二条消息",
            recent_context="",
            context=context,
            observed_state={"clock": 2},
            receipts=[],
            history=[],
        )[1]

        first_tool_end, first_state_end = first.cache_breakpoint_offsets
        second_tool_end, second_state_end = second.cache_breakpoint_offsets
        self.assertEqual(
            first.content[:first_tool_end],
            second.content[:second_tool_end],
        )
        self.assertNotEqual(
            first.content[:first_state_end],
            second.content[:second_state_end],
        )

    def test_actionable_message_kind_cannot_finish_silent(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="记录玩家已经执行的普通场景行动。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_in_scene_action",
                    state_changed=True,
                    public_reply="苍祈点亮了蓝芯守望灯。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "performed_action",
                        "audience": "players",
                        "reason": "苍祈已经点亮守望灯。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "table",
                        "tool_name": "perform_in_scene_action",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
        )
        context = GMToolExecutionContext(
            campaign_id="agent-test",
            session_id="s1",
            channel_id="group-1",
            speaker="澄砚",
            gate_status="adventure",
            directly_addressed=False,
        )

        outcome = agent.run(
            "苍祈点亮手中的蓝芯守望灯，向守望会发出示警。",
            recent_context="队伍正在讨论是否暴露位置。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.reply, "苍祈点亮了蓝芯守望灯。")
        self.assertTrue(outcome.state_changed)
        self.assertEqual(len(core_client.calls), 2)
        self.assertTrue(
            any(
                item.get("protocol_error") == "ACTIONABLE_MESSAGE_CANNOT_BE_SILENCED"
                for item in outcome.trace
            )
        )

    def test_discussion_message_kind_can_stay_silent(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "玩家在征求队友意见，尚未行动。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
        )
        context = GMToolExecutionContext(
            campaign_id="agent-test",
            session_id="s1",
            channel_id="group-1",
            speaker="南星",
            gate_status="adventure",
            directly_addressed=False,
        )

        outcome = agent.run(
            "蓝芯守望灯要不要先别点，免得暴露位置？",
            recent_context="队伍取得了一盏蓝芯守望灯。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(core_client.calls), 1)

    def test_player_discussion_cannot_be_used_as_npc_turn_time_slice(self) -> None:
        npc_calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="run_current_npc_turn",
                description="执行当前NPC回合。",
                handler=lambda _context, arguments: (
                    npc_calls.append(dict(arguments))
                    or GMToolReceipt.success(
                        "run_current_npc_turn",
                        result={"actor": "监察官艾蕾娜"},
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "expected_actor",
                        "string",
                        "预期当前NPC。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "discussion",
                        "audience": "players",
                        "tool_name": "run_current_npc_turn",
                        "arguments": {"expected_actor": "监察官艾蕾娜"},
                        "reason": "玩家在讨论，但当前恰好轮到NPC。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "NPC回合应由独立主动节拍触发。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context(speaker="澄砚")
        context.directly_addressed = False

        outcome = agent.run(
            "我有点担心先开旧路会不会让守望会背锅，你们怎么看？",
            recent_context="当前冲突行动者是监察官艾蕾娜。",
            context=context,
            state_summary={"conflict": {"current_actor": "监察官艾蕾娜"}},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(npc_calls, [])
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            any(
                item.get("protocol_error")
                == "PLAYER_DISCUSSION_CANNOT_DRIVE_NPC_TURN"
                for item in outcome.trace
            )
        )

    def test_semantic_silence_review_recovers_table_fact_clarification(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "误判为角色内讨论。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "刚才没人提到庄园。灰耳说的是地下铁匣和蓝灰色冷光。",
                        "reason": "这是对公开桌面记录的事实核对。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "table_fact_clarification",
                        "reason": "这是对最近公开聊天内容的记忆核对。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": True,
                        "request_fulfilled": True,
                        "category": "grounded",
                        "unsupported_claims": [],
                        "correction_hint": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = GMToolExecutionContext(
            campaign_id="agent-test",
            session_id="s1",
            channel_id="group-1",
            speaker="测试玩家甲",
            gate_status="adventure",
            directly_addressed=False,
        )

        outcome = agent.run(
            "诺艾尔皱了皱眉：等等，刚才是谁提到了庄园？我没听清。",
            recent_context="灰耳说，两名看守往地下搬过蒙油布铁匣。",
            context=context,
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "刚才没人提到庄园。灰耳说的是地下铁匣和蓝灰色冷光。",
        )
        self.assertEqual(len(core_client.calls), 2)
        self.assertEqual(len(review_client.calls), 2)
        self.assertEqual(
            review_client.calls[0]["operation"],
            "gm_silence_responsibility_verification",
        )
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "SILENCE_REVIEW_REQUIRES_GM_REPLY",
        )
        self.assertTrue(
            any(
                step.get("silence_responsibility", {}).get("category")
                == "table_fact_clarification"
                for step in outcome.trace
            )
        )

    def test_player_discussion_cannot_trigger_npc_response_tool(self) -> None:
        executed: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="让NPC回应实际发生的互动。",
                handler=lambda _context, arguments: (
                    executed.append(dict(arguments))
                    or GMToolReceipt.success(
                        "decide_npc_response",
                        state_changed=True,
                        public_fallback_reply="旅人回答了旧路的秘密。",
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "instruction",
                        "string",
                        "NPC需要回应的实际互动。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "npc_or_world_interaction",
                        "audience": "gm",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "instruction": "让旅人立刻告诉众人旧路尽头有钟声。"
                        },
                        "reason": "误把玩家之间的提议当成已经发生的询问。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "玩家只是在队内建议下一步，尚未和旅人互动。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "valid": False,
                        "category": "gm_must_repair",
                        "unsupported_claims": [
                            "玩家尚未询问旅人，却提议让旅人立即回答。"
                        ],
                        "correction_hint": (
                            "取消decide_npc_response；这只是玩家之间的建议，"
                            "没有其他主持职责时选择silent。"
                        ),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "request_fulfilled": True,
                        "category": "player_discussion",
                        "reason": "玩家在向队友建议下一步，没有要求NPC或GM回应。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context(speaker="南星")
        context.directly_addressed = False

        outcome = agent.run(
            "那位旅人一直在低语名字，我们不如先听听他到底在说什么？",
            recent_context="旅人站在廊柱旁低语；守望会会长守着旧路闸门。",
            context=context,
            state_summary={},
        )

        self.assertEqual(executed, [])
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(core_client.calls), 2)
        self.assertEqual(len(review_client.calls), 2)
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
        )
        self.assertTrue(
            any(
                step.get("tool_proposal_grounding", [{}])[0].get("category")
                == "gm_must_repair"
                for step in outcome.trace
                if step.get("tool_proposal_grounding")
            )
        )
        system_prompt = core_client.calls[0]["messages"][0].content
        self.assertIn("对最近公开聊天作记忆核对或桌面事实澄清", system_prompt)

    def test_unsupported_npc_knowledge_retry_is_directed_to_admit_unknown(self) -> None:
        executed: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="让NPC回应实际发生的互动。",
                handler=lambda _context, arguments: (
                    executed.append(dict(arguments))
                    or GMToolReceipt(
                        tool_name="decide_npc_response",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="卡尔摇头：这个人我没有印象。信里写了什么？",
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "instruction",
                        "string",
                        "NPC如何回应。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "npc_or_world_interaction",
                        "audience": "gm",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "instruction": "卡尔说自己见过这个瘸腿老人往西走。"
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "npc_or_world_interaction",
                        "audience": "gm",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "instruction": "admit_unknown，只询问玩家已经提到的信。"
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "valid": False,
                        "category": "npc_knowledge_unsupported",
                        "unsupported_claims": ["卡尔见过老科特并知道其去向。"],
                        "correction_hint": "卡尔的知识边界没有这项见闻。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": True,
                        "category": "grounded",
                        "unsupported_claims": [],
                        "correction_hint": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )

        outcome = agent.run(
            "我师傅叫老科特，是个右腿有点瘸的退役伙夫，他留下一封信就没回来。",
            recent_context="卡尔正在听伊大石说明师傅的情况。",
            context=execution_context(speaker="测试玩家乙"),
            state_summary={
                "npcs": {
                    "present_npcs": [
                        {
                            "name": "卡尔",
                            "knowledge_scope": ["边境巡逻", "灰烬之潮迹象"],
                        }
                    ]
                }
            },
        )

        self.assertEqual(
            executed,
            [{"instruction": "admit_unknown，只询问玩家已经提到的信。"}],
        )
        self.assertEqual(
            outcome.reply,
            "卡尔摇头：这个人我没有印象。信里写了什么？",
        )
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        correction = retry_request["history"][-1]["protocol_error"]
        self.assertEqual(
            correction["error_code"],
            "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
        )
        self.assertIn("用fact_effects明确分类", correction["correction_hint"])
        self.assertIn("speech_act=admit_unknown", correction["correction_hint"])
        self.assertIn("不得只换一种说法再次提交", correction["correction_hint"])

    def test_repeated_gm_repair_for_npc_knowledge_forces_structured_safe_exit(
        self,
    ) -> None:
        executed: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="让NPC回应实际发生的互动。",
                handler=lambda _context, arguments: (
                    executed.append(dict(arguments))
                    or GMToolReceipt(
                        tool_name="decide_npc_response",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply=(
                            "卡尔摇了摇头：这个名字我没听过。"
                            "那封信里还留下了什么？"
                        ),
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "speech_act",
                        "string",
                        "NPC如何回应。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        invented = {
            "decision": "call_tool",
            "message_kind": "npc_or_world_interaction",
            "audience": "gm",
            "tool_name": "decide_npc_response",
            "arguments": {"speech_act": "answer"},
        }
        core_client = ScriptedClient(
            [
                json.dumps(invented, ensure_ascii=False),
                json.dumps(invented, ensure_ascii=False),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "npc_or_world_interaction",
                        "audience": "gm",
                        "tool_name": "decide_npc_response",
                        "arguments": {"speech_act": "admit_unknown"},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "valid": False,
                        "category": "gm_must_repair",
                        "repair_mode": "npc_fact_or_nonclaim",
                        "unsupported_claims": [
                            "卡尔声称三天前见过符合描述的老人并知道其去向。"
                        ],
                        "correction_hint": "这项新见闻没有分类。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": False,
                        "category": "gm_must_repair",
                        "repair_mode": "npc_fact_or_nonclaim",
                        "unsupported_claims": [
                            "卡尔再次声称见过老人并知道其去向。"
                        ],
                        "correction_hint": "不要改写同一条未分类情报。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": True,
                        "category": "grounded",
                        "repair_mode": "ordinary",
                        "unsupported_claims": [],
                        "correction_hint": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )

        outcome = agent.run(
            (
                "我师傅叫老科特，他只是个退役伙夫，灰白头发，"
                "右腿有点瘸，留下一封信就再没回来。"
            ),
            recent_context="卡尔正在听伊大石说明师傅的情况。",
            context=execution_context(speaker="测试玩家乙"),
            state_summary={
                "npcs": {
                    "present_npcs": [
                        {
                            "name": "卡尔",
                            "knowledge_scope": ["边境巡逻", "灰烬之潮迹象"],
                        }
                    ]
                }
            },
        )

        self.assertEqual(executed, [{"speech_act": "admit_unknown"}])
        self.assertIn("这个名字我没听过", outcome.reply)
        first_retry = json.loads(core_client.calls[1]["messages"][-1].content)
        first_contract = first_retry["history"][-1]["protocol_error"][
            "npc_response_repair_contract"
        ]
        self.assertEqual(first_contract["rejection_count"], 1)
        self.assertFalse(first_contract["repeated_rejection_requires_safe_exit"])
        second_retry = json.loads(core_client.calls[2]["messages"][-1].content)
        second_contract = second_retry["history"][-1]["protocol_error"][
            "npc_response_repair_contract"
        ]
        self.assertEqual(second_contract["rejection_count"], 2)
        self.assertTrue(second_contract["repeated_rejection_requires_safe_exit"])
        self.assertEqual(
            second_contract["valid_nonclaim_path"]["allowed_speech_acts"],
            ["admit_unknown", "refuse", "deflect", "new_gate"],
        )

    def test_semantic_silence_review_recovers_followup_to_gm_permission_refusal(
        self,
    ) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "误判成玩家之间的确认。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "需要loading本人明确授权；你代他说同意还不能修改他的角色草稿。",
                        "reason": "这句话承接时悠刚才的权限拒绝，必须继续回应。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "management_request",
                        "reason": "当前短句直接回应了时悠上一句权限说明。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": True,
                        "request_fulfilled": True,
                        "category": "grounded",
                        "unsupported_claims": [],
                        "correction_hint": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context(speaker="测试玩家甲")
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "他同意了",
            recent_context="时悠: 这张角色草稿只能由所属玩家本人修改。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("loading本人明确授权", outcome.reply)
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "SILENCE_REVIEW_REQUIRES_GM_REPLY",
        )
        self.assertIn(
            "最近一条公开发言是时悠的提问、拒绝或说明",
            core_client.calls[0]["messages"][0].content,
        )

    def test_semantic_silence_review_preserves_player_only_discussion(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "玩家正在彼此商量分工。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "player_discussion",
                        "reason": "问题明确交给其他玩家决定。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "谁方便盯外面，谁继续和会长谈？",
            recent_context="队伍正在讨论下一步分工。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(core_client.calls), 1)
        self.assertEqual(len(review_client.calls), 1)

    def test_semantic_silence_review_repairs_contradictory_core_audience(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "gm",
                        "reason": (
                            "玩家只是在赞同另一名玩家的角色画面，"
                            "没有要求主持人回应。"
                        ),
                    },
                    ensure_ascii=False,
                )
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "request_fulfilled": True,
                        "category": "player_discussion",
                        "reason": (
                            "这是玩家对另一名玩家的评价，"
                            "没有主持请求或外界行动。"
                        ),
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context(speaker="白河")
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "这个搭配挺好。有人帮忙隔开危险，修灯的人就能专心。",
            recent_context=(
                "阿凛: 伊莉雅会举盾挡在故障船灯和修理匠之间。"
            ),
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(core_client.calls), 1)
        self.assertEqual(len(review_client.calls), 1)
        normalization = outcome.trace[0]["semantic_normalization"]
        self.assertEqual(normalization["original"]["audience"], "gm")
        self.assertEqual(
            normalization["effective"],
            {"message_kind": "discussion", "audience": "players"},
        )

    def test_table_fact_reply_retries_when_it_leaks_unintroduced_npc_name(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "刚才没人提到庄园。赫德说的是封印回流。",
                        "reason": "依据公开聊天澄清。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "刚才没人提到庄园。隔壁牢房那个人说的是封印回流。",
                        "reason": "只沿用公开称呼。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "valid": False,
                        "category": "private_fact_disclosure",
                        "unsupported_claims": ["匿名说话者名叫赫德"],
                        "correction_hint": "沿用最近公开聊天中的匿名描述。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "valid": True,
                        "request_fulfilled": True,
                        "category": "grounded",
                        "unsupported_claims": [],
                        "correction_hint": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = GMToolExecutionContext(
            campaign_id="agent-test",
            session_id="s1",
            channel_id="group-1",
            speaker="测试玩家甲",
            gate_status="adventure",
            directly_addressed=False,
        )

        outcome = agent.run(
            "诺艾尔皱眉：刚才是谁提到了庄园？",
            recent_context=(
                "隔壁牢房那个人压低声音说：封印的力量正在往下拽。"
            ),
            context=context,
            state_summary={
                "private_npc_profiles": {
                    "赫德": {"publicly_introduced": False}
                }
            },
        )

        self.assertEqual(
            outcome.reply,
            "刚才没人提到庄园。隔壁牢房那个人说的是封印回流。",
        )
        self.assertEqual(len(core_client.calls), 2)
        self.assertEqual(len(review_client.calls), 2)
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "PUBLIC_REPLY_NOT_GROUNDED",
        )
        self.assertEqual(
            outcome.trace[0]["reply_grounding"]["category"],
            "private_fact_disclosure",
        )

    def test_provider_failure_rolls_back_incomplete_cross_iteration_transaction(
        self,
    ) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="prepare_transition",
                description="prepare",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_transition",
                        ok=True,
                        result={
                            "required_followup_tools": ["finish_transition"],
                        },
                        state_changed=True,
                        public_fallback_reply="前置步骤完成。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="finish_transition",
                description="finish",
                handler=lambda _context, _arguments: (
                    state.append("finished")
                    or GMToolReceipt.success(
                        "finish_transition",
                        state_changed=True,
                        public_reply="转场完成。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "prepare_transition",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 带我们转场",
            recent_context="",
            context=execution_context(),
            state_summary={"marker": "before"},
        )

        self.assertEqual(state, [])
        self.assertFalse(outcome.state_changed)
        self.assertEqual(
            outcome.mode,
            "gm_agent_message_transaction_rolled_back",
        )
        self.assertIn("没有留下改动", outcome.reply)
        self.assertTrue(outcome.receipts[0].result["rolled_back"])
        self.assertTrue(
            any("message_transaction_rollback" in item for item in outcome.trace)
        )

    def test_batch_reply_cannot_finish_before_required_followup_tool(self) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="prepare_transition",
                description="prepare",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_transition",
                        ok=True,
                        result={
                            "required_followup_tools": ["finish_transition"],
                            "required_followup_calls": [],
                        },
                        state_changed=True,
                        public_fallback_reply="第一幕定下来了。",
                        lock_public_reply=False,
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="finish_transition",
                description="finish",
                handler=lambda _context, _arguments: (
                    state.append("finished")
                    or GMToolReceipt.success(
                        "finish_transition",
                        state_changed=True,
                        public_reply="第一章可以开始了。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "message_kind": "state_contribution",
                        "audience": "table",
                        "calls": [
                            {
                                "tool_name": "prepare_transition",
                                "arguments": {},
                            }
                        ],
                        "terminal_decision": "final",
                        "reply": "第一幕定下来了。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tools",
                        "message_kind": "state_contribution",
                        "audience": "table",
                        "calls": [
                            {
                                "tool_name": "finish_transition",
                                "arguments": {},
                            }
                        ],
                        "terminal_decision": "final",
                        "reply": "第一章可以开始了。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我们确认第一幕。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, ["prepared", "finished"])
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "第一章可以开始了。")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            outcome.trace[0]["post_tool_required_followup_pending"],
            ["finish_transition"],
        )

    def test_semantically_addressed_request_is_not_silenced_after_later_failure(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_status",
                description="读取当前状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_status",
                    result={"status": "ok"},
                ),
                side_effect="read",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "inspect_status",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "时悠，看看现在怎么样。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("模型", outcome.reply)
        self.assertIn("没有结算", outcome.reply)
        self.assertIn("原样重发", outcome.reply)
        self.assertTrue(
            any(
                item.get("semantic_gm_addressed") is True
                for item in outcome.trace
                if isinstance(item, dict)
            )
        )

    def test_terminal_read_receipt_finishes_without_second_model_call(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_status",
                description="inspect",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_status",
                    result={
                        "active_alerts": [],
                        "terminal_public_result": True,
                    },
                    public_reply="监督检查完成：当前没有活动告警。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "inspect_status",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 检查监督状态",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(outcome.mode, "gm_agent_tool")
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "监督检查完成：当前没有活动告警。")
        self.assertFalse(outcome.state_changed)
        self.assertFalse(outcome.error)

    def test_singleton_batch_terminal_read_finishes_without_second_model_call(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="inspect_status",
                description="inspect",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "inspect_status",
                    result={
                        "active_alerts": [],
                        "terminal_public_result": True,
                    },
                    public_reply="监督检查完成：当前没有活动告警。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "calls": [
                            {"tool_name": "inspect_status", "arguments": {}}
                        ],
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 检查监督状态",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(outcome.mode, "gm_agent_tool")
        self.assertEqual(outcome.reply, "监督检查完成：当前没有活动告警。")

    def test_terminal_read_cannot_swallow_independent_map_request(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description="读取第零章缺项。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_session_zero_readiness",
                    result={
                        "ready": False,
                        "missing": ["世界奥秘"],
                        "terminal_public_result": True,
                    },
                    public_reply="第零章还缺世界奥秘。",
                    lock_public_reply=True,
                ),
                side_effect="read",
            )
        )
        registry.register(
            GMToolDefinition(
                name="generate_world_map_preview",
                description="生成世界地图；缺少名称时询问玩家。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "generate_world_map_preview",
                    result={
                        "status": "needs_name",
                        "required_field": "continent_name",
                    },
                    public_reply="这张地图还没有名字。你想叫它什么？",
                    lock_public_reply=True,
                ),
                side_effect="read",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "tool_name": "get_session_zero_readiness",
                        "arguments": {},
                        "reason": "先检查是否能够进入第一章。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": True,
                        "audience": "gm",
                        "tool_name": "generate_world_map_preview",
                        "arguments": {},
                        "reason": "继续履行同一句中的地图请求。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "direct_gm_request",
                        "reason": "缺项清单只回答了能否进入第一章，地图请求尚未处理。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "direct_gm_request",
                        "reason": "缺项已经说明，地图工具也已明确询问缺少的名称。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.gate_status = "session_zero"

        outcome = agent.run(
            "是不是可以开始第一章了，悠老师地图画一张我看看",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("第零章还缺世界奥秘", outcome.reply)
        self.assertIn("地图还没有名字", outcome.reply)
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["get_session_zero_readiness", "generate_world_map_preview"],
        )
        self.assertEqual(len(core_client.calls), 2)
        completion_calls = [
            call
            for call in review_client.calls
            if call["operation"] == "gm_silence_responsibility_verification"
        ]
        self.assertEqual(len(completion_calls), 2)

    def test_direct_future_promise_must_finish_query_in_same_request(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description="读取缺项。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_session_zero_readiness",
                    result={
                        "missing": ["世界奥秘"],
                        "terminal_public_result": True,
                    },
                    public_reply="第零章还缺世界奥秘。",
                    lock_public_reply=True,
                ),
                side_effect="read",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "好的，我来看看还缺什么。",
                        "reason": "玩家要查询缺项。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "get_session_zero_readiness",
                        "arguments": {},
                        "reason": "立即查询并回答缺项。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "request_fulfilled": False,
                        "category": "direct_gm_request",
                        "reason": "拟回复只说准备查询，没有给出查询结果。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.gate_status = "session_zero"

        outcome = agent.run(
            "yes",
            recent_context="时悠：你是想问世界创建还缺什么吗？",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.reply, "第零章还缺世界奥秘。")
        self.assertEqual(len(core_client.calls), 2)
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "DIRECT_REPLY_LEFT_REQUEST_UNHANDLED",
        )

    def test_complete_direct_reply_is_not_forced_into_another_iteration(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "旅人是职业名；你问的是旅人职业的技能。",
                        "reason": "直接澄清规则名词。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "request_fulfilled": True,
                        "category": "rules_request",
                        "reason": "拟回复已经直接回答名词问题。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )

        context = execution_context()
        context.gate_status = "session_zero"
        outcome = agent.run(
            "旅人是职业吗？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(len(core_client.calls), 1)
        self.assertEqual(outcome.reply, "旅人是职业名；你问的是旅人职业的技能。")
        self.assertTrue(outcome.trace[0]["no_tool_completion_review"]["request_fulfilled"])

    def test_pronoun_complaint_about_gm_silence_requires_a_reply(self) -> None:
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "误判为玩家间讨论。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": (
                            "已经写入了。刚才按静默记录处理，没有再复述你们说过的设定。"
                        ),
                        "reason": "解释刚才的写入与静默处理。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "request_fulfilled": False,
                        "category": "table_fact_clarification",
                        "reason": "玩家在追问时悠为何没有回复。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "request_fulfilled": True,
                        "category": "table_fact_clarification",
                        "reason": "拟回复已经解释写入与静默状态。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "写入了但是她没回",
            recent_context=(
                "村夫：世界性威胁灰烬之潮有复苏的迹象。\n"
                "测试玩家乙：重大历史事件是索朗帝国内战。"
            ),
            context=context,
            state_summary={},
        )

        self.assertEqual(len(core_client.calls), 2)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("已经写入", outcome.reply)
        self.assertEqual(
            outcome.trace[0]["silence_responsibility"]["category"],
            "table_fact_clarification",
        )

    def test_allowlisted_python_signed_followup_executes_without_second_model_call(self) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="prepare_exact_transition",
                description="prepare one exact child call",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_exact_transition",
                        ok=True,
                        state_changed=True,
                        result={
                            "required_followup_tools": ["select_first_act"],
                            "required_followup_calls": [
                                {
                                    "tool_name": "select_first_act",
                                    "arguments": {"candidate_id": "candidate-1"},
                                    "python_auto_execute": True,
                                }
                            ],
                            "required_followup_mode": "all",
                            "python_auto_followup_terminal": True,
                        },
                        public_fallback_reply="转场参数已经锁定。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="select_first_act",
                description="finish the signed child call",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("candidate_id") or ""))
                    or GMToolReceipt.success(
                        "select_first_act",
                        state_changed=True,
                        public_reply="第一幕已确定。",
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "candidate_id",
                        "string",
                        "signed first act id",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "prepare_exact_transition",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 按已经锁定的参数完成转场",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(state, ["prepared", "candidate-1"])
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["prepare_exact_transition", "select_first_act"],
        )
        self.assertIn("转场参数已经锁定", outcome.reply)
        self.assertIn("第一幕已确定", outcome.reply)
        self.assertTrue(outcome.state_changed)
        self.assertEqual(
            outcome.trace[0]["python_signed_followups"][0]["tool_name"],
            "select_first_act",
        )

    def test_failed_python_signed_packet_rolls_back_all_prior_writes(self) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def set_state_changed(self, _changed: bool) -> None:
                return None

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(
            transaction_factory=lambda *_args: Transaction()
        )

        def prepare(_context, _arguments):
            state.append("proposal-cleared")
            return GMToolReceipt(
                tool_name="confirm_session_zero_proposal",
                ok=True,
                state_changed=True,
                result={
                    "required_followup_tools": [
                        "create_world_setting",
                        "update_world_setting",
                    ],
                    "required_followup_calls": [
                        {
                            "tool_name": "create_world_setting",
                            "arguments": {"value": "钟鸣公国"},
                            "python_auto_execute": True,
                        },
                        {
                            "tool_name": "update_world_setting",
                            "arguments": {"value": "钟鸣公国地图投影"},
                            "python_auto_execute": True,
                        },
                    ],
                    "required_followup_mode": "all",
                    "python_auto_followup_terminal": True,
                },
            )

        def create(_context, arguments):
            state.append(str(arguments.get("value") or ""))
            return GMToolReceipt.success(
                "create_world_setting",
                state_changed=True,
            )

        def fail_update(_context, _arguments):
            return GMToolReceipt.failure(
                "update_world_setting",
                "WORLD_SETTING_ALREADY_EXISTS",
                "地图投影冲突。",
                "修正签发包后重新提交整条事务。",
            )

        registry.register(
            GMToolDefinition(
                name="confirm_session_zero_proposal",
                description="prepare signed packet",
                handler=prepare,
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description="first signed write",
                handler=create,
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="update_world_setting",
                description="second signed write fails",
                handler=fail_update,
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "confirm_session_zero_proposal",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_world_setting",
                        "arguments": {"value": "钟鸣公国地图投影"},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=2,
        )

        outcome = agent.run(
            "确认这个完整版本。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, [])
        self.assertFalse(outcome.state_changed)
        self.assertEqual(outcome.mode, "gm_agent_unresolved")
        self.assertFalse(
            any(
                receipt.ok and receipt.state_changed
                for receipt in outcome.receipts
            )
        )

    def test_completed_signed_proposal_packet_stays_silent_in_table_discussion(
        self,
    ) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="confirm_session_zero_proposal",
                description="confirm proposal",
                handler=lambda _context, _arguments: (
                    state.append("confirmed")
                    or GMToolReceipt(
                        tool_name="confirm_session_zero_proposal",
                        ok=True,
                        state_changed=True,
                        result={
                            "required_followup_tools": ["create_world_setting"],
                            "required_followup_calls": [
                                {
                                    "tool_name": "create_world_setting",
                                    "arguments": {"value": "苦乐交织"},
                                    "python_auto_execute": True,
                                }
                            ],
                            "required_followup_mode": "all",
                            "python_auto_followup_terminal": True,
                            "silent_commit_allowed": True,
                            "source_message_already_public": True,
                        },
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description="save proposal",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("value") or ""))
                    or GMToolReceipt.success(
                        "create_world_setting",
                        result={
                            "silent_commit_allowed": True,
                            "source_message_already_public": True,
                        },
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "discussion",
                        "audience": "table",
                        "tool_name": "confirm_session_zero_proposal",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "玩家原话已经完成共识，不复述。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "我也赞成，再补一句最后仍要看得见希望。",
            recent_context="南星：我提议苦乐交织。",
            context=context,
            state_summary={},
        )

        self.assertEqual(state, ["confirmed", "苦乐交织"])
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")

    def test_persisted_map_name_followup_generates_without_second_model_call(
        self,
    ) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description="save the supplied map name",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("value") or ""))
                    or GMToolReceipt(
                        tool_name="create_world_setting",
                        ok=True,
                        state_changed=True,
                        result={
                            "required_followup_tools": [
                                "generate_world_map_preview"
                            ],
                            "required_followup_calls": [
                                {
                                    "tool_name": "generate_world_map_preview",
                                    "arguments": {"redraw": False},
                                    "python_auto_execute": True,
                                }
                            ],
                            "required_followup_mode": "all",
                        },
                    )
                ),
                parameters=(
                    GMToolParameter("value", "string", "map name", required=True),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="generate_world_map_preview",
                description="render the named map",
                handler=lambda _context, arguments: (
                    state.append(f"render:{bool(arguments.get('redraw'))}")
                    or GMToolReceipt.success(
                        "generate_world_map_preview",
                        result={"status": "generated"},
                        state_changed=True,
                        public_reply="地图画好了。",
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter("redraw", "boolean", "redraw existing map"),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "create_world_setting",
                        "arguments": {"value": "余烬大陆"},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "叫余烬大陆。",
            recent_context="时悠：这张地图还没有名字。你想叫它什么？",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(state, ["余烬大陆", "render:False"])
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["create_world_setting", "generate_world_map_preview"],
        )
        self.assertEqual(outcome.reply, "地图画好了。")

    def test_single_item_batch_executes_python_signed_followup_without_second_model_call(
        self,
    ) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="prepare_exact_transition",
                description="prepare one exact child call",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_exact_transition",
                        ok=True,
                        state_changed=True,
                        result={
                            "required_followup_tools": ["select_first_act"],
                            "required_followup_calls": [
                                {
                                    "tool_name": "select_first_act",
                                    "arguments": {"candidate_id": "candidate-1"},
                                    "python_auto_execute": True,
                                }
                            ],
                            "required_followup_mode": "all",
                            "python_auto_followup_terminal": True,
                        },
                        public_fallback_reply="转场参数已经锁定。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="select_first_act",
                description="finish the signed child call",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("candidate_id") or ""))
                    or GMToolReceipt.success(
                        "select_first_act",
                        state_changed=True,
                        public_reply="第一幕已确定。",
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "candidate_id",
                        "string",
                        "signed first act id",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "prepare_exact_transition",
                                "arguments": {},
                            }
                        ],
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 按已经锁定的参数完成转场",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(state, ["prepared", "candidate-1"])
        self.assertEqual(outcome.mode, "gm_agent_tool")
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["prepare_exact_transition", "select_first_act"],
        )
        self.assertEqual(
            outcome.trace[0]["python_signed_followups"][0]["tool_name"],
            "select_first_act",
        )
        self.assertEqual(
            [item["tool_name"] for item in outcome.trace[0]["batch_receipts"]],
            ["prepare_exact_transition", "select_first_act"],
        )

    def test_multi_item_batch_executes_each_python_signed_followup_inline(
        self,
    ) -> None:
        state: list[str] = []
        registry = GMToolRegistry()

        def prepare(name: str, candidate_id: str):
            def handler(_context, _arguments):
                state.append(name)
                return GMToolReceipt(
                    tool_name=name,
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": ["select_first_act"],
                        "required_followup_calls": [
                            {
                                "tool_name": "select_first_act",
                                "arguments": {"candidate_id": candidate_id},
                                "python_auto_execute": True,
                            }
                        ],
                        "required_followup_mode": "all",
                        "python_auto_followup_terminal": True,
                    },
                    public_fallback_reply=f"{name}已准备。",
                    lock_public_reply=True,
                )

            return handler

        for name, candidate_id in (
            ("prepare_transition_a", "candidate-a"),
            ("prepare_transition_b", "candidate-b"),
        ):
            registry.register(
                GMToolDefinition(
                    name=name,
                    description="prepare one exact child call",
                    handler=prepare(name, candidate_id),
                    side_effect="write_pending",
                )
            )
        registry.register(
            GMToolDefinition(
                name="select_first_act",
                description="finish one signed child call",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("candidate_id") or ""))
                    or GMToolReceipt.success(
                        "select_first_act",
                        state_changed=True,
                        public_reply=(
                            f"{arguments.get('candidate_id')}已提交。"
                        ),
                        lock_public_reply=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "candidate_id",
                        "string",
                        "signed first act id",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "prepare_transition_a", "arguments": {}},
                            {"tool_name": "prepare_transition_b", "arguments": {}},
                        ],
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 按锁定参数完成两项转场",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            state,
            [
                "prepare_transition_a",
                "candidate-a",
                "prepare_transition_b",
                "candidate-b",
            ],
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            [
                "prepare_transition_a",
                "select_first_act",
                "prepare_transition_b",
                "select_first_act",
            ],
        )
        self.assertEqual(
            [item["tool_name"] for item in outcome.trace[0]["batch_receipts"]],
            [
                "prepare_transition_a",
                "select_first_act",
                "prepare_transition_b",
                "select_first_act",
            ],
        )

    def test_unknown_python_auto_execute_marker_requires_model_round(self) -> None:
        state: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="prepare_future_transition",
                description="prepare an unreviewed future child call",
                handler=lambda _context, _arguments: (
                    state.append("prepared")
                    or GMToolReceipt(
                        tool_name="prepare_future_transition",
                        ok=True,
                        state_changed=True,
                        result={
                            "required_followup_tools": [
                                "future_exact_transition"
                            ],
                            "required_followup_calls": [
                                {
                                    "tool_name": "future_exact_transition",
                                    "arguments": {
                                        "transition_id": "future-1"
                                    },
                                    "python_auto_execute": True,
                                }
                            ],
                            "required_followup_mode": "all",
                            "python_auto_followup_terminal": True,
                        },
                        public_fallback_reply="未来转场参数已经锁定。",
                    )
                ),
                side_effect="write_pending",
            )
        )
        registry.register(
            GMToolDefinition(
                name="future_exact_transition",
                description="unreviewed future exact child",
                handler=lambda _context, arguments: (
                    state.append(str(arguments.get("transition_id") or ""))
                    or GMToolReceipt.success(
                        "future_exact_transition",
                        state_changed=True,
                        public_reply="未来转场完成。",
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "transition_id",
                        "string",
                        "future transition id",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "prepare_future_transition",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "future_exact_transition",
                        "arguments": {"transition_id": "future-1"},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "未来转场完成。",
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 按锁定参数完成未来转场",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertGreaterEqual(len(client.calls), 2)
        self.assertEqual(state, ["prepared", "future-1"])
        rejection = next(
            item["python_signed_followup_rejected"]
            for item in outcome.trace
            if "python_signed_followup_rejected" in item
        )
        self.assertEqual(rejection["reason"], "tool_not_allowlisted")
        self.assertEqual(
            rejection["tool_names"],
            ["future_exact_transition"],
        )
        self.assertFalse(
            any(
                call.get("tool_name") == "future_exact_transition"
                for item in outcome.trace[:1]
                for call in item.get("python_signed_followups", [])
            )
        )

    def test_provider_failure_keeps_complete_tool_result_with_authoritative_reply(
        self,
    ) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="record_fact",
                description="record",
                handler=lambda _context, _arguments: (
                    state.append("recorded")
                    or GMToolReceipt(
                        tool_name="record_fact",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这项设定记下了。",
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "record_fact",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 记下这项设定",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, ["recorded"])
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "这项设定记下了。")

    def test_real_focus_branch_rolls_back_when_required_action_never_arrives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            runtime = service._runtime("focus-rollback")
            for name in ("伊莉雅", "赛璃"):
                runtime.app.character_manager.add(
                    Character(
                        name=name,
                        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                        max_hp=45,
                        hp=45,
                        max_mp=45,
                        mp=45,
                        traits=["pc"],
                    )
                )
            original = runtime.app.start_scene(
                "风铃廊",
                SceneType.STANDARD,
                location="白花碑驿站",
                participants=["伊莉雅"],
            )
            service._autosave_campaign(runtime, "focus-rollback")
            snapshot_path = runtime.app.memory_store._snapshot_path(
                "focus-rollback"
            )
            snapshot_before = snapshot_path.read_bytes()
            client = ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "focus_scene_branch",
                            "arguments": {
                                "actor": "赛璃",
                                "name": "驿站外缘",
                                "scene_type": "standard",
                                "location": "白花碑驿站外缘",
                                "objective": "查看追兵火光",
                                "private_situation": {
                                    "current_pressure": "远处有巡逻灯火",
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                ]
            )
            agent = LLMGMToolAgent(
                client,
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context(
                campaign_id="focus-rollback",
                speaker="赛璃",
            )
            context.metadata.update(
                {
                    "current_message": "赛璃去驿站外缘查看追兵火光。",
                    "recent_public_context": "伊莉雅留在风铃廊。",
                }
            )

            outcome = agent.run(
                "赛璃去驿站外缘查看追兵火光。",
                recent_context="伊莉雅留在风铃廊。",
                context=context,
                state_summary={},
            )

            self.assertFalse(outcome.state_changed)
            self.assertEqual(
                outcome.mode,
                "gm_agent_message_transaction_rolled_back",
            )
            self.assertEqual(
                runtime.app.scene_manager.current_scene.scene_id,
                original.scene_id,
            )
            self.assertEqual(runtime.app.scene_manager.suspended_scenes, [])
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)

    def test_execution_scope_rejects_system_only_tool_from_player_message(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="system NPC beat",
                handler=lambda _context, _arguments: (
                    calls.append("executed")
                    or GMToolReceipt.success(
                        "decide_npc_action",
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "这不是当前玩家消息可以触发的行动。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "继续。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [])
        self.assertEqual(outcome.reply, "这不是当前玩家消息可以触发的行动。")
        self.assertTrue(
            any(
                step.get("protocol_error") == "TOOL_NOT_AVAILABLE_IN_CONTEXT"
                for step in outcome.trace
            )
        )

    def test_free_scene_beat_cannot_run_an_npc_action(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="system NPC beat",
                handler=lambda _context, _arguments: (
                    calls.append("executed")
                    or GMToolReceipt.success(
                        "decide_npc_action",
                        state_changed=True,
                        public_reply="守门人终于从门后走了出来。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_action",
                        "arguments": {},
                    }
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context(speaker="系统主动节拍")
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
            }
        )

        outcome = agent.run(
            "请判断现场NPC是否需要行动。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(calls, [])
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.target, "silent")

    def test_session_zero_heartbeat_prompt_is_not_parsed_as_player_contribution(
        self,
    ) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context(speaker="系统主动节拍")
        context.gate_status = "session_zero"
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "session_zero_nudge",
            }
        )

        plans = agent._message_integrity_plans(
            "请全桌谈谈魔法与科技在日常生活中如何共存。",
            context=context,
            state_summary={},
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].world_categories, ())

    def test_execution_scope_rejects_adventure_tool_during_session_zero(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="start an adventure scene",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "start_scene",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"

        self.assertFalse(agent._tool_is_permitted("start_scene", context))

    def test_execution_scope_preserves_unmanaged_extension_tools(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="custom_extension",
                description="custom",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "custom_extension"
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )

        self.assertTrue(
            agent._tool_is_permitted("custom_extension", execution_context())
        )

    def test_addressed_player_can_use_safe_read_omitted_by_schema_narrowing(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="discover_capabilities",
                description="发现能力。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "discover_capabilities"
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_hero_state",
                description="读取公开角色状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_hero_state"
                ),
                allow_addressed_dynamic_grant=True,
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"
        context.metadata["gm_dynamic_capabilities_enabled"] = True

        self.assertFalse(agent._tool_is_permitted("get_hero_state", context))
        self.assertEqual(
            context.metadata["gm_addressed_dynamic_read_grants"],
            ["get_hero_state"],
        )
        self.assertTrue(agent._tool_is_permitted("get_hero_state", context))

    def test_addressed_dynamic_grant_cannot_widen_to_write_tool(self) -> None:
        registry = GMToolRegistry()
        with self.assertRaisesRegex(ValueError, "只有只读工具"):
            registry.register(
                GMToolDefinition(
                    name="update_hero_draft",
                    description="更新角色草稿。",
                    handler=lambda _context, _arguments: GMToolReceipt.success(
                        "update_hero_draft",
                        state_changed=True,
                    ),
                    side_effect="write",
                    allow_addressed_dynamic_grant=True,
                )
            )

    def test_system_beat_rejects_unmanaged_extension_tools(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="custom_world_writer",
                description="未托管的扩展写工具。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "custom_world_writer",
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context(speaker="系统主动节拍")
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "adventure_table_nudge",
            }
        )

        self.assertFalse(
            agent._tool_is_permitted("custom_world_writer", context)
        )

    def test_agent_prompt_composes_only_current_phase_guidance(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=GMToolRegistry(),
        )
        session_context = execution_context()
        session_context.gate_status = "session_zero"
        session_prompt = agent._system_prompt(
            session_context,
            observed_state={},
        )
        session_post_tool_prompt = agent._system_prompt(
            session_context,
            observed_state={},
            has_receipts=True,
        )
        adventure_prompt = agent._system_prompt(
            execution_context(),
            observed_state={"runtime": {"conflict": {"active": False}}},
        )

        self.assertIn("提交对应来源事件新增或明确纠正的最小差量", session_prompt)
        self.assertIn("tool_name、arguments、calls、terminal_decision", session_prompt)
        self.assertIn("玩家明确表示方案已形成共识", session_prompt)
        self.assertIn("先判断message_kind，再判断audience与行动阶段", session_prompt)
        self.assertIn("候选、建议、征求同伴意见", session_prompt)
        self.assertIn("发言人、建议、行动与闲聊沿用各自原始事件", session_prompt)
        self.assertIn("状态写入属于后台工作", session_prompt)
        self.assertIn("玩家主动征求看法时再完整点评", session_prompt)
        self.assertIn("不逐项报出记录类别", session_prompt)
        self.assertIn("仍另建historical_events记录", session_prompt)
        self.assertIn("每笔仍有自己的类别、名称、正文与回执", session_prompt)
        self.assertIn("逐句重读current_message", session_post_tool_prompt)
        self.assertIn("另建historical_events", session_post_tool_prompt)
        self.assertNotIn("### NPC与集体", session_prompt)
        self.assertIn("required_followup_calls", adventure_prompt)
        self.assertIn("移动后必须兑现的NPC承诺", adventure_prompt)
        self.assertIn("followup调用与ID", adventure_prompt)
        self.assertNotIn("## 当前阶段：开团前与第零章", adventure_prompt)

    def test_hero_update_and_confirmation_batch_requires_observation(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=GMToolRegistry(),
        )

        error = agent._dependent_batch_error(
            [
                {
                    "tool_name": "update_hero_draft",
                    "arguments": {"subject": "苍祈", "patch": {"equipment": ["魔典"]}},
                },
                {
                    "tool_name": "confirm_hero_draft",
                    "arguments": {"subject": "苍祈"},
                },
            ]
        )

        self.assertIsNotNone(error)
        protocol_error = error["protocol_error"]
        self.assertEqual(
            protocol_error["error_code"],
            "DEPENDENT_TOOL_BATCH_REQUIRES_OBSERVATION",
        )
        self.assertIn("先单独调用update_hero_draft", protocol_error["correction_hint"])

    def test_required_retry_schema_bypasses_normal_phase_scope(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_check_action"
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="position",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_in_scene_action"
                ),
                parameters=(
                    GMToolParameter("actor", "string", "actor", required=True),
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        schemas = agent._available_tool_schemas(
            context,
            required_retry_tool="perform_in_scene_action",
        )

        self.assertEqual(
            [str(schema.get("name") or "") for schema in schemas],
            ["perform_in_scene_action"],
        )

    def test_narrow_registry_without_discovery_exposes_custom_tool(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="record_probe_fact",
                description="record one probe fact",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "record_probe_fact"
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient([]),
            model="fake",
            registry=registry,
        )

        schemas = agent._available_tool_schemas(execution_context())

        self.assertEqual([item["name"] for item in schemas], ["record_probe_fact"])

    def test_post_tool_iteration_uses_focused_receipt_prompt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero",
                description="update hero",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="update_hero",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="技能记下了。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_hero",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "这项技能记下了。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅选择保镖。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "这项技能记下了。")
        self.assertIn("工具事务收尾层", client.calls[1]["messages"][0].content)
        self.assertIn("确认后自然收束", client.calls[1]["messages"][0].content)

    def test_cross_scene_move_required_npc_followup_finishes_same_message(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="move_scene_group",
                description="move then ask",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="move_scene_group",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": ["decide_npc_response"],
                        "required_followup_calls": [
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {
                                    "name": "白花守望会会长",
                                    "actor": "苍祈",
                                    "response_instruction": "明确表态。",
                                },
                            }
                        ],
                    },
                    public_fallback_reply="苍祈抵达风铃廊。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc answers",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="会长说：“旧路可以开，但巡守必须同行。”",
                    lock_public_reply=True,
                ),
                parameters=(
                    GMToolParameter("name", "string", "NPC", required=True),
                    GMToolParameter("actor", "string", "交谈者", required=True),
                    GMToolParameter(
                        "response_instruction",
                        "string",
                        "回应要求",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "move_scene_group",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花守望会会长",
                            "actor": "苍祈",
                            "response_instruction": "明确表态。",
                        },
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "苍祈去风铃廊请会长明确表态。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["move_scene_group", "decide_npc_response"],
        )
        self.assertIn("苍祈抵达风铃廊", outcome.reply)
        self.assertIn("旧路可以开", outcome.reply)
        second_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            [tool["name"] for tool in second_request["available_tools"]],
            ["decide_npc_response"],
        )

    def test_independent_followups_cannot_be_skipped_after_first_completion(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check with two independent consequences",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="perform_check_action",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": [
                            "resolve_gm_opportunity",
                            "decide_npc_response",
                        ],
                        "required_followup_calls": [
                            {
                                "tool_name": "resolve_gm_opportunity",
                                "arguments": {},
                            },
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {},
                            },
                        ],
                        "required_followup_mode": "all",
                    },
                    public_fallback_reply="伊莉雅失手惊动了守望会。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="resolve fumble",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="resolve_gm_opportunity",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="门外的巡逻铃骤然响起。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc pays off condition",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=(
                        "会长沉声道：“我答应的路，仍会替你们打开。”"
                    ),
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "perform_check_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "resolve_gm_opportunity",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "巡逻铃响起，事情结束了。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅尝试履行会长提出的条件。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            [
                "perform_check_action",
                "resolve_gm_opportunity",
                "decide_npc_response",
            ],
        )
        self.assertIn("伊莉雅失手", outcome.reply)
        self.assertIn("巡逻铃骤然响起", outcome.reply)
        self.assertIn("我答应的路", outcome.reply)
        self.assertEqual(len(client.calls), 4)

    def test_required_followup_rejects_wrong_npc_before_tool_execution(self) -> None:
        registry = GMToolRegistry()
        executed: list[dict[str, object]] = []
        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check that obligates one NPC",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="perform_check_action",
                    ok=True,
                    state_changed=True,
                    result={
                        "required_followup_tools": ["decide_npc_response"],
                        "required_followup_calls": [
                            {
                                "tool_name": "decide_npc_response",
                                "arguments": {
                                    "name": "白花守望会会长",
                                    "condition_id": "condition-1",
                                },
                            }
                        ],
                        "required_followup_mode": "all",
                    },
                    public_fallback_reply="伊莉雅完成了约定的暗号。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )

        def answer(_context, arguments):
            executed.append(dict(arguments))
            return GMToolReceipt(
                tool_name="decide_npc_response",
                ok=True,
                state_changed=True,
                result={"npc": arguments["name"]},
                public_fallback_reply="会长打开了旧路闸门。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc answers",
                handler=answer,
                parameters=(
                    GMToolParameter(
                        "name",
                        "string",
                        "NPC name",
                        required=True,
                    ),
                    GMToolParameter(
                        "condition_id",
                        "string",
                        "condition",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "perform_check_action",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花巡守",
                            "condition_id": "condition-1",
                        },
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {
                            "name": "白花守望会会长",
                            "condition_id": "condition-1",
                        },
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅完成了会长要求的风铃暗号。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            executed,
            [
                {
                    "name": "白花守望会会长",
                    "condition_id": "condition-1",
                }
            ],
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["perform_check_action", "decide_npc_response"],
        )
        self.assertIn("打开了旧路闸门", outcome.reply)
        self.assertTrue(
            any(
                item.get("protocol_error")
                == "REQUIRED_FOLLOWUP_ARGUMENT_MISMATCH"
                for item in outcome.trace
            )
        )

    def test_single_successful_player_write_requires_brief_acknowledgement(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero_draft",
                description="update hero",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="update_hero_draft",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="这项角色信息记下了。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "update_hero_draft",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "未被直接叫到的逐项技能选择只需写入。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "table",
                        "reply": "保镖记下了。",
                        "reason": "玩家要求的角色修改已经成功，需要简短确认。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "伊莉雅选择保镖。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "保镖记下了。")
        self.assertTrue(outcome.state_changed)
        retry_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "PLAYER_STATE_CHANGE_REQUIRES_ACKNOWLEDGEMENT",
        )
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["correction_hint"],
            agent._STATE_CHANGE_ACKNOWLEDGEMENT_HINT,
        )
        self.assertNotIn(
            "不要",
            retry_request["history"][-1]["protocol_error"]["correction_hint"],
        )

    def test_post_tool_silence_review_catches_delegated_session_zero_task(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description="写入玩家明确给出的世界设定。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "create_world_setting",
                    result={
                        "operation": "create",
                        "category": "kingdoms",
                        "name": "索朗帝国",
                        "value": "一个很富饶的王国",
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="保存GM受托创作、等待玩家确认的世界设定提案。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "propose_session_zero_update",
                    result={
                        "proposal_id": "proposal-history-1",
                        "summary": "赤沙断流",
                    },
                    state_changed=True,
                ),
                side_effect="write_pending",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "state_contribution",
                        "has_independent_followup": False,
                        "audience": "table",
                        "tool_name": "create_world_setting",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "先保存玩家给出的国家，历史事件以后再处理。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "tool_name": "propose_session_zero_update",
                        "arguments": {},
                        "reason": "把GM受托创作的历史事件保存为待确认提案。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "reply": "历史事件我提议定为“赤沙断流”：旧王朝为灌溉索朗腹地改道大河，也让东部沙海从此扩张。你觉得这段合适吗？",
                        "reason": "完成玩家明确委托给GM的历史事件提案。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "delegated_gm_task",
                        "reason": "三项回执没有完成玩家委托GM创作重大历史事件的请求。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "delegated_gm_task",
                        "reason": "待确认提案已保存并展示了具体内容。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "地图是很普通的大陆，主要王国是索朗帝国，一个很富饶的王国，重大历史事件悠老师想一个。",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("赤沙断流", outcome.reply)
        self.assertTrue(outcome.state_changed)
        self.assertEqual(len(core_client.calls), 3)
        retry_request = json.loads(core_client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "POST_TOOL_SILENCE_LEFT_REQUEST_UNHANDLED",
        )
        post_review_request = json.loads(
            review_client.calls[0]["messages"][-1].content
        )
        self.assertEqual(
            post_review_request["completed_tool_receipts"][0]["result"]["name"],
            "索朗帝国",
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["create_world_setting", "propose_session_zero_update"],
        )

    def test_existing_session_zero_proposal_only_needs_public_reply(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="create_world_setting",
                description="写入玩家确认的世界设定。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "create_world_setting",
                    result={
                        "category": "kingdoms",
                        "name": "索朗帝国",
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="保存待确认的GM创作提案。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "propose_session_zero_update",
                    result={
                        "proposal_id": "proposal-history-1",
                        "summary": "灰烬战争",
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                    },
                    state_changed=True,
                ),
                side_effect="write_pending",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "message_kind": "state_contribution",
                        "has_independent_followup": False,
                        "audience": "table",
                        "calls": [
                            {
                                "tool_name": "create_world_setting",
                                "arguments": {},
                            },
                            {
                                "tool_name": "propose_session_zero_update",
                                "arguments": {},
                            },
                        ],
                        "terminal_decision": "silent",
                        "reason": "保存玩家贡献与GM待确认提案。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "reply": "重大历史事件我想到“灰烬战争”：索朗帝国的胜利掏空了国库，也催生了今天的魔导技术。这个方向合适吗？",
                        "reason": "把已经保存的待确认提案展示给玩家。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "delegated_gm_task",
                        "reason": "待确认提案已经保存，但内容还没有展示给玩家。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "delegated_gm_task",
                        "reason": "待确认提案已保存并展示了具体内容。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "主要王国是索朗帝国，重大历史事件悠老师想一个。",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("灰烬战争", outcome.reply)
        self.assertEqual(len(core_client.calls), 2)
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["create_world_setting", "propose_session_zero_update"],
        )
        self.assertFalse(
            any(step.get("post_tool_followup_tool_missing") for step in outcome.trace)
        )

    def test_public_promise_cannot_end_delegated_session_zero_task(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description="读取第零章缺项。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_session_zero_readiness",
                    result={
                        "ready": False,
                        "missing": ["重大历史事件", "世界奥秘", "世界威胁"],
                    },
                    state_changed=False,
                ),
                side_effect="read",
            )
        )
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="保存GM受托创作、等待玩家确认的世界设定提案。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "propose_session_zero_update",
                    result={
                        "proposal_id": "proposal-world-gaps-1",
                        "summary": "灰潮断代、无名王陵与枯日风暴",
                    },
                    state_changed=True,
                ),
                side_effect="write_pending",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "tool_name": "get_session_zero_readiness",
                        "arguments": {},
                        "reason": "先读取缺项。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "reply": "好嘞，我先把缺项理一遍，然后给你们补上几个方向。",
                        "reason": "先读取缺项，具体内容以后再处理。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "tool_name": "propose_session_zero_update",
                        "arguments": {},
                        "reason": "保存受托补齐的具体世界设定提案。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "reply": (
                            "我提议补上三笔：塑造时代的事件叫“灰潮断代”；"
                            "世界奥秘是无人记得王陵中埋着谁；当前威胁则是每年扩张的枯日风暴。"
                            "这三个方向你们觉得合适吗？"
                        ),
                        "reason": "具体提案已保存并展示给玩家确认。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "delegated_gm_task",
                        "reason": "拟回复只有未来承诺，没有给出并保存具体世界设定提案。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "delegated_gm_task",
                        "reason": "待确认提案已保存，拟回复也展示了具体内容。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "帮我们想想剩下的世界共创缺项内容。",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("灰潮断代", outcome.reply)
        self.assertEqual(len(core_client.calls), 4)
        retry_request = json.loads(core_client.calls[2]["messages"][-1].content)
        protocol_error = retry_request["history"][-1]["protocol_error"]
        self.assertEqual(
            protocol_error["error_code"],
            "POST_TOOL_REPLY_LEFT_REQUEST_UNHANDLED",
        )
        first_completion_review = next(
            json.loads(call["messages"][-1].content)
            for call in review_client.calls
            if call["operation"] == "gm_silence_responsibility_verification"
        )
        self.assertEqual(
            first_completion_review["proposed_public_reply"],
            "好嘞，我先把缺项理一遍，然后给你们补上几个方向。",
        )
        self.assertEqual(
            first_completion_review["completed_tool_receipts"][0]["tool_name"],
            "get_session_zero_readiness",
        )
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["get_session_zero_readiness", "propose_session_zero_update"],
        )

    def test_locked_concrete_session_zero_proposal_finishes_without_second_review(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description="读取第零章缺项。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_session_zero_readiness",
                    result={"ready": False, "missing": ["世界奥秘"]},
                ),
                side_effect="read",
            )
        )
        summary = (
            "世界奥秘是：索朗帝国每代皇帝都会梦见同一座无名王陵。\n\n"
            "你们觉得如何？"
        )
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="保存并公开待确认提案。",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="propose_session_zero_update",
                    ok=True,
                    result={
                        "proposal": {
                            "id": "proposal-world-1",
                            "summary": summary,
                            "world_operations": [
                                {
                                    "operation": "create",
                                    "category": "mysteries",
                                    "name": summary,
                                    "value": summary,
                                    "visibility": "public",
                                }
                            ],
                        }
                    },
                    state_changed=True,
                    public_fallback_reply=summary,
                    lock_public_reply=True,
                ),
                side_effect="write_pending",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "get_session_zero_readiness",
                        "arguments": {},
                        "reason": "读取缺项。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "我先想想，等会儿告诉你。",
                        "reason": "未来承诺。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "propose_session_zero_update",
                        "arguments": {},
                        "reason": "保存具体提案并直接公开其摘要。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": True,
                        "category": "delegated_gm_task",
                        "reason": "未来承诺没有完成玩家委托。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.gate_status = "session_zero"

        outcome = agent.run(
            "@时悠，帮我们想想剩下的世界共创缺项内容。",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.mode, "gm_agent_tool")
        self.assertEqual("".join(outcome.reply.split()), "".join(summary.split()))
        self.assertEqual(len(core_client.calls), 3)
        self.assertEqual(len(review_client.calls), 1)
        self.assertNotEqual(
            outcome.mode,
            "gm_agent_message_transaction_rolled_back",
        )

    def test_concrete_session_zero_readiness_answer_can_finish_after_read(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_session_zero_readiness",
                description="读取第零章缺项。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "get_session_zero_readiness",
                    result={
                        "ready": False,
                        "missing": ["世界奥秘", "世界威胁"],
                    },
                    state_changed=False,
                ),
                side_effect="read",
            )
        )
        core_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "message_kind": "gm_request",
                        "has_independent_followup": False,
                        "audience": "gm",
                        "calls": [
                            {
                                "tool_name": "get_session_zero_readiness",
                                "arguments": {},
                            }
                        ],
                        "terminal_decision": "final",
                        "reply": "目前还缺世界奥秘和世界威胁，其余世界创建项已经记下。",
                        "reason": "直接回答玩家查询的缺项。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        review_client = ScriptedClient(
            [
                json.dumps(
                    {
                        "requires_gm_reply": False,
                        "category": "direct_gm_request",
                        "reason": "只读回执和拟回复已经明确给出缺项。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            core_client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=GMReplyGroundingVerifier(
                review_client,
                model="semantic-model",
            ),
        )
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "session_zero"

        outcome = agent.run(
            "现在世界共创还缺什么？",
            recent_context="大家正在共同创建世界。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("世界奥秘", outcome.reply)
        self.assertEqual(len(core_client.calls), 1)
        completion_calls = [
            call
            for call in review_client.calls
            if call["operation"] == "gm_silence_responsibility_verification"
        ]
        self.assertEqual(len(completion_calls), 1)

    def test_preparatory_receipt_cannot_end_silently_before_required_followup(self) -> None:
        registry = GMToolRegistry()
        committed: list[dict[str, object]] = []
        registry.register(
            GMToolDefinition(
                name="discover_local_action",
                description="取得本条消息需要的最终状态工具。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "discover_local_action",
                    result={
                        "required_followup_tools": ["commit_final_location"],
                        "required_followup_mode": "any",
                    },
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_final_location",
                description="一次提交物件的最终落点。",
                handler=lambda _context, arguments: (
                    committed.append(dict(arguments))
                    or GMToolReceipt.success(
                        "commit_final_location",
                        result={"silent_commit_allowed": True},
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter(
                        "location",
                        "string",
                        "玩家动作结束时的最终落点。",
                        required=True,
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "players",
                        "tool_name": "discover_local_action",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "先取得能力，但错误地尝试提前结束。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "players",
                        "tool_name": "commit_final_location",
                        "arguments": {"location": "艾丽妮牢房一侧"},
                        "terminal_decision": "silent",
                        "reason": "提交玩家已经完整说出的最终落点。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "诺艾尔捡起铁片后，从铁栏缝隙抛到艾丽妮那边。",
            recent_context="两人在相邻牢房。",
            context=context,
            state_summary={},
        )

        self.assertEqual(committed, [{"location": "艾丽妮牢房一侧"}])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertTrue(outcome.state_changed)

    def test_receipt_fallback_preserves_all_successful_batch_domains(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="commit_world",
                ok=True,
                state_changed=True,
                public_fallback_reply="世界设定记下了。",
            ),
            GMToolReceipt(
                tool_name="record_line",
                ok=True,
                state_changed=True,
                public_fallback_reply="ok，已记录这条界限。",
            ),
            GMToolReceipt(
                tool_name="record_veil",
                ok=True,
                state_changed=True,
                public_fallback_reply="ok，已记录这条帷幕。",
            ),
        ]

        reply = GMToolReceiptPolicy.authoritative_reply(receipts)

        self.assertIn("世界设定", reply)
        self.assertIn("界限", reply)
        self.assertIn("帷幕", reply)

    def test_confirmation_fallback_supersedes_intermediate_draft_update(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="update_hero_draft",
                ok=True,
                state_changed=True,
                public_fallback_reply="这项角色信息记下了。",
            ),
            GMToolReceipt(
                tool_name="confirm_hero_draft",
                ok=True,
                state_changed=True,
                public_fallback_reply="好，洛岚建好了。",
            ),
        ]

        reply = GMToolReceiptPolicy.authoritative_reply(receipts)

        self.assertEqual(reply, "好，洛岚建好了。")

    def test_registry_rejects_invalid_arguments_before_side_effect(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="save", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save",
                description="test",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )

        receipt = registry.execute("save", {"slot": 7}, execution_context())

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_TYPE_MISMATCH")
        self.assertTrue(receipt.retryable)
        self.assertEqual(calls, [])

    def test_registry_rejects_invalid_nested_arguments_before_side_effect(self) -> None:
        calls: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="test",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(tool_name="commit_world", ok=True, state_changed=True)
                ),
                parameters=(
                    GMToolParameter(
                        "updates",
                        "object",
                        "updates",
                        required=True,
                        schema_details={
                            "properties": {
                                "kingdoms": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                }
                            },
                            "additionalProperties": False,
                        },
                    ),
                ),
                side_effect="write",
            )
        )

        receipt = registry.execute(
            "commit_world",
            {"updates": {"kingdoms": ["钟鸣公国"]}},
            execution_context(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_SCHEMA_MISMATCH")
        self.assertIn("updates.kingdoms", receipt.message)
        self.assertIn("argument_schema", receipt.result)
        self.assertIn("该参数用途：updates", receipt.correction_hint)
        self.assertIn("移动到对应参数", receipt.correction_hint)
        self.assertEqual(calls, [])

    def test_nested_enum_error_lists_legal_values(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_segments",
                description="test",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "commit_segments"
                ),
                parameters=(
                    GMToolParameter(
                        "segments",
                        "array",
                        "segments",
                        required=True,
                        schema_details={
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tag": {
                                        "type": "string",
                                        "enum": ["fact", "direct_answer"],
                                    }
                                },
                                "required": ["tag"],
                                "additionalProperties": False,
                            }
                        },
                    ),
                ),
            )
        )

        receipt = registry.execute(
            "commit_segments",
            {"segments": [{"tag": "new_gate"}]},
            execution_context(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "ARGUMENT_SCHEMA_MISMATCH")
        self.assertIn("允许值：fact、direct_answer", receipt.message)

    def test_current_message_provenance_is_hidden_and_cannot_be_spoofed(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        definition = GMToolDefinition(
            name="commit",
            description="commit",
            handler=lambda _context, arguments: (
                calls.append(arguments)
                or GMToolReceipt(tool_name="commit", ok=True, state_changed=True)
            ),
            parameters=(
                GMToolParameter("value", "string", "value", required=True),
                GMToolParameter(
                    "evidence",
                    "string",
                    "server provenance",
                    required=True,
                    source="current_message",
                ),
            ),
            side_effect="write",
        )
        registry.register(definition)
        context = execution_context()
        context.metadata["current_message"] = "玩家真正说的话"

        rejected = registry.execute(
            "commit",
            {"value": "事实", "evidence": "模型伪造的话"},
            context,
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "SYSTEM_ARGUMENT_NOT_ALLOWED")
        self.assertNotIn("evidence", definition.schema()["parameters"]["properties"])
        self.assertEqual(calls, [])

        receipt = registry.execute("commit", {"value": "事实"}, context)

        self.assertTrue(receipt.ok)
        self.assertEqual(
            calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )

    def test_freshness_guard_and_handler_receive_effective_arguments(self) -> None:
        guard_calls: list[dict[str, object]] = []
        handler_calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit",
                description="commit",
                handler=lambda _context, arguments: (
                    handler_calls.append(dict(arguments))
                    or GMToolReceipt.success("commit", state_changed=True)
                ),
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "server provenance",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        context = execution_context()
        context.metadata["current_message"] = "玩家真正说的话"

        receipt = registry.execute(
            "commit",
            {"value": "事实"},
            context,
            freshness_guard=lambda _definition, arguments, _context: (
                guard_calls.append(dict(arguments))
                or True
            ),
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            guard_calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )
        self.assertEqual(
            handler_calls,
            [{"value": "事实", "evidence": "玩家真正说的话"}],
        )

    def test_stale_guard_blocks_write_under_transaction_lock(self) -> None:
        calls: list[dict[str, object]] = []
        guard_calls: list[str] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="save", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save",
                description="test",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )

        receipt = registry.execute(
            "save",
            {"slot": "第一幕"},
            execution_context(),
            freshness_guard=lambda definition, _arguments, _context: (
                guard_calls.append(definition.name) or False
            ),
            side_effect_lock=threading.RLock(),
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "STALE_AGENT_REQUEST")
        self.assertFalse(receipt.retryable)
        self.assertEqual(guard_calls, ["save"])
        self.assertEqual(calls, [])

    def test_read_tool_uses_campaign_lock_for_a_coherent_snapshot(self) -> None:
        entered: list[str] = []

        class TrackingLock:
            def __enter__(self):
                entered.append("enter")
                return self

            def __exit__(self, *_args):
                entered.append("exit")
                return False

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="get_state",
                description="read",
                handler=lambda _context, _arguments: (
                    entered.append("handler")
                    or GMToolReceipt.success("get_state")
                ),
                side_effect="read",
            )
        )

        receipt = registry.execute(
            "get_state",
            {},
            execution_context(),
            side_effect_lock=TrackingLock(),
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(entered, ["enter", "handler", "exit"])

    def test_agent_stops_silently_when_scheduled_write_becomes_stale(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(tool_name="commit", ok=True, state_changed=True)

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit",
                description="test",
                handler=handler,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交主动节拍。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "系统GM主动节拍请求",
            recent_context="",
            context=execution_context(),
            state_summary={},
            freshness_guard=lambda *_args: False,
            side_effect_lock=threading.RLock(),
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_stale")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(calls, [])

    def test_agent_rolls_back_write_when_request_becomes_stale_inside_handler(
        self,
    ) -> None:
        state: list[str] = []
        freshness = threading.Event()
        freshness.set()
        handler_entered = threading.Event()
        release_handler = threading.Event()
        guard_arity: list[int] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        def request_is_current(*args: object) -> bool:
            guard_arity.append(len(args))
            return freshness.is_set()

        def handler(_context, _arguments):
            handler_entered.set()
            if not release_handler.wait(timeout=2):
                raise AssertionError("等待群聊 freshness 翻转超时。")
            state.append("狐獴穿过矮墙")
            return GMToolReceipt.success(
                "record_local_move",
                state_changed=True,
                public_reply="狐獴已经穿过矮墙。",
                lock_public_reply=True,
            )

        registry = GMToolRegistry(
            transaction_factory=lambda *_args: Transaction()
        )
        registry.register(
            GMToolDefinition(
                name="record_local_move",
                description="test",
                handler=handler,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "record_local_move",
                            "arguments": {},
                            "reason": "登记当前局部移动。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )
        result: dict[str, object] = {}
        failures: list[BaseException] = []

        def invoke_agent() -> None:
            try:
                result["outcome"] = agent.run(
                    "狐獴穿过矮墙。",
                    recent_context="",
                    context=execution_context(),
                    state_summary={},
                    freshness_guard=request_is_current,
                    commit_freshness_guard=request_is_current,
                    side_effect_lock=threading.RLock(),
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        worker = threading.Thread(target=invoke_agent)
        worker.start()
        self.assertTrue(handler_entered.wait(timeout=1))
        freshness.clear()
        release_handler.set()
        worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        outcome = result["outcome"]
        self.assertEqual(state, [])
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_stale")
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertFalse(outcome.receipts[0].state_changed)
        self.assertTrue(outcome.receipts[0].result["rolled_back"])
        self.assertEqual(guard_arity, [3, 0])
        freshness_trace = outcome.trace[-1]["transaction_freshness"]
        self.assertEqual(
            freshness_trace["error_code"],
            "STALE_AGENT_REQUEST",
        )

    def test_agent_can_repair_rejected_tool_call_from_receipt(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(_context, arguments):
            calls.append(arguments)
            return GMToolReceipt(
                tool_name="save_campaign",
                ok=True,
                result={"slot": arguments["slot"]},
                state_changed=True,
                public_fallback_reply="存好了。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=handler,
                parameters=(GMToolParameter("slot", "string", "slot"),),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {"slot": 7},
                        "reply": "",
                        "reason": "首次参数类型错误。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {"slot": "第一幕结束"},
                        "reply": "",
                        "reason": "按回执修正参数。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "存好了，叫「第一幕结束」。",
                        "reason": "工具已经成功。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)

        outcome = agent.run(
            "@时悠 帮我存成第一幕结束",
            recent_context="阿凛刚结束了第一幕。",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.state_changed)
        self.assertEqual([receipt.ok for receipt in outcome.receipts], [False, True])
        self.assertEqual(calls, [{"slot": "第一幕结束"}])
        self.assertIn("第一幕结束", outcome.reply)

    def test_agent_reasks_model_for_one_json_before_executing_any_tool(self) -> None:
        calls: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="save_campaign",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="存好了。",
                    )
                ),
                parameters=(GMToolParameter("slot", "string", "slot", required=True),),
                side_effect="write",
            )
        )
        malformed = (
            '{"decision":"call_tool","tool_name":"save_campaign",'
            '"arguments":{"slot":"错误对象"}}'
            '{"decision":"not_applicable"}'
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    malformed,
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "save_campaign",
                            "arguments": {"slot": "正确对象"},
                            "reply": "",
                            "reason": "纠正为单个JSON。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "存好了。",
                            "reason": "保存完成。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"slot": "正确对象"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertTrue(outcome.receipts[0].ok)
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )

    def test_adjacent_tool_objects_are_validated_then_executed_as_one_batch(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        for name in ("commit_world", "record_line"):
            registry.register(
                GMToolDefinition(
                    name=name,
                    description=name,
                    handler=lambda _context, _arguments, tool_name=name: (
                        calls.append(tool_name)
                        or GMToolReceipt(
                            tool_name=tool_name,
                            ok=True,
                            state_changed=True,
                            public_fallback_reply=f"{tool_name}完成。",
                        )
                    ),
                    side_effect="write",
                )
            )
        raw = "".join(
            [
                '{"decision":"call_tool","tool_name":"commit_world","arguments":{}}',
                '{"decision":"call_tool","tool_name":"record_line","arguments":{}}',
                '{"decision":"final","reply":"世界和界限都记好了。"}',
            ]
        )
        agent = LLMGMToolAgent(
            ScriptedClient([raw]),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 记录世界和界限",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["commit_world", "record_line"])
        self.assertEqual(outcome.reply, "世界和界限都记好了。")
        self.assertEqual([item.ok for item in outcome.receipts], [True, True])
        self.assertEqual(outcome.trace[0]["decision"], "call_tools")

    def test_batch_stops_after_first_failed_receipt_before_later_side_effects(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="first",
                description="first",
                handler=lambda _context, _arguments: (
                    calls.append("first")
                    or GMToolReceipt(
                        tool_name="first",
                        ok=False,
                        error_code="REPAIR_ME",
                        retryable=True,
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="second",
                description="second",
                handler=lambda _context, _arguments: (
                    calls.append("second")
                    or GMToolReceipt(tool_name="second", ok=True, state_changed=True)
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "first", "arguments": {}},
                                {"tool_name": "second", "arguments": {}},
                            ],
                        }
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "第一步没通过，后面没有执行。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 执行两步",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["first"])
        self.assertFalse(outcome.state_changed)
        self.assertEqual(len(outcome.receipts), 1)

    def test_batch_collapses_redundant_major_location_projection(self) -> None:
        common = {
            "name": "边境驿站",
            "value": "边境驿站供大陆上的商旅和旅人歇脚。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的地点。",
            "source_event_id": "message-1",
        }
        decision = {
            "calls": [
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        **common,
                        "category": "major_locations",
                    },
                },
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        **common,
                        "category": "map_locations",
                        "attributes": {"feature_type": "settlement"},
                    },
                },
            ]
        }
        step: dict[str, object] = {}

        calls = LLMGMToolAgent._schedule_batch_calls(
            decision=decision,
            observed_state={},
            step=step,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["category"], "map_locations")
        self.assertEqual(
            step["skipped_projection_calls"][0]["name"],
            "边境驿站",
        )

    def test_batch_rewrites_kingdom_map_create_as_dependent_update(self) -> None:
        common = {
            "name": "钟鸣公国",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "记录玩家明确贡献的国家与方位。",
            "source_event_id": "message-kingdom-1",
        }
        decision = {
            "calls": [
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        **common,
                        "category": "map_locations",
                        "value": "钟鸣公国位于内海北岸。",
                        "attributes": {
                            "feature_type": "country",
                            "position_hint": "north",
                        },
                        "expected_revision": 2,
                    },
                },
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        **common,
                        "category": "kingdoms",
                        "value": "钟鸣公国以安抚亡魂的钟声闻名。",
                    },
                },
            ]
        }
        step: dict[str, object] = {}

        calls = LLMGMToolAgent._schedule_batch_calls(
            decision=decision,
            observed_state={},
            step=step,
        )

        self.assertEqual(
            [call["arguments"]["category"] for call in calls],
            ["kingdoms", "map_locations"],
        )
        self.assertEqual(calls[1]["tool_name"], "update_world_setting")
        self.assertNotIn("expected_revision", calls[1]["arguments"])
        self.assertEqual(
            step["rewritten_projection_calls"][0]["name"],
            "钟鸣公国",
        )

    def test_batch_does_not_rewrite_kingdom_map_with_different_authority(self) -> None:
        decision = {
            "calls": [
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        "category": "kingdoms",
                        "name": "钟鸣公国",
                        "value": "玩家确认的国家。",
                        "visibility": "public",
                        "authority": "player_confirmed",
                    },
                },
                {
                    "tool_name": "create_world_setting",
                    "arguments": {
                        "category": "map_locations",
                        "name": "钟鸣公国",
                        "value": "GM另行准备的位置。",
                        "visibility": "public",
                        "authority": "gm_authored",
                        "attributes": {"feature_type": "country"},
                    },
                },
            ]
        }
        step: dict[str, object] = {}

        calls = LLMGMToolAgent._schedule_batch_calls(
            decision=decision,
            observed_state={},
            step=step,
        )

        self.assertEqual(
            [call["tool_name"] for call in calls],
            ["create_world_setting", "create_world_setting"],
        )
        self.assertNotIn("rewritten_projection_calls", step)

    def test_rewritten_kingdom_map_batch_persists_both_projections(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            message = "钟鸣公国位于内海北岸，以安抚亡魂的钟声闻名。"
            context = execution_context(
                campaign_id="kingdom-map-projection",
                speaker="阿凛",
            )
            context.gate_status = "session_zero"
            context.metadata.update(
                {
                    "current_message": message,
                    "current_event_id": "message-kingdom-1",
                    "current_turn_events": [
                        {
                            "event_id": "message-kingdom-1",
                            "speaker": "阿凛",
                            "text": message,
                        }
                    ],
                }
            )
            decision = {
                "calls": [
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "category": "map_locations",
                            "name": "钟鸣公国",
                            "value": "钟鸣公国位于内海北岸。",
                            "visibility": "public",
                            "authority": "player_confirmed",
                            "reason": "记录玩家明确贡献的国家方位。",
                            "source_event_id": "message-kingdom-1",
                            "attributes": {
                                "feature_type": "country",
                                "position_hint": "north",
                            },
                        },
                    },
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "category": "kingdoms",
                            "name": "钟鸣公国",
                            "value": "钟鸣公国以安抚亡魂的钟声闻名。",
                            "visibility": "public",
                            "authority": "player_confirmed",
                            "reason": "记录玩家明确贡献的国家。",
                            "source_event_id": "message-kingdom-1",
                        },
                    },
                ]
            }
            calls = LLMGMToolAgent._schedule_batch_calls(
                decision=decision,
                observed_state={},
                step={},
            )

            receipts = [
                service.gm_tool_registry.execute(
                    call["tool_name"],
                    call["arguments"],
                    context,
                )
                for call in calls
            ]

            self.assertTrue(all(receipt.ok for receipt in receipts))
            runtime = service._runtime("kingdom-map-projection")
            self.assertEqual(
                runtime.app.world_state.world_profile.kingdoms["钟鸣公国"],
                "钟鸣公国以安抚亡魂的钟声闻名。",
            )
            location = runtime.app.world_state.map_locations["钟鸣公国"]
            self.assertEqual(location.feature_type, "country")
            self.assertEqual(location.position_hint, "north")

    def test_replace_state_tool_must_run_before_other_batch_writes(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()

        def load_handler(_context, _arguments):
            calls.append("load")
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=True,
                result={"active_campaign_id": "旧团"},
                state_changed=True,
                public_fallback_reply="已经读回旧团。",
            )

        def write_handler(_context, _arguments):
            calls.append("write")
            return GMToolReceipt(
                tool_name="commit_note",
                ok=True,
                state_changed=True,
                public_fallback_reply="设定已写入。",
            )

        registry.register(
            GMToolDefinition(
                name="load_campaign",
                description="load",
                handler=load_handler,
                side_effect="replace_state",
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_note",
                description="write",
                handler=write_handler,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {
                                    "tool_name": "load_campaign",
                                    "arguments": {},
                                },
                                {
                                    "tool_name": "commit_note",
                                    "arguments": {},
                                },
                            ],
                            "terminal_decision": "final",
                            "reply": "读档并修改完成。",
                            "reason": "错误地按旧状态规划同批写入。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "load_campaign",
                            "arguments": {},
                            "reply": "",
                            "reason": "先单独读取战役。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "已经读回旧团。",
                            "reason": "等拿到新状态后再修改。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "读取旧团，之后再改设定。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["load"])
        self.assertEqual(
            outcome.trace[0]["protocol_error"],
            "REPLACE_STATE_BATCH_MUST_BE_ISOLATED",
        )
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "已经读回旧团。")

    def test_batch_rolls_back_preparatory_write_when_main_action_fails(self) -> None:
        state: list[str] = []
        action_attempts = 0

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="focus",
                handler=lambda _context, _arguments: (
                    state.append("focused")
                    or GMToolReceipt.success(
                        "focus_scene_branch",
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )

        def perform(_context, arguments):
            nonlocal action_attempts
            action_attempts += 1
            if not arguments.get("valid"):
                return GMToolReceipt.failure(
                    "perform_character_action",
                    "UNKNOWN_ARGUMENT",
                    "法术参数需要修正。",
                    "补齐标准参数后重试。",
                )
            state.append("acted")
            return GMToolReceipt.success(
                "perform_character_action",
                state_changed=True,
                public_reply="行动已结算。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="perform_character_action",
                description="act",
                handler=perform,
                parameters=(GMToolParameter("valid", "boolean", "valid"),),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "focus_scene_branch", "arguments": {}},
                            {"tool_name": "perform_character_action", "arguments": {}},
                        ],
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "focus_scene_branch", "arguments": {}},
                            {
                                "tool_name": "perform_character_action",
                                "arguments": {"valid": True},
                            },
                        ],
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠 施法",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(state, ["focused", "acted"])
        self.assertEqual(action_attempts, 2)
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "行动已结算。")
        rollback = outcome.trace[0]["batch_receipts"][0]
        self.assertTrue(rollback["result"]["rolled_back"])

    def test_schema_failure_must_retry_same_tool_before_other_writes(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit world",
                parameters=(
                    GMToolParameter("updates", "object", "world updates", required=True),
                ),
                handler=lambda _context, arguments: (
                    calls.append(("commit_world", dict(arguments)))
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="世界记好了。",
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="record_line",
                description="record line",
                handler=lambda _context, arguments: (
                    calls.append(("record_line", dict(arguments)))
                    or GMToolReceipt(
                        tool_name="record_line",
                        ok=True,
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_world",
                            "arguments": {
                                "updates": {"continent_name": "白钟大陆"},
                                "reason": "说明文字不应成为参数",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "record_line",
                            "arguments": {},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {
                                    "tool_name": "commit_world",
                                    "arguments": {
                                        "updates": {"continent_name": "白钟大陆"}
                                    },
                                },
                                {"tool_name": "record_line", "arguments": {}},
                            ],
                            "terminal_decision": "final",
                            "reply": "都记好了。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 记录世界和界限",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            calls,
            [
                ("commit_world", {"updates": {"continent_name": "白钟大陆"}}),
                ("record_line", {}),
            ],
        )
        self.assertEqual(outcome.reply, "都记好了。")
        self.assertEqual(outcome.trace[1]["protocol_error"], "SCHEMA_RETRY_TOOL_OMITTED")

    def test_same_tool_schema_retry_succeeds_after_corrected_arguments(self) -> None:
        committed: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit world",
                parameters=(
                    GMToolParameter("value", "string", "world value", required=True),
                ),
                handler=lambda _context, arguments: (
                    committed.append(str(arguments["value"]))
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="世界设定已记下。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "commit_world",
                        "arguments": {"wrong_field": "白钟大陆"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "commit_world",
                        "arguments": {"value": "白钟大陆"},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()

        outcome = agent.run(
            "@时悠 记下白钟大陆。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(committed, ["白钟大陆"])
        self.assertEqual(outcome.reply, "世界设定已记下。")
        self.assertEqual(len(outcome.trace), 2)
        retry_request = json.loads(client.calls[1]["messages"][-1].content)
        retry_issues = retry_request["runtime_feedback"]["issues"]
        self.assertEqual(
            [item["code"] for item in retry_issues],
            ["TOOL_RETRY_REQUIRED"],
        )
        self.assertEqual(retry_issues[0]["tool_name"], "commit_world")

    def test_discovered_but_unsupported_capability_can_end_with_honest_final(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="discover_capabilities",
                description="discover optional capabilities",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="discover_capabilities",
                    ok=True,
                    result={"capability_candidates": ["inspect_campaign"]},
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "discover_capabilities",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "当前工具只能查看存档，不能重启章节；本轮没有执行任何改动。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context(speaker="澄砚")
        context.gate_status = "adventure"

        outcome = agent.run(
            "请重启这一章。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "当前工具只能查看存档，不能重启章节；本轮没有执行任何改动。",
        )
        self.assertEqual(len(outcome.receipts), 1)
        self.assertFalse(outcome.receipts[0].state_changed)

    def test_capability_candidates_can_ask_for_missing_campaign_id_in_two_turns(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="discover_capabilities",
                description="discover optional campaign tools",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "discover_capabilities",
                    result={
                        "capability_candidates": [
                            "list_saves",
                            "inspect_campaign",
                        ]
                    },
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "tool_name": "discover_capabilities",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "ask_user",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "reply": "你想查看哪一个战役存档？请告诉我存档名。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.gate_status = "adventure"

        outcome = agent.run(
            "帮我查看存档。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "你想查看哪一个战役存档？请告诉我存档名。",
        )
        self.assertEqual(len(outcome.trace), 2)
        self.assertFalse(outcome.receipts[0].state_changed)
        self.assertFalse(
            any(
                step.get("protocol_error") == "REQUIRED_FOLLOWUP_PENDING"
                for step in outcome.trace
            )
        )

    def test_free_scene_beat_never_starts_npc_retry_transactions(self) -> None:
        attempts: list[int] = []
        registry = GMToolRegistry()

        def fail_npc(_context, _arguments):
            attempts.append(1)
            return GMToolReceipt(
                tool_name="decide_npc_action",
                ok=False,
                error_code="NPC_RESPONSE_TRANSACTION_INVALID",
                message="GM提交的NPC行动没有承接最新公开事实。",
                correction_hint="保留同一NPC，依据最新公开事实重提事务。",
                retryable=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_action",
                description="npc beat",
                handler=fail_npc,
                side_effect="write",
            )
        )
        response = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "decide_npc_action",
                "arguments": {},
            }
        )
        client = ScriptedClient([response, response, response, response])
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
            }
        )

        outcome = agent.run(
            "推进当前局面",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(attempts, [])
        self.assertEqual(outcome.target, "silent")

    def test_agent_never_executes_the_same_successful_write_twice(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这条设定记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
            )
        )
        repeated_call = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "commit_world",
                "arguments": {"value": "沉默森林"},
            },
            ensure_ascii=False,
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    repeated_call,
                    repeated_call,
                    json.dumps(
                        {
                            "decision": "final",
                            "reply": "沉默森林记下了。",
                            "reason": "写入已经成功。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "我贡献沉默森林。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"value": "沉默森林"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "沉默森林记下了。")
        self.assertTrue(
            any(
                item.get("protocol_error") == "DUPLICATE_SUCCESSFUL_TOOL_CALL"
                for item in outcome.trace
            )
        )

    def test_recovery_batch_skips_completed_write_and_commits_new_write(self) -> None:
        calls: list[str] = []
        registry = GMToolRegistry()

        def commit(_context, arguments):
            value = str(arguments["value"])
            calls.append(value)
            result: dict[str, object] = {
                "value": value,
                "silent_commit_allowed": True,
            }
            if value == "已完成的设定":
                result.update(
                    {
                        "required_followup_tools": ["commit_world"],
                        "required_followup_calls": [
                            {
                                "tool_name": "commit_world",
                                "arguments": {"value": "尚未完成的设定"},
                            }
                        ],
                        "required_followup_mode": "all",
                    }
                )
            return GMToolReceipt.success(
                "commit_world",
                result=result,
                state_changed=True,
            )

        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit",
                handler=commit,
                parameters=(
                    GMToolParameter("value", "string", "value", required=True),
                ),
                side_effect="write",
                max_successful_calls_per_message=4,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_world",
                        "arguments": {"value": "已完成的设定"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "commit_world",
                                "arguments": {"value": "已完成的设定"},
                            },
                            {
                                "tool_name": "commit_world",
                                "arguments": {"value": "尚未完成的设定"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        context = execution_context()
        context.directly_addressed = False
        outcome = agent.run(
            "请把两项设定都记下。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(calls, ["已完成的设定", "尚未完成的设定"])
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertEqual(len(outcome.receipts), 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            outcome.trace[1]["skipped_duplicate_calls"][0]["tool_name"],
            "commit_world",
        )

    def test_repeated_duplicate_write_falls_back_without_a_second_side_effect(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_world",
                description="commit",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="commit_world",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="这条设定记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
            )
        )
        repeated_call = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "commit_world",
                "arguments": {"value": "沉默森林"},
            },
            ensure_ascii=False,
        )
        agent = LLMGMToolAgent(
            ScriptedClient([repeated_call, repeated_call, repeated_call]),
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "我贡献沉默森林。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, [{"value": "沉默森林"}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "这条设定记下了。")

    def test_npc_response_without_authorized_followup_finishes_immediately(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()

        def answer(_context, arguments):
            calls.append(dict(arguments))
            return GMToolReceipt(
                tool_name="decide_npc_response",
                ok=True,
                state_changed=True,
                public_fallback_reply="会长摇头：条件还没有补齐。",
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=answer,
                parameters=(
                    GMToolParameter("instruction", "string", "instruction", required=True),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {"instruction": "回应尚未满足的条件"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {"instruction": "换个说法再次回应条件"},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "不应执行到这里。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)

        outcome = agent.run(
            "伊莉雅回应会长的放行条件。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "会长摇头：条件还没有补齐。")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(outcome.trace[-1]["tool_name"], "decide_npc_response")

    def test_repeated_invalid_json_fails_closed_without_state_change(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(tool_name="save_campaign", ok=True, state_changed=True)
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(["not-json", "still-not-json"])
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unavailable")
        self.assertFalse(outcome.state_changed)
        self.assertEqual(calls, [])
        self.assertEqual(
            sum(item.get("phase") == "parse_recovery" for item in outcome.trace),
            2,
        )
        retry_messages = client.calls[1]["messages"]
        self.assertEqual([item.role for item in retry_messages], ["system", "user"])
        repair_instruction = retry_messages[0]
        self.assertIn("不是玩家消息", repair_instruction.content)
        self.assertIn("绝不向玩家提及", repair_instruction.content)
        self.assertIn("不得增加、删除或改换工具调用", repair_instruction.content)

    def test_readable_invalid_batch_is_returned_to_agent_before_any_call_executes(self) -> None:
        writes: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="save",
                handler=lambda _context, _arguments: (
                    writes.append("saved")
                    or GMToolReceipt(
                        tool_name="save_campaign",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="存好了。",
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "save_campaign", "arguments": {}},
                            {"arguments": {"slot": "不应猜测"}},
                        ],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "save_campaign",
                        "arguments": {},
                        "terminal_decision": "final",
                        "reply": "存好了。",
                        "reason": "根据协议错误重新提交完整决策。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="semantic-model",
            protocol_repair_model="syntax-model",
            registry=registry,
            max_iterations=3,
        )

        outcome = agent.run(
            "@时悠 存档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(writes, ["saved"])
        self.assertEqual(outcome.reply, "存好了。")
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )
        second_request = json.loads(client.calls[1]["messages"][1].content)
        protocol_error = second_request["history"][-1]["protocol_error"]
        self.assertEqual(
            protocol_error["error_code"],
            "INVALID_AGENT_TOOL_PROTOCOL",
        )
        self.assertIn("calls[2]缺少tool_name", protocol_error["message"])
        self.assertTrue(protocol_error["invalid_protocol_draft_discarded"])
        self.assertNotIn("invalid_protocol_draft", protocol_error)

    def test_active_group_model_failure_is_silent_but_still_owned(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "谁方便盯外面，谁继续和会长谈？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unavailable_silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_provider_failure_reviews_unaddressed_action_before_silencing(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        verifier = FailureReplyObligationVerifier(requires_gm_reply=True)
        agent = LLMGMToolAgent(
            TransportFailureClient(),
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=verifier,
            timeout_seconds=2,
        )

        outcome = agent.run(
            "艾丽妮接过铁片，贴着锁孔探进去。",
            recent_context="诺艾尔把铁片抛到了艾丽妮牢房一侧。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.mode, "gm_agent_unavailable")
        self.assertIn("没有结算", outcome.reply)
        self.assertIn("原样重发", outcome.reply)
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(
            verifier.calls[0]["current_message"],
            "艾丽妮接过铁片，贴着锁孔探进去。",
        )
        self.assertTrue(
            any(
                item.get("phase")
                == "provider_failure_reply_obligation_review"
                for item in outcome.trace
            )
        )

    def test_provider_failure_keeps_verified_player_discussion_silent(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        verifier = FailureReplyObligationVerifier(requires_gm_reply=False)
        agent = LLMGMToolAgent(
            TransportFailureClient(),
            model="fake",
            registry=GMToolRegistry(),
            reply_grounding_verifier=verifier,
            timeout_seconds=2,
        )

        outcome = agent.run(
            "或许她觉得这件小事不需要检定。",
            recent_context="艾丽妮正在尝试开锁。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unavailable_silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(verifier.calls), 1)

    def test_failed_player_action_is_not_silenced_when_model_then_fails(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="结算玩家已执行的场景行动。",
                handler=lambda _context, _arguments: GMToolReceipt.failure(
                    "perform_in_scene_action",
                    "ACTION_NOT_LEGAL",
                    "当前局面无法完成这项行动。",
                    "保留原行动意图，改用合法参数重新提交。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "performed_action",
                        "audience": "table",
                        "tool_name": "perform_in_scene_action",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        context = execution_context()
        context.directly_addressed = False
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "伊莉雅猛推已经卡死的铁门。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("没有结算", outcome.reply)
        self.assertIn("原样重发", outcome.reply)
        self.assertFalse(outcome.state_changed)

    def test_system_beat_model_failure_never_emits_player_facing_error(self) -> None:
        context = execution_context()
        context.metadata["system_gm_beat_request"] = True
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "系统要求判断是否推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.mode, "gm_agent_unavailable_silent")
        self.assertEqual(outcome.reply, "")

    def test_inactive_unaddressed_model_failure_can_route_external(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        context.gate_status = "inactive"
        agent = LLMGMToolAgent(
            ScriptedClient(["not-json", "still-not-json"]),
            model="fake",
            registry=GMToolRegistry(),
            parse_retries=1,
        )

        outcome = agent.run(
            "今晚天气怎么样？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertFalse(outcome.handled)

    def test_parse_repair_isolated_from_world_state_and_original_request(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_scene_action",
                description="commit",
                handler=lambda _context, arguments: GMToolReceipt(
                    tool_name="perform_scene_action",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=str(arguments["public_reply"]),
                    lock_public_reply=True,
                ),
                parameters=(
                    GMToolParameter("public_reply", "string", "reply", required=True),
                ),
                side_effect="write",
            )
        )
        malformed = (
            '{"decision":"call_tool","tool_name":"perform_scene_action",'
            '"arguments":{"public_reply":"守路人接过纸。"},reason:"交付成立"}'
        )
        repaired = json.dumps(
            {
                "decision": "call_tool",
                "tool_name": "perform_scene_action",
                "arguments": {"public_reply": "守路人接过纸。"},
                "reason": "交付成立",
            },
            ensure_ascii=False,
        )
        client = ScriptedClient(
            [
                malformed,
                repaired,
                json.dumps(
                    {"decision": "final", "reply": "", "reason": "已提交。"},
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "苍祈把纸递给守路人。",
            recent_context="这里有一段不应交给修复器的长聊天。",
            context=execution_context(),
            state_summary={"private_secret": "不能进入语法修复请求"},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "守路人接过纸。")
        repair_messages = client.calls[1]["messages"]
        joined = "\n".join(item.content for item in repair_messages)
        repair_payload = json.loads(repair_messages[1].content)
        self.assertEqual(repair_payload["malformed_protocol_draft"], malformed)
        self.assertNotIn("苍祈把纸递给守路人", joined)
        self.assertNotIn("private_secret", joined)
        self.assertNotIn("长聊天", joined)

    def test_agent_requests_sufficient_tokens_for_structured_tool_decision(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "我在。",
                        "reason": "直接回应。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
            max_output_tokens=4096,
        )

        outcome = agent.run(
            "@时悠 在吗",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(client.calls[0]["max_tokens"], 4096)

    def test_agent_allows_only_one_successful_limited_write_per_message(self) -> None:
        calls: list[dict[str, object]] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_draft",
                description="update once",
                handler=lambda _context, arguments: (
                    calls.append(arguments)
                    or GMToolReceipt(
                        tool_name="update_draft",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="记下了。",
                    )
                ),
                parameters=(GMToolParameter("value", "string", "value", required=True),),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_draft",
                        "arguments": {"value": "第一次"},
                        "reply": "",
                        "reason": "写入。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_draft",
                        "arguments": {"value": "第二次"},
                        "reply": "",
                        "reason": "又改一次。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "记下了。",
                        "reason": "遵循调用上限。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=4,
        )

        outcome = agent.run(
            "只改一次",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(calls, [{"value": "第一次"}])
        self.assertTrue(
            any(
                item.get("protocol_error") == "TOOL_CALL_LIMIT_REACHED"
                for item in outcome.trace
            )
        )

    def test_parse_failure_after_successful_write_uses_receipt_without_agent_error(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="这条设定记下了。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "先写入一部分。",
                        },
                        ensure_ascii=False,
                    ),
                    "not-json",
                    "still-not-json",
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "@时悠 记下这一整段复合设定",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "这条设定记下了。")
        self.assertEqual(outcome.error, "")
        self.assertEqual(outcome.mode, "gm_agent_tool")

    def test_iteration_limit_after_write_never_exposes_protocol_recovery_text(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="白蜡封片裂开，露出一截旧登记条。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交公开变化。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
            max_iterations=1,
        )

        outcome = agent.run(
            "系统主动节拍要求局面发生变化。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "白蜡封片裂开，露出一截旧登记条。")
        self.assertNotIn("还没记下", outcome.reply)
        self.assertNotIn("没有完成", outcome.reply)

    def test_parse_failure_after_corrected_write_is_recovered(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_part",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_part",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="最后一项技能记下了。",
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "不存在的包装工具",
                            "arguments": {},
                            "reply": "",
                            "reason": "协议包装错误。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_part",
                            "arguments": {},
                            "reply": "",
                            "reason": "根据失败回执改用正确工具。",
                        },
                        ensure_ascii=False,
                    ),
                    "not-json",
                    "still-not-json",
                ]
            ),
            model="fake",
            registry=registry,
            parse_retries=1,
        )

        outcome = agent.run(
            "洛岚最后一项技能选破防打击。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual([receipt.ok for receipt in outcome.receipts], [False, True])
        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "最后一项技能记下了。")
        self.assertEqual(outcome.error, "")

    def test_successful_gate_receipt_refreshes_context_for_next_batch_tool(self) -> None:
        observed_gate_statuses: list[str] = []
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="open_gate",
                description="open",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="open_gate",
                    ok=True,
                    result={"gate": {"campaign_id": "agent-test", "status": "adventure"}},
                    state_changed=True,
                    public_fallback_reply="第一章开始了。",
                ),
                side_effect="write",
            )
        )

        def start_scene(context, _arguments):
            observed_gate_statuses.append(context.gate_status)
            return GMToolReceipt(
                tool_name="start_scene",
                ok=context.gate_status == "adventure",
                state_changed=context.gate_status == "adventure",
                public_fallback_reply="风铃廊出现在眼前。",
            )

        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=start_scene,
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "open_gate", "arguments": {}},
                                {"tool_name": "start_scene", "arguments": {}},
                            ],
                            "terminal_decision": "final",
                            "reply": "第一章开始了，风铃廊出现在眼前。",
                            "reason": "依次进入冒险并建立场景。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"
        state_summary = {"runtime": {"gate": {"status": "session_zero"}}}

        outcome = agent.run(
            "大家确认进入第一章。",
            recent_context="",
            context=context,
            state_summary=state_summary,
        )

        self.assertEqual(observed_gate_statuses, ["adventure"])
        self.assertTrue(all(receipt.ok for receipt in outcome.receipts))
        self.assertEqual(context.gate_status, "adventure")
        self.assertEqual(state_summary["runtime"]["gate"]["status"], "adventure")

    def test_not_applicable_in_active_session_fails_closed_without_legacy_fallback(self) -> None:
        registry = GMToolRegistry()
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "not_applicable",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "",
                            "reason": "这是角色行动，应交给跑团流程。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅推开旧路闸门。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unavailable")
        self.assertIn("没有结算", outcome.reply)
        self.assertIn("原样重发", outcome.reply)

    def test_not_applicable_in_active_group_exhaustion_stays_silent(self) -> None:
        context = execution_context()
        context.directly_addressed = False
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "not_applicable",
                            "reason": "错误地尝试交给旧流程。",
                        },
                        ensure_ascii=False,
                    )
                    for _ in range(2)
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
            max_iterations=2,
        )

        outcome = agent.run(
            "伊莉雅推开旧路闸门。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_unresolved_silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_agent_can_authoritatively_keep_player_discussion_silent(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "",
                        "reason": "玩家正在彼此商量分工，尚未声明行动。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
        )

        context = execution_context()
        context.directly_addressed = False
        outcome = agent.run(
            "谁方便盯外面，谁继续和会长谈？",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertTrue(outcome.stop_astrbot)
        self.assertEqual(outcome.reply, "")
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("纯玩家间对话、商量和玩笑", system_prompt)
        self.assertIn("向队友概括当前局面", system_prompt)
        self.assertIn("谁更适合", system_prompt)
        self.assertIn("ask_user仅用于GM请求缺少执行必需参数", system_prompt)
        self.assertIn("玩家对话在聊天记录中原样继续", system_prompt)
        self.assertIn("发言人、建议、行动与闲聊沿用各自原始事件", system_prompt)
        self.assertIn("权威current_actor恰好是NPC时", system_prompt)
        self.assertIn("玩家讨论本身仍不触发run_current_npc_turn", system_prompt)

    def test_unaddressed_table_reply_is_returned_to_agent_for_silence(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "final",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reply": "由伊莉雅负责最合适。",
                        "reason": "玩家正在商量登记分工。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reply": "",
                        "reason": "这是玩家之间的分工讨论，GM不替他们决定。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "我赞成先登记；登记由谁来负责比较合适？",
            recent_context="伊莉雅已经走进登记小室。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(client.calls), 2)
        retry_history = client.calls[1]["messages"][1].content
        self.assertIn("UNADDRESSED_TABLE_TALK_SHOULD_STAY_SILENT", retry_history)

    def test_in_character_speech_to_another_pc_stays_silent_without_state_write(
        self,
    ) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "message_kind": "discussion",
                        "audience": "players",
                        "reason": "这是PC之间的角色内对话，公开聊天已保存，不替任何一方回应。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            (
                "苍祈压低声音对艾薇娅说：“我愿意承担失忆旅人的同行照看；"
                "若遇袭或旅人要求停下，我会立即撤回。请把这份承诺转告会长。”"
            ),
            recent_context="会长要求队伍报备护送人选。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertFalse(outcome.state_changed)
        self.assertEqual(len(client.calls), 1)
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("纯对话、提问、玩笑、商议", system_prompt)
        self.assertIn("归类为discussion并选择silent", system_prompt)

    def test_silent_scene_pass_suppresses_model_paraphrase_after_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="pass_in_scene_action",
                description="记录普通场景行动轮中的明确略过。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "pass_in_scene_action",
                    result={"silent_commit_allowed": True},
                    state_changed=True,
                ),
                parameters=(
                    GMToolParameter("actor", "string", "行动角色。", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "pass_in_scene_action",
                        "arguments": {"actor": "伊莉雅"},
                        "reason": "记录本轮略过。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "table",
                        "reply": "伊莉雅暂时让出本轮行动，安静守在原地。",
                        "reason": "复述玩家的略过。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "伊莉雅暂时不采取行动。",
            recent_context="巡逻队刚刚退向闸门。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertTrue(outcome.state_changed)

    def test_session_zero_answer_commit_does_not_force_same_request_next_question(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero_draft",
                description="登记玩家本轮明确给出的角色草稿增量。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "update_hero_draft",
                    result={
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                        "completion_scope": "source_statement",
                        "changed_fields": ["classes"],
                        "player_name": "南星",
                        "hero_name": "赛璃",
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "state_contribution",
                        "has_independent_followup": False,
                        "audience": "table",
                        "tool_name": "update_hero_draft",
                        "arguments": {},
                        "reason": "登记本轮职业等级选择。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        verifier = ReceiptAwareSessionZeroCompletionVerifier()
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            reply_grounding_verifier=verifier,
        )
        context = execution_context(speaker="南星")
        context.gate_status = "session_zero"
        context.directly_addressed = False

        outcome = agent.run(
            "御魂使3级、旅人2级。",
            recent_context="时悠：五个初始等级想怎样分配？",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(
            verifier.calls[0]["completed_receipts"][0].result[
                "completion_scope"
            ],
            "source_statement",
        )

    def test_directly_addressed_silent_capable_write_still_requires_reply(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="record_local_note",
                description="记录本地状态。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "record_local_note",
                    result={"silent_commit_allowed": True},
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "gm",
                        "tool_name": "record_local_note",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "尝试静默。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "记下了。",
                        "reason": "直接询问需要回应。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = True

        outcome = agent.run(
            "@时悠，记一下。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "记下了。")

    def test_game_turn_can_silently_commit_player_action_already_public(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="登记玩家已经公开说完的本地行动。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_in_scene_action",
                    result={
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "perform_in_scene_action",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "玩家自己的公开行动已经是完整表达。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = True

        outcome = agent.run(
            "伊莉雅走到门边站定。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertTrue(outcome.state_changed)

    def test_scene_focus_recovery_does_not_force_echo_of_public_player_action(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="恢复玩家角色所在的既有并行镜头。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "focus_scene_branch",
                    result={
                        "silent_commit_neutral": True,
                        "required_followup_tools": ["perform_in_scene_action"],
                        "allowed_followup_tools": ["perform_in_scene_action"],
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_in_scene_action",
                description="登记玩家已经公开说完的本地行动。",
                handler=lambda _context, _arguments: GMToolReceipt.success(
                    "perform_in_scene_action",
                    result={
                        "silent_commit_allowed": True,
                        "source_message_already_public": True,
                    },
                    state_changed=True,
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "focus_scene_branch",
                        "arguments": {},
                        "reason": "先恢复赛璃所在的并行镜头。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "table",
                        "tool_name": "perform_in_scene_action",
                        "arguments": {},
                        "terminal_decision": "silent",
                        "reason": "玩家的确定性行动已经完整公开。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.directly_addressed = True

        outcome = agent.run(
            "赛璃顺着旧谱的节奏，一点点放缓钟绳。",
            recent_context="当前镜头在伊莉雅所在的领航钟架。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(outcome.mode, "gm_agent_silent_commit")
        self.assertEqual(
            [receipt.tool_name for receipt in outcome.receipts],
            ["focus_scene_branch", "perform_in_scene_action"],
        )

    def test_neutral_scene_focus_cannot_authorize_silence_by_itself(self) -> None:
        receipt = GMToolReceipt.success(
            "focus_scene_branch",
            result={"silent_commit_neutral": True},
            state_changed=True,
        )
        context = execution_context()
        context.directly_addressed = True

        self.assertFalse(
            LLMGMToolAgent._mutations_can_commit_silently(
                [receipt],
                context=context,
            )
        )

    def test_unaddressed_session_zero_table_proposal_is_persisted_but_silent(self) -> None:
        writes: list[dict[str, object]] = []

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="propose_session_zero_update",
                description="保存正在征求同伴意见的具体待定提案。",
                handler=lambda _context, arguments: (
                    writes.append(dict(arguments))
                    or GMToolReceipt.success(
                        "propose_session_zero_update",
                        result={
                            "silent_commit_allowed": True,
                            "source_message_already_public": True,
                        },
                        state_changed=True,
                    )
                ),
                parameters=(
                    GMToolParameter("summary", "string", "待定提案摘要。", required=True),
                ),
                side_effect="write_pending",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "audience": "players",
                        "tool_name": "propose_session_zero_update",
                        "arguments": {"summary": "第一幕从白花碑驿站开始"},
                        "terminal_decision": "silent",
                        "reason": "保存具体提案但不打断玩家讨论。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
        )
        context = execution_context()
        context.gate_status = "session_zero"
        context.directly_addressed = False

        outcome = agent.run(
            "第一幕我提议从白花碑驿站开始，大家觉得呢？",
            recent_context="",
            context=context,
            state_summary={"session_zero": {"pending_proposals": []}},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(writes, [{"summary": "第一幕从白花碑驿站开始"}])
        self.assertEqual(len(outcome.receipts), 1)
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("才用propose_session_zero_update保存为待定提案", system_prompt)
        self.assertIn("可选择terminal_decision=silent", system_prompt)
        self.assertIn("绝不当作共识", system_prompt)
        self.assertIn("该双人明确证据足以形成共识", system_prompt)
        self.assertIn("绝不能代拟、默认或提议玩家的安全界限与帷幕", system_prompt)

    def test_recent_context_pronoun_address_to_gm_cannot_finish_silent(self) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "audience": "gm",
                        "reason": "错误地把这句当成玩家闲聊。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "audience": "gm",
                        "reply": "是有点坏，我都问他三回啦。",
                        "reason": "结合上一句可知“你”指时悠。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            "他很坏啊都不理你",
            recent_context=(
                "时悠: 测试玩家乙，如果暂时没灵感，也可以只留下一件怪事；"
                "想不到的话，先跳过世界奥秘也可以。"
            ),
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.reply, "是有点坏，我都问他三回啦。")
        retry_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(
            retry_request["history"][-1]["protocol_error"]["error_code"],
            "SEMANTICALLY_ADDRESSED_MESSAGE_REQUIRES_REPLY",
        )
        self.assertIn(
            "称呼、代词、省略主语、引用与最近问答必须结合上下文解析",
            client.calls[0]["messages"][0].content,
        )

    def test_agent_can_answer_without_tool_when_no_state_changes(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "命刻用来追踪复杂目标或逐步逼近的威胁。",
                            "reason": "这是纯规则解释，不修改团状态。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )

        outcome = agent.run(
            "@时悠，命刻是什么？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertEqual(outcome.mode, "gm_agent_reply")
        self.assertEqual(outcome.receipts, [])

    def test_agent_silences_an_exact_echo_of_a_non_addressed_player_pass(self) -> None:
        message = "伊莉雅暂时不采取行动。"
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": message,
                            "reason": "错误地复述玩家。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )
        context = execution_context()
        context.directly_addressed = False

        outcome = agent.run(
            message,
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertTrue(outcome.stop_astrbot)

    def test_platform_address_prevents_agent_silence(self) -> None:
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "silent",
                            "audience": "gm",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "",
                            "reason": "误判为玩家闲聊。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "我在，刚才你是在问我。",
                            "reason": "平台确认玩家直接点名了时悠。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=GMToolRegistry(),
        )

        outcome = agent.run(
            "@时悠，你在吗？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.target, "fu_gm")
        self.assertIn("我在", outcome.reply)
        self.assertEqual(len(outcome.trace), 2)

    def test_failed_receipt_cannot_be_rewritten_as_success_by_model(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt(
                tool_name="load_campaign",
                ok=False,
                error_code="SAVE_SLOT_NOT_FOUND",
                message="没有这个存档。",
                retryable=False,
                public_fallback_reply="没有找到这个存档，当前进度没有改动。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="load_campaign",
                description="load",
                handler=handler,
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "load_campaign",
                            "arguments": {},
                            "reply": "",
                            "reason": "尝试读取。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "读档成功。",
                            "reason": "错误地把失败当成成功。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "@时悠 读档",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertFalse(outcome.state_changed)
        self.assertNotIn("成功", outcome.reply)
        self.assertIn("没有改动", outcome.reply)

    def test_nonretryable_tool_rejection_stops_after_one_model_decision(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt.failure(
                "prepare_solo_adventure",
                "CAMPAIGN_IS_NOT_SOLO",
                "当前存档包含其他玩家。",
                "改用普通第零章流程。",
                retryable=False,
                public_reply="这个存档里还有其他玩家，不能由一人替大家补完。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="prepare_solo_adventure",
                description="prepare solo adventure",
                handler=handler,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "risk_tier": "commit",
                        "tool_name": "prepare_solo_adventure",
                        "arguments": {},
                        "reason": "执行玩家的单人开章委托。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "@时悠，剩下的你补完，然后开始第一章。",
            recent_context="",
            context=GMToolExecutionContext(
                campaign_id="agent-test",
                session_id="s0",
                channel_id="private:100000001",
                speaker="阿凛",
                gate_status="session_zero",
                is_private=True,
                directly_addressed=True,
            ),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.mode, "gm_agent_rule_rejected")
        self.assertIn("还有其他玩家", outcome.reply)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(outcome.state_changed)

    def test_nonretryable_owner_rejection_is_not_overwritten_by_integrity_rollback(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt.failure(
                "update_hero_draft",
                "HERO_DRAFT_UPDATE_NOT_OWNER",
                "村夫不能修改loading的角色草稿。",
                "只允许角色所属玩家本人修改。",
                retryable=False,
                public_reply="这张角色草稿只能由所属玩家本人修改。",
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="update_hero_draft",
                description="更新角色草稿。",
                handler=handler,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "audience": "gm",
                        "risk_tier": "commit",
                        "tool_name": "update_hero_draft",
                        "arguments": {},
                        "reason": "尝试执行第三方代改。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        tool_context = execution_context()
        tool_context.speaker = "村夫"
        tool_context.gate_status = "session_zero"

        outcome = agent.run(
            "@时悠 伊大石的职业改为守护者4级+元素使1级，能做到吗？",
            recent_context="伊大石是loading的角色。",
            context=tool_context,
            state_summary={},
        )

        self.assertEqual(outcome.mode, "gm_agent_rule_rejected")
        self.assertEqual(outcome.reply, "这张角色草稿只能由所属玩家本人修改。")
        self.assertNotIn("没有处理完整", outcome.reply)
        self.assertEqual(len(client.calls), 1)

    def test_locked_public_reply_cannot_be_paraphrased_after_fact_commit(self) -> None:
        def handler(_context, _arguments):
            return GMToolReceipt(
                tool_name="perform_scene_action",
                ok=True,
                result={"public_facts": ["巡守把钥匙收回腰间。"]},
                state_changed=True,
                public_fallback_reply="巡守把钥匙收回腰间。",
                lock_public_reply=True,
            )

        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="perform_scene_action",
                description="commit scene facts and reply",
                handler=handler,
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "perform_scene_action",
                            "arguments": {},
                            "reply": "",
                            "reason": "提交公开场景回应。",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "decision": "final",
                            "tool_name": "",
                            "arguments": {},
                            "reply": "巡守已经把钥匙交给了英雄。",
                            "reason": "错误地改写了事实。",
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "我示意巡守收好钥匙。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply, "巡守把钥匙收回腰间。")
        self.assertNotIn("交给", outcome.reply)

    def test_multiple_successful_locked_replies_are_preserved_in_tool_order(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    result={"allowed_followup_tools": ["start_scene"]},
                    state_changed=True,
                    public_fallback_reply="会长点头：‘我带你们过去。’",
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="一行人穿过门洞，抵达旧路第一处界碑。",
                    lock_public_reply=True,
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps({"decision": "call_tool", "tool_name": "decide_npc_response", "arguments": {}}),
                    json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
                    json.dumps({"decision": "final", "tool_name": "", "arguments": {}, "reply": "已经出发。"}),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "请会长带我们去旧路界碑。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "会长点头：‘我带你们过去。’\n一行人穿过门洞，抵达旧路第一处界碑。",
        )
        self.assertEqual([item.tool_name for item in outcome.receipts], ["decide_npc_response", "start_scene"])

    def test_adventure_gate_must_continue_to_typed_scene_opening(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "adventure_opening_required": True,
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    public_fallback_reply="",
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="潮雾压低风铃声，失忆旅人站在会长面前等候去路。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "大家同意进入第一章，请先描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "潮雾压低风铃声，失忆旅人站在会长面前等候去路。",
        )
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        second_request = json.loads(client.calls[1]["messages"][1].content)
        self.assertEqual(
            [tool["name"] for tool in second_request["available_tools"]],
            ["start_scene"],
        )

    def test_nonpublic_scene_focus_cannot_end_before_required_action(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="focus",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="focus_scene_branch",
                    ok=True,
                    result={
                        "required_followup_tools": ["move_scene_group"],
                        "allowed_followup_tools": ["move_scene_group"],
                    },
                    state_changed=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="move_scene_group",
                description="move",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="move_scene_group",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="赛璃与失忆旅人抵达登记小室。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "focus_scene_branch",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "tool_name": "",
                        "arguments": {},
                        "reason": "镜头准备本身不公开。",
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "move_scene_group",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "赛璃牵着失忆旅人进入登记小室。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "赛璃与失忆旅人抵达登记小室。")
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["focus_scene_branch", "move_scene_group"],
        )

    def test_defeat_aftermath_receipt_temporarily_exposes_scene_commit(self) -> None:
        for source_tool in ("focus_scene_branch", "transition_scene"):
            with self.subTest(source_tool=source_tool):
                registry = GMToolRegistry()
                registry.register(
                    GMToolDefinition(
                        name=source_tool,
                        description="准备败北后果场景。",
                        handler=lambda _context, _arguments, tool=source_tool: (
                            GMToolReceipt.success(
                                tool,
                                result={
                                    "allowed_followup_tools": ["commit_scene_response"],
                                    "required_followup_tools": ["commit_scene_response"],
                                    "scene_response_followup": {
                                        "public_reply": "铁门落锁，牢房里只剩下滴水声。",
                                        "public_facts": [],
                                    },
                                },
                                state_changed=True,
                            )
                        ),
                        side_effect="write",
                    )
                )
                registry.register(
                    GMToolDefinition(
                        name="commit_scene_response",
                        description="公开已建立的败北后果场景。",
                        handler=lambda _context, _arguments: GMToolReceipt.success(
                            "commit_scene_response",
                            state_changed=True,
                            public_reply="铁门落锁，牢房里只剩下滴水声。",
                            lock_public_reply=True,
                        ),
                        side_effect="write",
                    )
                )
                client = ScriptedClient(
                    [
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "tool_name": source_tool,
                                "arguments": {},
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "tool_name": "commit_scene_response",
                                "arguments": {},
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                agent = LLMGMToolAgent(client, model="fake", registry=registry)
                context = execution_context()
                context.directly_addressed = False
                context.metadata.update(
                    {
                        "system_gm_beat_request": True,
                        "heartbeat_action": "defeat_aftermath",
                        "heartbeat_require_material_change": True,
                    }
                )

                outcome = agent.run(
                    "必须让放弃抵抗的角色进入下一场后果场景。",
                    recent_context="",
                    context=context,
                    state_summary={},
                )

                first_request = json.loads(client.calls[0]["messages"][-1].content)
                second_request = json.loads(client.calls[1]["messages"][-1].content)
                self.assertNotIn(
                    "commit_scene_response",
                    {item["name"] for item in first_request["available_tools"]},
                )
                self.assertEqual(
                    [item["name"] for item in second_request["available_tools"]],
                    ["commit_scene_response"],
                )
                self.assertEqual(
                    [item.tool_name for item in outcome.receipts],
                    [source_tool, "commit_scene_response"],
                )
                self.assertEqual(
                    outcome.reply,
                    "铁门落锁，牢房里只剩下滴水声。",
                )

    def test_defeat_aftermath_direct_scene_commit_stays_silent_across_retries(self) -> None:
        published: list[str] = []
        fallen_pcs = {"艾薇娅": "分离：被守卫重新收押"}
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="公开场景变化。",
                handler=lambda _context, _arguments: (
                    published.append("旧后果被再次公开")
                    or GMToolReceipt.success(
                        "commit_scene_response",
                        state_changed=True,
                        public_reply="牢门上的蓝光再次亮起。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        outcomes = []

        for _attempt in range(2):
            client = ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "commit_scene_response",
                            "arguments": {},
                        },
                        ensure_ascii=False,
                    )
                ]
            )
            agent = LLMGMToolAgent(
                client,
                model="fake",
                registry=registry,
                max_iterations=1,
            )
            context = execution_context()
            context.directly_addressed = False
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "defeat_aftermath",
                    "heartbeat_require_material_change": True,
                }
            )

            outcome = agent.run(
                "必须让放弃抵抗的角色进入下一场后果场景。",
                recent_context="牢门上的蓝光已经重新亮起。",
                context=context,
                state_summary={"fallen_pcs": dict(fallen_pcs)},
            )
            request = json.loads(client.calls[0]["messages"][-1].content)
            self.assertNotIn(
                "commit_scene_response",
                {item["name"] for item in request["available_tools"]},
            )
            outcomes.append(outcome)

        self.assertEqual(published, [])
        self.assertEqual(fallen_pcs, {"艾薇娅": "分离：被守卫重新收押"})
        self.assertTrue(all(outcome.reply == "" for outcome in outcomes))
        self.assertTrue(all(outcome.target == "silent" for outcome in outcomes))
        self.assertTrue(all(outcome.receipts == [] for outcome in outcomes))

    def test_required_followup_gets_bounded_grace_after_normal_iteration_limit(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description="focus",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="focus_scene_branch",
                    ok=True,
                    result={
                        "required_followup_tools": ["commit_story_item_action"],
                        "allowed_followup_tools": ["commit_story_item_action"],
                    },
                    state_changed=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_story_item_action",
                description="story item",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_story_item_action",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="苍祈点亮蓝芯守望灯，示警蓝光从廊下亮起。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "focus_scene_branch",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "已经切回风铃廊。",
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_story_item_action",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=2,
        )

        outcome = agent.run(
            "苍祈点亮手中的蓝芯守望灯。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "苍祈点亮蓝芯守望灯，示警蓝光从廊下亮起。",
        )
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["focus_scene_branch", "commit_story_item_action"],
        )
        self.assertTrue(
            any(
                item.get("phase") == "bounded_transaction_recovery_grace"
                for item in outcome.trace
            )
        )

    def test_session_zero_opening_is_owned_by_core_gm_after_gate_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "session_zero_opening_required": True,
                        "opening_instruction": "请开始第零章，先聊基调和安全边界。",
                    },
                    state_changed=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "好，我们先聊基调和安全边界。大家希望故事整体是什么感觉，又有哪些内容不希望出现或只想淡出处理？",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=3,
        )

        outcome = agent.run(
            "大家准备好了，请开始第零章，先聊基调和安全边界。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "好，我们先聊基调和安全边界。大家希望故事整体是什么感觉，又有哪些内容不希望出现或只想淡出处理？",
        )
        self.assertEqual([item.tool_name for item in outcome.receipts], ["start_session"])

    def test_malformed_required_followup_returns_to_full_agent_loop(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="潮雾压着风铃廊，失忆旅人正在门边等候。",
                    lock_public_reply=True,
                ),
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "start_session",
                        "arguments": {},
                    }
                ),
                '{"decision":"call_tool","tool_name":"start_scene","arguments":',
                '{"decision":"call_tool","tool_name":"start_scene","arguments":',
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "start_scene",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="semantic-model",
            protocol_repair_model="syntax-model",
            registry=registry,
            parse_retries=1,
            max_iterations=4,
        )

        outcome = agent.run(
            "大家同意进入第一章，请描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(
            outcome.reply,
            "潮雾压着风铃廊，失忆旅人正在门边等候。",
        )
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        self.assertTrue(
            any(
                item.get("phase") == "decision_protocol_returned_to_agent"
                for item in outcome.trace
            )
        )
        resumed_request = json.loads(client.calls[3]["messages"][1].content)
        self.assertEqual(
            [tool["name"] for tool in resumed_request["available_tools"]],
            ["start_scene"],
        )
        self.assertEqual(
            resumed_request["history"][-1]["protocol_error"]["error_code"],
            "INVALID_AGENT_TOOL_PROTOCOL",
        )

    def test_pending_followup_is_not_a_recovered_complete_state_change(self) -> None:
        receipts = [
            GMToolReceipt(
                tool_name="start_session",
                ok=True,
                result={
                    "allowed_followup_tools": ["start_scene"],
                    "required_followup_tools": ["start_scene"],
                },
                state_changed=True,
                lock_public_reply=True,
            )
        ]

        self.assertFalse(GMToolReceiptPolicy.state_change_recovered(receipts))

    def test_player_input_blocker_keeps_prior_successful_write(
        self,
    ) -> None:
        state: list[str] = []

        class Transaction:
            def __init__(self) -> None:
                self.before = list(state)
                self.active = True

            def commit(self) -> None:
                self.active = False

            def rollback(self) -> None:
                if self.active:
                    state[:] = self.before
                    self.active = False

        registry = GMToolRegistry(transaction_factory=lambda *_args: Transaction())
        registry.register(
            GMToolDefinition(
                name="write_world_detail",
                description="write",
                handler=lambda _context, _arguments: (
                    state.append("world-detail")
                    or GMToolReceipt.success(
                        "write_world_detail",
                        state_changed=True,
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="start",
                handler=lambda _context, _arguments: GMToolReceipt.failure(
                    "start_session",
                    "ADVENTURE_START_BLOCKED",
                    "仍缺少玩家角色与安全边界。",
                    "等待玩家补充。",
                    retryable=False,
                    result={"player_input_required": True},
                    public_reply="先定好角色和界限，我们就开第一章。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "message_kind": "gm_request",
                        "tool_name": "write_world_detail",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "start_session",
                        "arguments": {},
                    }
                ),
            ]
        )
        context = execution_context()
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "其余世界设定由你补充，然后开始第一章。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(state, ["world-detail"])
        self.assertEqual(outcome.mode, "gm_agent_rule_rejected")
        self.assertEqual(outcome.reply, "先定好角色和界限，我们就开第一章。")
        self.assertTrue(outcome.state_changed)
        self.assertFalse(
            any("message_transaction_rollback" in item for item in outcome.trace)
        )

    def test_technical_failure_after_write_still_rolls_back_transaction(self) -> None:
        receipts = [
            GMToolReceipt.success("write_world_detail", state_changed=True),
            GMToolReceipt.failure(
                "start_session",
                "TOOL_EXECUTION_FAILED",
                "list index out of range",
                "检查服务日志。",
                retryable=False,
            ),
        ]

        self.assertFalse(
            GMToolReceiptPolicy.state_change_recovered_with_player_input_blocker(
                receipts
            )
        )

    def test_required_scene_followup_rejects_premature_final(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="start_session",
                description="gate",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_session",
                    ok=True,
                    result={
                        "allowed_followup_tools": ["start_scene"],
                        "required_followup_tools": ["start_scene"],
                    },
                    state_changed=True,
                    lock_public_reply=True,
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description="scene",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="start_scene",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="风铃廊在潮雾中显出轮廓。",
                    lock_public_reply=True,
                ),
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps({"decision": "call_tool", "tool_name": "start_session", "arguments": {}}),
                    json.dumps({"decision": "final", "reply": "第一章开始。"}),
                    json.dumps({"decision": "call_tool", "tool_name": "start_scene", "arguments": {}}),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "大家同意进入第一章，请描述现场。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "风铃廊在潮雾中显出轮廓。")
        self.assertEqual(
            [item.tool_name for item in outcome.receipts],
            ["start_session", "start_scene"],
        )
        self.assertEqual(len(agent.client.calls), 3)

    def test_npc_followup_grant_rejects_unrelated_second_tool(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        registry.register(
            GMToolDefinition(
                name="decide_npc_response",
                description="npc",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="decide_npc_response",
                    ok=True,
                    result={"allowed_followup_tools": ["start_scene"]},
                    state_changed=True,
                    public_fallback_reply="会长答应带路。",
                    lock_public_reply=True,
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="fill_clock",
                description="clock",
                handler=lambda _context, _arguments: (
                    executed.append("fill_clock")
                    or GMToolReceipt.success("fill_clock", state_changed=True)
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "decide_npc_response",
                        "arguments": {},
                    }
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "fill_clock",
                        "arguments": {},
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "请会长带路。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(outcome.reply, "会长答应带路。")
        self.assertEqual(executed, [])
        self.assertEqual(outcome.trace[-1]["protocol_error"], "PUBLIC_RECEIPT_FOLLOWUP_NOT_ALLOWED")

    def test_primary_rules_action_stops_later_actions_from_the_same_message(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        def locked_receipt(name: str, reply: str):
            return lambda _context, _arguments: (
                executed.append(name)
                or GMToolReceipt(
                    tool_name=name,
                    ok=True,
                    state_changed=True,
                    public_fallback_reply=reply,
                    lock_public_reply=True,
                )
            )

        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=locked_receipt("perform_check_action", "伊莉雅完成了调查。"),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="perform_character_action",
                description="guard",
                handler=locked_receipt("perform_character_action", "伊莉雅随后进入防御。"),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tools",
                            "calls": [
                                {"tool_name": "perform_check_action", "arguments": {}},
                                {"tool_name": "perform_character_action", "arguments": {}},
                            ],
                            "reason": "错误地把同一句站位和观察拆成两个规则动作。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅挡在旅人身前，观察门外是否有追兵。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(executed, ["perform_check_action"])
        self.assertEqual(outcome.reply, "伊莉雅完成了调查。")
        self.assertEqual(len(outcome.receipts), 1)

    def test_gm_owned_fumble_window_can_finish_the_same_rules_transaction(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        registry.register(
            GMToolDefinition(
                name="perform_check_action",
                description="check",
                handler=lambda _context, _arguments: (
                    executed.append("check")
                    or GMToolReceipt(
                        tool_name="perform_check_action",
                        ok=True,
                        state_changed=True,
                        result={
                            "pending_decisions": [
                                {
                                    "window_id": "fumble-1",
                                    "kind": "fumble_opportunity",
                                    "owner": "__gm__",
                                }
                            ]
                        },
                        public_fallback_reply="检定掷出了大失败。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="resolve_gm_opportunity",
                description="resolve",
                handler=lambda _context, _arguments: (
                    executed.append("resolve")
                    or GMToolReceipt(
                        tool_name="resolve_gm_opportunity",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="GM把机会用于制造新的危险。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        agent = LLMGMToolAgent(
            ScriptedClient(
                [
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "perform_check_action",
                            "arguments": {},
                        }
                    ),
                    json.dumps(
                        {
                            "decision": "call_tool",
                            "tool_name": "resolve_gm_opportunity",
                            "arguments": {},
                        }
                    ),
                ]
            ),
            model="fake",
            registry=registry,
        )

        outcome = agent.run(
            "伊莉雅检查闸门机关。",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(executed, ["check", "resolve"])
        self.assertEqual(
            outcome.reply,
            "检定掷出了大失败。\nGM把机会用于制造新的危险。",
        )
        self.assertEqual(len(outcome.receipts), 2)

    def test_required_material_heartbeat_rejects_silence_until_write_receipt(self) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="commit_scene_response",
                    ok=True,
                    state_changed=True,
                    public_fallback_reply="闸门外的骑手落地，封住了北侧出口。",
                    lock_public_reply=True,
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="save_campaign",
                description="unrelated",
                handler=lambda _context, _arguments: GMToolReceipt(
                    tool_name="save_campaign", ok=True, state_changed=True
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps({"decision": "silent", "reason": "错误地保持静默。"}),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_scene_response",
                        "arguments": {},
                        "reason": "提交可见变化。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"decision": "final", "reply": "不应覆盖锁定回复。"},
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry, max_iterations=4)
        context = execution_context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "event_id": "riders-arrive",
                        "source_kind": "scheduled_event",
                        "status": "due",
                        "public_reply": "闸门外的骑手落地，封住了北侧出口。",
                        "public_facts": [],
                    }
                ],
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertTrue(outcome.state_changed)
        self.assertEqual(outcome.reply, "闸门外的骑手落地，封住了北侧出口。")
        first_request = json.loads(client.calls[0]["messages"][-1].content)
        tool_names = {item["name"] for item in first_request["available_tools"]}
        self.assertEqual(tool_names, {"commit_scene_response"})
        system_prompt = client.calls[0]["messages"][0].content
        self.assertIn("主动节拍决策层", system_prompt)
        self.assertNotIn("查看“我的角色草稿”", system_prompt)
        self.assertEqual(outcome.trace[1]["arguments"], {})
        self.assertEqual(len(client.calls), 2)

    def test_required_material_heartbeat_rejects_private_state_write(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []
        registry.register(
            GMToolDefinition(
                name="update_npc_state",
                description="只更新NPC后台状态。",
                handler=lambda _context, _arguments: (
                    executed.append("private")
                    or GMToolReceipt(
                        tool_name="update_npc_state",
                        ok=True,
                        state_changed=True,
                        result={"npc": {"mood": "警惕"}},
                    )
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="提交公开场景变化。",
                handler=lambda _context, _arguments: (
                    executed.append("public")
                    or GMToolReceipt(
                        tool_name="commit_scene_response",
                        ok=True,
                        state_changed=True,
                        public_fallback_reply="门外传来新的钥匙转动声。",
                        lock_public_reply=True,
                    )
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "update_npc_state",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "reply": "牢门上的蓝光仍在蔓延。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_scene_response",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=registry,
            max_iterations=4,
        )
        context = execution_context()
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "event_id": "key-turn",
                        "source_kind": "scheduled_event",
                        "status": "due",
                        "public_reply": "门外传来新的钥匙转动声。",
                        "public_facts": [],
                    }
                ],
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="牢门上的蓝光已经沿石缝蔓延。",
            context=context,
            state_summary={},
        )

        self.assertEqual(executed, ["public"])
        self.assertEqual(outcome.reply, "门外传来新的钥匙转动声。")
        self.assertEqual(len(client.calls), 3)
        third_request = json.loads(client.calls[2]["messages"][-1].content)
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("protocol_error", {}).get("error_code")
                == "MATERIAL_CHANGE_REQUIRED"
                for item in third_request["history"]
            )
        )

    def test_material_heartbeat_can_end_silent_after_authority_gate_rejects_change(
        self,
    ) -> None:
        registry = GMToolRegistry()
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="提交公开场景变化。",
                handler=lambda _context, _arguments: GMToolReceipt.failure(
                    "commit_scene_response",
                    "SCENE_CHANGE_AUTHORITY_REQUIRED",
                    "这项变化没有引用结构化权限来源。",
                    "没有到期结果时保持静默。",
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "commit_scene_response",
                        "arguments": {},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "decision": "silent",
                        "reason": "当前没有到期的结构化变化。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "event_id": "rejected-due-result",
                        "source_kind": "scheduled_event",
                        "status": "due",
                        "public_reply": "旧钟在这一刻敲响。",
                        "public_facts": [],
                    }
                ],
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="只有风声与先前留下的脚印。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(any(receipt.ok for receipt in outcome.receipts))

    def test_material_heartbeat_without_due_authority_can_end_silent_immediately(
        self,
    ) -> None:
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "silent",
                        "reason": "当前没有到期变化，也没有适合此刻行动的在场NPC。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
        context = execution_context(speaker="系统主动节拍")
        context.directly_addressed = False
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_force": True,
                "heartbeat_require_material_change": True,
            }
        )

        outcome = agent.run(
            "系统要求判断当前局面是否需要推进。",
            recent_context="英雄刚刚改变了局面，现场人物仍在反应。",
            context=context,
            state_summary={},
        )

        self.assertEqual(outcome.target, "silent")
        self.assertEqual(outcome.reply, "")
        self.assertEqual(len(client.calls), 1)

    def test_heartbeat_batch_stops_after_first_public_material_change(self) -> None:
        registry = GMToolRegistry()
        executed: list[str] = []

        def commit(_context, arguments):
            marker = str(arguments.get("marker") or "")
            executed.append(marker)
            return GMToolReceipt(
                tool_name="commit_scene_response",
                ok=True,
                state_changed=True,
                public_fallback_reply=marker,
                lock_public_reply=True,
            )

        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description="commit",
                handler=commit,
                parameters=(
                    GMToolParameter("marker", "string", "公开变化", required=True),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {"tool_name": "commit_scene_response", "arguments": {"marker": "第一拍"}},
                            {"tool_name": "commit_scene_response", "arguments": {"marker": "不应执行"}},
                        ],
                        "reason": "错误地试图连续推进。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "free_scene_beat",
                "heartbeat_require_material_change": True,
                "scene_change_authorities": [
                    {
                        "event_id": "first-material-beat",
                        "source_kind": "scheduled_event",
                        "status": "due",
                        "public_reply": "第一拍",
                        "public_facts": [],
                    }
                ],
            }
        )

        outcome = agent.run(
            "系统要求推进当前局面。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(executed, ["第一拍"])
        self.assertEqual(outcome.reply, "第一拍")
        self.assertEqual(len(outcome.receipts), 1)

    def test_batch_executes_identical_read_call_only_once(self) -> None:
        registry = GMToolRegistry()
        calls: list[str] = []

        def read(_context, _arguments):
            calls.append("read")
            return GMToolReceipt.success(
                "search_rule_references",
                result={"name": "谴责"},
            )

        registry.register(
            GMToolDefinition(
                name="search_rule_references",
                description="search",
                handler=read,
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "search_rule_references",
                                "arguments": {},
                            },
                            {
                                "tool_name": "search_rule_references",
                                "arguments": {},
                            },
                        ],
                        "terminal_decision": "final",
                        "reply": "【谴责】是游说家技能。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "谴责是什么技能？",
            recent_context="",
            context=execution_context(),
            state_summary={},
        )

        self.assertEqual(calls, ["read"])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(outcome.reply, "【谴责】是游说家技能。")
        self.assertEqual(
            outcome.trace[0]["skipped_duplicate_calls"][0]["batch_index"],
            2,
        )

    def test_batch_deduplicates_implicit_and_explicit_single_event_source(self) -> None:
        registry = GMToolRegistry()
        calls: list[dict[str, object]] = []

        def write(_context, arguments):
            calls.append(dict(arguments))
            return GMToolReceipt.success(
                "commit_test_update",
                result={"recorded": True},
                state_changed=True,
            )

        registry.register(
            GMToolDefinition(
                name="commit_test_update",
                description="write",
                handler=write,
                parameters=(
                    GMToolParameter(
                        name="updates",
                        kind="array",
                        description="updates",
                        required=True,
                        schema_details={"items": {"type": "string"}},
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "commit_test_update",
                                "arguments": {"updates": ["火锅大陆"]},
                            },
                            {
                                "tool_name": "commit_test_update",
                                "arguments": {
                                    "updates": ["火锅大陆"],
                                    "source_event_id": "event-hotpot",
                                },
                            },
                        ],
                        "terminal_decision": "final",
                        "reply": "已记下。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.metadata["current_turn_events"] = [
            {
                "event_id": "event-hotpot",
                "message_id": "message-hotpot",
                "speaker": "测试玩家甲",
                "text": "就叫火锅大陆吧",
            }
        ]

        outcome = agent.run(
            "就叫火锅大陆吧",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(calls, [{"updates": ["火锅大陆"]}])
        self.assertEqual(len(outcome.receipts), 1)
        self.assertEqual(
            outcome.trace[0]["skipped_duplicate_calls"][0]["batch_index"],
            2,
        )

    def test_batch_keeps_same_write_from_distinct_source_events(self) -> None:
        registry = GMToolRegistry()
        calls: list[str] = []

        def write(context, _arguments):
            calls.append(str(context.metadata.get("source_event_id") or ""))
            return GMToolReceipt.success(
                "commit_test_update",
                result={"recorded": True},
                state_changed=True,
            )

        registry.register(
            GMToolDefinition(
                name="commit_test_update",
                description="write",
                handler=write,
                parameters=(
                    GMToolParameter(
                        name="updates",
                        kind="array",
                        description="updates",
                        required=True,
                        schema_details={"items": {"type": "string"}},
                    ),
                ),
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tools",
                        "calls": [
                            {
                                "tool_name": "commit_test_update",
                                "arguments": {
                                    "updates": ["先跳过"],
                                    "source_event_id": "event-a",
                                },
                            },
                            {
                                "tool_name": "commit_test_update",
                                "arguments": {
                                    "updates": ["先跳过"],
                                    "source_event_id": "event-b",
                                },
                            },
                        ],
                        "terminal_decision": "final",
                        "reply": "都记下了。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)
        context = execution_context()
        context.metadata["current_turn_events"] = [
            {
                "event_id": "event-a",
                "speaker": "阿凛",
                "text": "这项我先跳过。",
            },
            {
                "event_id": "event-b",
                "speaker": "南星",
                "text": "这项我也先跳过。",
            },
        ]

        outcome = agent.run(
            "阿凛和南星都选择先跳过。",
            recent_context="",
            context=context,
            state_summary={},
        )

        self.assertEqual(calls, ["event-a", "event-b"])
        self.assertEqual(len(outcome.receipts), 2)
        self.assertNotIn("skipped_duplicate_calls", outcome.trace[0])

    def test_npc_profile_tool_schema_exposes_allowed_nested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            schema = next(
                item
                for item in service.gm_tool_registry.schemas()
                if item["name"] == "create_npc_profile"
            )

        parameters = schema["parameters"]
        self.assertIn("present_in_scene", parameters["required"])
        profile = parameters["properties"]["profile"]
        self.assertFalse(profile["additionalProperties"])
        self.assertIn("active_goal", profile["properties"])
        self.assertNotIn("current_location", profile["properties"])

    def test_free_scene_heartbeat_without_due_authority_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = LLMGMToolAgent(
                ScriptedClient([]),
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context()
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "free_scene_beat",
                }
            )

            names = {
                item["name"] for item in agent._available_tool_schemas(context)
            }

        self.assertEqual(
            names,
            {
                "get_scene_state",
                "get_gameplay_state",
                "get_clocks",
                "get_npc_profiles",
            },
        )

    def test_scene_opening_heartbeat_exposes_only_atomic_scene_publication_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = LLMGMToolAgent(
                ScriptedClient([]),
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context()
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "scene_opening",
                    "heartbeat_require_material_change": True,
                }
            )

            names = {item["name"] for item in agent._available_tool_schemas(context)}

        self.assertIn("start_scene", names)
        self.assertIn("get_gameplay_state", names)
        self.assertIn("get_scene_state", names)
        self.assertIn("get_clocks", names)
        self.assertIn("get_npc_profiles", names)
        self.assertNotIn("commit_scene_response", names)
        self.assertNotIn("save_campaign", names)

    def test_authored_existing_scene_opening_exposes_scene_response_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            agent = LLMGMToolAgent(
                ScriptedClient([]),
                model="fake",
                registry=service.gm_tool_registry,
            )
            context = execution_context()
            context.metadata.update(
                {
                    "system_gm_beat_request": True,
                    "heartbeat_action": "scene_opening",
                    "heartbeat_require_material_change": True,
                    "gm_authored_scene_opening": True,
                }
            )

            names = {item["name"] for item in agent._available_tool_schemas(context)}

        self.assertIn("commit_scene_response", names)


    def test_agent_refreshes_authoritative_state_after_each_tool_call(self) -> None:
        state = {"value": 0}
        registry = GMToolRegistry()

        def increment(_context, _arguments):
            state["value"] += 1
            return GMToolReceipt(
                tool_name="increment",
                ok=True,
                state_changed=True,
                result={"value": state["value"]},
            )

        registry.register(
            GMToolDefinition(
                name="increment",
                description="increment state",
                handler=increment,
                side_effect="write",
            )
        )
        client = ScriptedClient(
            [
                json.dumps(
                    {
                        "decision": "call_tool",
                        "tool_name": "increment",
                        "arguments": {},
                        "reply": "",
                        "reason": "change state",
                    }
                ),
                json.dumps(
                    {
                        "decision": "final",
                        "tool_name": "",
                        "arguments": {},
                        "reply": "updated",
                        "reason": "observed committed state",
                    }
                ),
            ]
        )
        agent = LLMGMToolAgent(client, model="fake", registry=registry)

        outcome = agent.run(
            "update it",
            recent_context="",
            context=execution_context(),
            state_summary={"value": 0},
            state_summary_provider=lambda: {"value": state["value"]},
        )

        self.assertEqual(state["value"], 1)
        self.assertEqual(outcome.reply, "updated")
        second_request = json.loads(client.calls[1]["messages"][-1].content)
        self.assertEqual(second_request["current_state_summary"]["value"], 1)


class FUGMToolHandlerTests(unittest.TestCase):
    def test_successful_agent_load_exposes_backend_confirmed_campaign_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._save_campaign({"campaign_id": "旧团"})
            service._save_campaign({"campaign_id": "当前团"})
            service.session_gates.activate(
                "当前团",
                "group-1",
                "s1",
                status="adventure",
            )
            service.gm_tool_agent = LLMGMToolAgent(
                ScriptedClient(
                    [
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "message_semantics": {
                                    "version": "1",
                                    "events": [
                                        {
                                            "event_id": "message:当前团:group-1:load-old-campaign-1",
                                            "speaker": "阿凛",
                                            "relation": "gm",
                                            "targets": ["时悠"],
                                            "dialogue_act": "question",
                                            "action_commitment": "none",
                                            "responds_to_event_id": "",
                                            "reason": "玩家明确要求主持人读取旧团。",
                                        }
                                    ],
                                },
                                "message_kind": "gm_request",
                                "audience": "gm",
                                "tool_name": "discover_capabilities",
                                "arguments": {
                                    "domains": ["campaign"],
                                    "reason": "玩家要求读取另一个战役。",
                                },
                                "reply": "",
                                "reason": "先取得存读档能力。",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "decision": "call_tool",
                                "tool_name": "load_campaign",
                                "arguments": {"campaign_id": "旧团"},
                                "reply": "",
                                "reason": "玩家明确选择旧团。",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "decision": "final",
                                "tool_name": "",
                                "arguments": {},
                                "reply": "已经读回《旧团》。",
                                "reason": "读取成功。",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                model="fake",
                registry=service.gm_tool_registry,
            )

            status, response = service.handle(
                "POST",
                "/v1/message/route",
                {
                    "campaign_id": "当前团",
                    "session_id": "s1",
                    "channel_id": "group-1",
                    "speaker": "阿凛",
                    "message_id": "load-old-campaign-1",
                    "message": "@时悠 读取旧团",
                    "is_at_bot": True,
                },
            )

            self.assertEqual(status, 200)
            self.assertEqual(response["campaign_id"], "当前团")
            self.assertEqual(response["active_campaign_id"], "旧团")
            self.assertEqual(service._current_campaign_id(), "旧团")

            dashboard_status, dashboard = service.handle(
                "GET",
                "/v1/audit/dashboard?campaign_id=%E5%BD%93%E5%89%8D%E5%9B%A2&session_id=s1",
            )
            self.assertEqual(dashboard_status, 200)
            event = dashboard["gm_tools"]["recent_events"][-1]
            self.assertTrue(event["state_changed"])
            load_receipt = next(
                item
                for item in event["receipts"]
                if item["tool_name"] == "load_campaign"
            )
            self.assertTrue(load_receipt["ok"])

    def test_ambiguous_slot_returns_structured_error_without_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._save_campaign({"campaign_id": "A", "slot": "共同槽"})
            service._save_campaign({"campaign_id": "B", "slot": "共同槽"})
            service._mark_current_campaign("A")

            receipt = service.gm_campaign_tools.load_campaign(
                execution_context(campaign_id="A"),
                {"slot": "共同槽"},
            )

            self.assertFalse(receipt.ok)
            self.assertEqual(receipt.error_code, "AMBIGUOUS_SAVE_SLOT")
            self.assertEqual(receipt.result["matching_campaigns"], ["A", "B"])
            self.assertEqual(service._current_campaign_id(), "A")

    def test_unknown_save_target_does_not_create_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service._runtime("当前团")

            receipt = service.gm_campaign_tools.save_campaign(
                execution_context(campaign_id="当前团"),
                {"campaign_id": "模型猜出来的团", "slot": "误存"},
            )

            self.assertFalse(receipt.ok)
            self.assertEqual(receipt.error_code, "UNKNOWN_CAMPAIGN")
            self.assertFalse(service._memory_store().snapshot_exists("模型猜出来的团"))

    def test_hero_draft_tool_keeps_player_and_hero_names_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("角色团")
            runtime.app.world_state.world_profile.hero_drafts["测试玩家乙"] = HeroDraft(
                player_name="测试玩家乙",
                hero_name="艾丽妮",
                identity="失忆的钟匠学徒",
            )

            receipt = service.gm_campaign_tools.get_hero_drafts(
                execution_context(campaign_id="角色团", speaker="测试玩家乙"),
                {"scope": "mine"},
            )

            self.assertTrue(receipt.ok)
            record = receipt.result["drafts"][0]
            self.assertEqual(record["player_name"], "测试玩家乙")
            self.assertEqual(record["hero_name"], "艾丽妮")


if __name__ == "__main__":
    unittest.main()
