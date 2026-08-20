from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fu_gm.components import (
    gm_agent_message_coordinator as gm_agent_message_coordinator_module,
)
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingVerifier,
)
from fu_gm.components.gm_turn_state_delta import (
    apply_state_delta,
    projection_hash,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character
from fu_gm.prompt_cache import (
    GM_DELTA_PROMPT_LAYOUT_VERSION,
    GM_PROMPT_LAYOUT_VERSION,
)


class _UnusedClient:
    class _Config:
        timeout_seconds = 5.0

    config = _Config()


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
            raise AssertionError("缺少脚本化核心模型响应")
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


class _LatestStateGroundingVerifier:
    def __init__(self) -> None:
        self.observed_states: list[dict[str, object]] = []

    def verify(self, **kwargs: object) -> object:
        self.observed_states.append(dict(kwargs["observed_state"]))
        return SimpleNamespace(
            valid=True,
            category="grounded",
            unsupported_claims=(),
            correction_hint="",
        )


class _ExplodingIntentRouter:
    @staticmethod
    def route(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("intent router unavailable")


def _context(
    message: str,
    *,
    routing_mode: str,
    gate_status: str = "adventure",
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="intent-integration",
        session_id="session-1",
        channel_id="group-1",
        speaker="玩家甲",
        gate_status=gate_status,
        directly_addressed=True,
        metadata={
            "current_message": message,
            "current_turn_events": [
                {
                    "event_id": "event-1",
                    "speaker": "玩家甲",
                    "text": message,
                }
            ],
            "gm_dynamic_capabilities_enabled": True,
            "gm_hot_adventure_capabilities_enabled": True,
            "gm_hot_session_zero_capabilities_enabled": True,
            "gm_capability_routing_mode": routing_mode,
            "gm_state_context_mode": "full",
        },
    )


def _build_view(
    root: Path,
    *,
    message: str,
    routing_mode: str,
    exploding_router: bool = False,
    gate_status: str = "adventure",
) -> tuple[
    FUGMHttpService,
    LLMGMToolAgent,
    GMToolExecutionContext,
    dict[str, object],
    set[str],
]:
    service = FUGMHttpService(data_root=str(root), use_llm=False)
    agent = LLMGMToolAgent(
        _UnusedClient(),
        model="fake",
        registry=service.gm_tool_registry,
    )
    context = _context(
        message,
        routing_mode=routing_mode,
        gate_status=gate_status,
    )
    if exploding_router:
        service.gm_agent_message_coordinator.state_builder.intent_capability_router = (
            _ExplodingIntentRouter()
        )
    state = service.gm_agent_message_coordinator.state_builder.build(context)
    visible = {
        str(schema.get("name") or "")
        for schema in agent._available_tool_schemas(context)
        if str(schema.get("name") or "")
    }
    return service, agent, context, state, visible


def test_service_defaults_enable_intent_routing_and_turn_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FU_GM_CAPABILITY_ROUTING_MODE", raising=False)
    monkeypatch.delenv("FU_GM_STATE_CONTEXT_MODE", raising=False)

    service = FUGMHttpService(
        data_root=str(tmp_path / "default-optimized-modes"),
        use_llm=False,
    )

    assert service.capability_routing_mode == "intent"
    assert service.state_context_mode == "summary_delta"


def test_baseline_and_shadow_keep_identical_visible_schema_and_state(
    tmp_path: Path,
) -> None:
    message = "我观察一下牢门。"
    _, _, baseline_context, baseline_state, baseline_visible = _build_view(
        tmp_path / "baseline",
        message=message,
        routing_mode="baseline",
    )
    _, _, shadow_context, shadow_state, shadow_visible = _build_view(
        tmp_path / "shadow",
        message=message,
        routing_mode="shadow",
    )

    assert baseline_visible == shadow_visible
    assert baseline_state == shadow_state
    assert "gm_intent_profile_ids" not in baseline_context.metadata
    assert shadow_context.metadata["gm_intent_router_status"] == "planned"
    assert shadow_context.metadata["gm_intent_profile_ids"] == ["check_action"]


@pytest.mark.parametrize(
    ("message", "profile_id", "required", "excluded"),
    [
        (
            "第一章已经开始了吗？",
            "reply_only",
            {"discover_capabilities"},
            {
                "declare_check_action",
                "decide_npc_response",
                "perform_character_action",
            },
        ),
        (
            "请查询一下伤害规则怎么算。",
            "rule_read",
            {"get_rule_reference", "search_rule_references"},
            {
                "declare_check_action",
                "decide_npc_response",
                "perform_character_action",
            },
        ),
        (
            "我观察一下牢门。",
            "check_action",
            {
                "declare_check_action",
                "perform_character_action",
                "perform_in_scene_action",
                "perform_ritual_project_action",
                "perform_scene_action",
            },
            {
                "decide_collective_response",
                "decide_npc_response",
                "move_scene_group",
                "save_campaign",
            },
        ),
    ],
)
def test_intent_mode_exposes_the_expected_micro_profile(
    tmp_path: Path,
    message: str,
    profile_id: str,
    required: set[str],
    excluded: set[str],
) -> None:
    _, _, context, _state, visible = _build_view(
        tmp_path / profile_id,
        message=message,
        routing_mode="intent",
    )

    assert context.metadata["gm_intent_router_status"] == "planned"
    assert context.metadata["gm_intent_profile_ids"] == [profile_id]
    assert required <= visible
    assert visible.isdisjoint(excluded)
    assert set(context.metadata["gm_hot_adventure_tool_names"]) <= visible


def test_boss_conflict_route_projects_skill_evidence_into_grounding_review(
    tmp_path: Path,
) -> None:
    message = (
        "诺艾尔使用利刃风暴，以双盾分别攻击赤炉大将和熔炉侍从；"
        "这是当前回合的完整动作，请按真实骰子结算两个不同目标。"
    )
    service = FUGMHttpService(
        data_root=str(tmp_path / "boss-skill-grounding"),
        use_llm=False,
    )
    context = _context(message, routing_mode="intent")
    runtime = service._runtime(context.campaign_id)
    runtime.app.character_manager.add(
        Character(
            name="诺艾尔",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=65,
            hp=65,
            max_mp=35,
            mp=35,
            traits=["pc"],
            skills={"双盾战士": 1, "利刃风暴": 1},
            equipment=["符文盾", "青铜盾"],
            equipped_main_hand="符文盾",
            equipped_shield="青铜盾",
        )
    )

    state = service.gm_agent_message_coordinator.state_builder.build(context)

    assert context.metadata["gm_intent_profile_ids"] == ["conflict"]
    hero = next(
        row
        for row in state["gameplay"]["characters"]
        if row["name"] == "诺艾尔"
    )
    assert "利刃风暴" in hero["skills"]
    assert hero["equipped"]["main_hand"] == "符文盾"
    assert hero["equipped"]["shield"] == "青铜盾"

    client = _ScriptedClient(
        [
            {
                "valid": True,
                "category": "grounded",
                "unsupported_claims": [],
                "correction_hint": "",
            }
        ]
    )
    verifier = GMReplyGroundingVerifier(client, model="fake")
    review = verifier.verify_tool_proposal(
        current_message=message,
        recent_context="",
        observed_state=state,
        tool_name="perform_character_action",
        arguments={
            "action_type": "Skill",
            "actor": "诺艾尔",
            "target": "赤炉大将",
            "details": {
                "skill_name": "利刃风暴",
                "targets": ["赤炉大将", "熔炉侍从"],
            },
        },
        deadline=999999999.0,
    )

    assert review.valid is True
    assert client.calls[0]["operation"] == "gm_tool_proposal_grounding_verification"
    request = json.loads(client.calls[0]["messages"][1].content)
    reviewer_hero = next(
        row
        for row in request["current_authoritative_state"]["gameplay"]["characters"]
        if row["name"] == "诺艾尔"
    )
    assert "利刃风暴" in reviewer_hero["skills"]


def test_intent_router_exception_falls_back_to_the_legacy_hot_view(
    tmp_path: Path,
) -> None:
    message = "我观察一下牢门。"
    _, _, _, baseline_state, baseline_visible = _build_view(
        tmp_path / "baseline-fallback",
        message=message,
        routing_mode="baseline",
    )
    _, _, intent_context, intent_state, intent_visible = _build_view(
        tmp_path / "intent-fallback",
        message=message,
        routing_mode="intent",
        exploding_router=True,
    )

    assert intent_context.metadata["gm_intent_router_status"] == "fallback_baseline"
    assert intent_visible == baseline_visible
    assert intent_state == baseline_state


def test_session_zero_shadow_keeps_the_legacy_hot_schema_view(
    tmp_path: Path,
) -> None:
    message = "我贡献钟鸣公国，位于镜线内海北岸。"
    _, _, baseline_context, baseline_state, baseline_visible = _build_view(
        tmp_path / "session-zero-baseline",
        message=message,
        routing_mode="baseline",
        gate_status="session_zero",
    )
    _, _, shadow_context, shadow_state, shadow_visible = _build_view(
        tmp_path / "session-zero-shadow",
        message=message,
        routing_mode="shadow",
        gate_status="session_zero",
    )

    assert baseline_visible == shadow_visible
    assert baseline_state == shadow_state
    assert "gm_intent_profile_ids" not in baseline_context.metadata
    assert shadow_context.metadata["gm_intent_profile_ids"] == [
        "session_zero_world"
    ]


def test_session_zero_intent_router_exception_falls_back_to_legacy_hot_view(
    tmp_path: Path,
) -> None:
    message = "我贡献钟鸣公国，位于镜线内海北岸。"
    _, _, _, baseline_state, baseline_visible = _build_view(
        tmp_path / "session-zero-fallback-baseline",
        message=message,
        routing_mode="baseline",
        gate_status="session_zero",
    )
    _, _, context, fallback_state, fallback_visible = _build_view(
        tmp_path / "session-zero-fallback-intent",
        message=message,
        routing_mode="intent",
        gate_status="session_zero",
        exploding_router=True,
    )

    assert context.metadata["gm_intent_router_status"] == "fallback_baseline"
    assert context.metadata["gm_intent_effective_mode"] == "baseline"
    assert fallback_visible == baseline_visible
    assert fallback_state == baseline_state


@pytest.mark.parametrize(
    ("message", "profile_id", "required", "excluded"),
    [
        (
            "我贡献钟鸣公国，位于镜线内海北岸。",
            "session_zero_world",
            {"create_world_setting", "query_world_settings"},
            {"confirm_hero_draft", "record_safety_boundary"},
        ),
        (
            "我的角色主题我选责任，确认写进角色草稿。",
            "session_zero_hero",
            {"confirm_hero_draft", "update_hero_draft"},
            {"create_world_setting", "record_safety_boundary"},
        ),
        (
            "界限：不要出现蜘蛛。",
            "session_zero_safety",
            {"record_safety_boundary"},
            {"create_world_setting", "update_hero_draft"},
        ),
    ],
)
def test_session_zero_intent_mode_exposes_fixed_micro_profiles(
    tmp_path: Path,
    message: str,
    profile_id: str,
    required: set[str],
    excluded: set[str],
) -> None:
    _, _, context, _state, visible = _build_view(
        tmp_path / profile_id,
        message=message,
        routing_mode="intent",
        gate_status="session_zero",
    )

    assert context.metadata["gm_intent_router_status"] == "planned"
    assert context.metadata["gm_intent_profile_ids"] == [profile_id]
    assert required <= visible
    assert visible.isdisjoint(excluded)
    assert set(context.metadata["gm_hot_session_zero_tool_names"]) <= visible


def test_ambiguous_session_zero_intent_keeps_the_legacy_hot_schema_view(
    tmp_path: Path,
) -> None:
    message = "我有个想法，先听听大家。"
    _, _, _, baseline_state, baseline_visible = _build_view(
        tmp_path / "ambiguous-baseline",
        message=message,
        routing_mode="baseline",
        gate_status="session_zero",
    )
    _, _, context, intent_state, intent_visible = _build_view(
        tmp_path / "ambiguous-intent",
        message=message,
        routing_mode="intent",
        gate_status="session_zero",
    )

    assert context.metadata["gm_intent_profile_ids"] == [
        "session_zero_ambiguous"
    ]
    assert intent_visible == baseline_visible
    assert intent_state == baseline_state


def test_session_zero_visible_managed_tools_match_execution_permission(
    tmp_path: Path,
) -> None:
    service, agent, context, _state, visible = _build_view(
        tmp_path / "session-zero-permission",
        message="伊莉雅职业技能先选保镖。",
        routing_mode="intent",
        gate_status="session_zero",
    )
    registered_managed = (
        set(service.gm_tool_registry._tools)
        & agent._capability_policy.managed_tool_names()
    )
    permitted = {
        name
        for name in registered_managed
        if agent._tool_is_permitted(name, context)
    }

    assert context.metadata["gm_intent_profile_ids"] == ["session_zero_hero"]
    assert permitted == visible


@pytest.mark.parametrize(
    ("routing_mode", "message"),
    [
        ("baseline", "我观察一下牢门。"),
        ("shadow", "我观察一下牢门。"),
        ("intent", "第一章已经开始了吗？"),
        ("intent", "请查询一下伤害规则怎么算。"),
        ("intent", "我观察一下牢门。"),
    ],
)
def test_visible_managed_tools_match_execution_permission(
    tmp_path: Path,
    routing_mode: str,
    message: str,
) -> None:
    service, agent, context, _state, visible = _build_view(
        tmp_path / "permission",
        message=message,
        routing_mode=routing_mode,
    )
    registered_managed = (
        set(service.gm_tool_registry._tools)
        & agent._capability_policy.managed_tool_names()
    )
    permitted = {
        name
        for name in registered_managed
        if agent._tool_is_permitted(name, context)
    }

    assert permitted == visible


def test_full_state_mode_keeps_v4_request_shape_without_delta() -> None:
    client = _ScriptedClient(
        [
            {
                "decision": "final",
                "message_kind": "gm_request",
                "audience": "gm",
                "reply": "当前状态没有变化。",
                "reason": "直接回答。",
            }
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
    context = _context("现在是什么状态？", routing_mode="baseline")
    context.metadata["gm_state_context_mode"] = "full"
    state = {"value": 0, "stable_padding": "x" * 1000}

    outcome = agent.run(
        "现在是什么状态？",
        recent_context="",
        context=context,
        state_summary=state,
    )
    request = json.loads(client.calls[0]["messages"][-1].content)

    assert outcome.reply == "当前状态没有变化。"
    assert request["prompt_layout_version"] == GM_PROMPT_LAYOUT_VERSION
    assert request["current_state_summary"] == state
    assert "turn_state_delta" not in request
    manifest = outcome.trace[-1]["context_manifest"]
    assert manifest["state_context_mode"] == "full"
    assert manifest["prompt_layout_version"] == GM_PROMPT_LAYOUT_VERSION


def test_summary_delta_main_loop_keeps_base_and_reconstructs_latest_state() -> None:
    live_state = {
        "value": 0,
        "stable_padding": "保持基线稳定" * 300,
        "scene": {
            "scene_id": "scene-1",
            "public_facts": ["门仍然关闭"],
        },
    }
    registry = GMToolRegistry()

    def increment(_context: object, _arguments: object) -> GMToolReceipt:
        live_state["value"] = int(live_state["value"]) + 1
        return GMToolReceipt.success(
            "increment",
            result={"value": live_state["value"]},
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="increment",
            description="递增测试状态。",
            handler=increment,
            side_effect="write",
        )
    )
    client = _ScriptedClient(
        [
            {
                "decision": "call_tool",
                "message_kind": "gm_request",
                "audience": "gm",
                "tool_name": "increment",
                "arguments": {},
                "reply": "",
                "reason": "执行权威写入。",
            },
            {
                "decision": "final",
                "message_kind": "gm_request",
                "audience": "gm",
                "reply": "权威状态现在是 1。",
                "reason": "依据刷新后的状态回应。",
            },
        ]
    )
    grounding = _LatestStateGroundingVerifier()
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        reply_grounding_verifier=grounding,
    )
    context = _context("把状态加一。", routing_mode="baseline")
    context.metadata.update(
        {
            "gm_state_context_mode": "summary_delta",
            "_gm_campaign_observed_version": 12,
        }
    )

    outcome = agent.run(
        "把状态加一。",
        recent_context="",
        context=context,
        state_summary=dict(live_state),
        state_summary_provider=lambda: dict(live_state),
    )

    assert outcome.reply == "权威状态现在是 1。"
    assert len(client.calls) == 2
    first_request = json.loads(client.calls[0]["messages"][-1].content)
    second_request = json.loads(client.calls[1]["messages"][-1].content)
    assert first_request["prompt_layout_version"] == GM_DELTA_PROMPT_LAYOUT_VERSION
    assert second_request["prompt_layout_version"] == GM_DELTA_PROMPT_LAYOUT_VERSION
    assert first_request["current_state_summary"] == second_request[
        "current_state_summary"
    ]
    assert first_request["turn_state_delta"]["ops"] == []

    delta = second_request["turn_state_delta"]
    reconstructed = apply_state_delta(
        second_request["current_state_summary"],
        delta["ops"],
    )
    assert reconstructed == live_state
    assert delta["base_hash"] == projection_hash(
        second_request["current_state_summary"]
    )
    assert delta["effective_hash"] == projection_hash(reconstructed)
    assert delta["ops"] == [
        {
            "sequence": 1,
            "op": "replace",
            "path": "/value",
            "source_tool": "increment",
            "value": 1,
        }
    ]

    # Grounding and final-answer logic must use the refreshed authoritative
    # state, not the unchanged delta base sent as current_state_summary.
    assert grounding.observed_states[-1]["value"] == 1
    assert second_request["current_state_summary"]["value"] == 0
    manifest = outcome.trace[-1]["context_manifest"]
    assert manifest["state_context_mode"] == "summary_delta"
    assert manifest["prompt_layout_version"] == GM_DELTA_PROMPT_LAYOUT_VERSION
    assert manifest["state_base_hash"] == delta["base_hash"]
    assert manifest["state_effective_hash"] == delta["effective_hash"]
    assert "current_state_summary" in manifest["protected_paths"]
    assert "turn_state_delta" in manifest["protected_paths"]


def test_delta_context_rebases_on_revision_or_projection_profile_change() -> None:
    agent = LLMGMToolAgent(
        _UnusedClient(),
        model="fake",
        registry=GMToolRegistry(),
    )
    base_state = {
        "scene": {"scene_id": "scene-1"},
        "stable_padding": "x" * 1000,
    }

    revision_context = _context("继续。", routing_mode="baseline")
    revision_context.metadata["_gm_campaign_observed_version"] = 1
    _, _, revision_tracker, cursor = agent._prepare_turn_state_context(
        observed_state=base_state,
        context=revision_context,
        receipts=[],
        tracker=None,
        receipt_cursor=0,
        enabled=True,
    )
    revision_context.metadata["_gm_campaign_observed_version"] = 2
    revision_base, revision_delta, _, _ = agent._prepare_turn_state_context(
        observed_state=base_state,
        context=revision_context,
        receipts=[],
        tracker=revision_tracker,
        receipt_cursor=cursor,
        enabled=True,
    )

    assert revision_base == base_state
    assert revision_delta["ops"] == []
    assert revision_delta["base_revision"] == 2
    assert revision_delta["reset_reason"] == "revision_changed"

    profile_context = _context("转场。", routing_mode="baseline")
    profile_context.metadata["_gm_campaign_observed_version"] = 3
    _, _, profile_tracker, cursor = agent._prepare_turn_state_context(
        observed_state=base_state,
        context=profile_context,
        receipts=[],
        tracker=None,
        receipt_cursor=0,
        enabled=True,
    )
    next_scene_state = {
        "scene": {"scene_id": "scene-2"},
        "stable_padding": "x" * 1000,
    }
    profile_base, profile_delta, _, _ = agent._prepare_turn_state_context(
        observed_state=next_scene_state,
        context=profile_context,
        receipts=[],
        tracker=profile_tracker,
        receipt_cursor=cursor,
        enabled=True,
    )

    assert profile_base == next_scene_state
    assert profile_delta["ops"] == []
    assert profile_delta["reset_reason"] == "profile_changed"


@pytest.mark.parametrize(
    ("identity_field", "replacement"),
    [
        ("campaign_id", "campaign-b"),
        ("session_id", "session-2"),
        ("channel_id", "private-channel-2"),
        ("speaker", "玩家乙"),
        ("speaker_id", "player-2"),
        ("is_private", False),
    ],
)
def test_delta_authority_identity_change_rebases_without_old_private_fact(
    identity_field: str,
    replacement: object,
) -> None:
    agent = LLMGMToolAgent(
        _UnusedClient(),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = _context("继续。", routing_mode="baseline")
    context.is_private = True
    context.metadata.update(
        {
            "speaker_id": "player-1",
            "_gm_campaign_observed_version": 7,
        }
    )
    first_state = {
        "scene": {"scene_id": "shared-scene"},
        "private_model_fact": "CAMPAIGN_A_SECRET",
        "stable_padding": "x" * 1000,
    }
    _, _, tracker, cursor = agent._prepare_turn_state_context(
        observed_state=first_state,
        context=context,
        receipts=[],
        tracker=None,
        receipt_cursor=0,
        enabled=True,
    )

    if identity_field == "speaker_id":
        context.metadata["speaker_id"] = replacement
    else:
        setattr(context, identity_field, replacement)
    second_state = {
        "scene": {"scene_id": "shared-scene"},
        "private_model_fact": "CAMPAIGN_B_SECRET",
        "stable_padding": "x" * 1000,
    }
    next_base, next_delta, _, _ = agent._prepare_turn_state_context(
        observed_state=second_state,
        context=context,
        receipts=[],
        tracker=tracker,
        receipt_cursor=cursor,
        enabled=True,
    )

    wire_payload = json.dumps(
        {"current_state_summary": next_base, "turn_state_delta": next_delta},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert next_base == second_state
    assert next_delta["ops"] == []
    assert "profile" in str(next_delta["reset_reason"] or "")
    assert "CAMPAIGN_A_SECRET" not in wire_payload
    assert "CAMPAIGN_B_SECRET" in wire_payload


def test_shadow_intent_ids_do_not_change_delta_projection_profile() -> None:
    agent = LLMGMToolAgent(
        _UnusedClient(),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = _context("我观察一下牢门。", routing_mode="baseline")
    context.metadata["_gm_campaign_observed_version"] = 4
    state = {
        "observation": {"profile": "hot_compact", "expanded_domains": []},
        "scene": {"scene_id": "scene-1"},
        "stable_padding": "x" * 1000,
    }
    _, baseline_delta, tracker, cursor = agent._prepare_turn_state_context(
        observed_state=state,
        context=context,
        receipts=[],
        tracker=None,
        receipt_cursor=0,
        enabled=True,
    )

    context.metadata.update(
        {
            "gm_capability_routing_mode": "shadow",
            "gm_intent_router_status": "planned",
            "gm_intent_profile_ids": ["check_action"],
            "gm_intent_state_scopes": [
                "decisions",
                "gameplay",
                "kernel",
                "scene",
                "speaker",
            ],
        }
    )
    _, shadow_delta, _, _ = agent._prepare_turn_state_context(
        observed_state=state,
        context=context,
        receipts=[],
        tracker=tracker,
        receipt_cursor=cursor,
        enabled=True,
    )

    assert baseline_delta is not None
    assert shadow_delta is not None
    assert shadow_delta["profile"] == baseline_delta["profile"]
    assert shadow_delta["scopes"] == baseline_delta["scopes"]
    assert shadow_delta["reset_reason"] is None


def test_intent_projection_adds_only_requested_state_sections(
    tmp_path: Path,
) -> None:
    _, _, context, state, _ = _build_view(
        tmp_path / "intent-state-sections",
        message="我观察一下牢门。",
        routing_mode="intent",
    )

    assert context.metadata["gm_intent_profile_ids"] == ["check_action"]
    assert context.metadata["gm_intent_state_scopes"] == [
        "decisions",
        "gameplay",
        "kernel",
        "scene",
        "speaker",
    ]
    assert {"gameplay", "scene", "runtime", "processes"} <= set(state)
    assert set(state).isdisjoint({"npcs", "clocks", "references"})


def test_required_retry_schemas_are_all_execution_permitted(
    tmp_path: Path,
) -> None:
    _, agent, context, state, _ = _build_view(
        tmp_path / "required-retry-permission",
        message="我观察一下牢门。",
        routing_mode="intent",
    )
    receipt = GMToolReceipt.success(
        "declare_check_action",
        result={"required_followup_tools": ["perform_check_action"]},
    )
    GMToolReceiptPolicy.apply_context(context, state, receipt)

    retry_schemas = agent._available_tool_schemas(
        context,
        receipts=[receipt],
        required_retry_tool="perform_check_action",
    )
    visible = {
        str(schema.get("name") or "")
        for schema in retry_schemas
        if str(schema.get("name") or "")
    }
    denied = {
        tool_name
        for tool_name in visible
        if not agent._tool_is_permitted(tool_name, context)
    }

    assert visible == {"discover_capabilities", "perform_check_action"}
    assert denied == set()


def test_write_lease_timeout_never_observes_state_or_calls_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ForbiddenAgent:
        timeout_seconds = 1.0

        def __init__(self) -> None:
            self.calls = 0

        def run(self, *_args: object, **_kwargs: object) -> GMToolAgentOutcome:
            self.calls += 1
            raise AssertionError("agent must not run after write-lease timeout")

    service = FUGMHttpService(
        data_root=str(tmp_path / "lease-fail-closed"),
        use_llm=False,
    )
    agent = _ForbiddenAgent()
    service.gm_tool_agent = agent
    campaign_id = "lease-fail-closed"
    runtime = service._runtime(campaign_id)
    runtime.write_lease_owner = "writer-a"
    service.session_gates.activate(
        campaign_id,
        "group-1",
        "session-1",
        status="adventure",
    )
    gate = service.session_gates.get(campaign_id, "group-1", "session-1")
    state_build_calls: list[GMToolExecutionContext] = []

    def forbidden_state_build(
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        state_build_calls.append(context)
        raise AssertionError("uncommitted runtime must not be projected")

    clock = iter((0.0, 10.0))
    monkeypatch.setattr(
        service.gm_agent_message_coordinator.state_builder,
        "build",
        forbidden_state_build,
    )
    monkeypatch.setattr(
        gm_agent_message_coordinator_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock)),
    )

    response = service.gm_agent_message_coordinator.handle(
        {
            "campaign_id": campaign_id,
            "session_id": "session-1",
            "channel_id": "group-1",
            "speaker": "玩家甲",
            "message": "现在怎样？",
        },
        gate=gate,
        is_private=False,
        explicitly_addressed=True,
        recent_context="",
        record_log=False,
    )

    assert response is not None
    assert agent.calls == 0
    assert state_build_calls == []
    assert response["agent_error"] == "WRITE_LEASE_TIMEOUT"
    assert response["agent_trace"] == [
        {
            "write_lease_timeout": {
                "fail_closed": True,
                "state_observed": False,
            }
        }
    ]


def test_refreshed_state_provider_uses_switched_campaign_lock_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_campaign = "campaign-a"
    target_campaign = "campaign-b"
    service = FUGMHttpService(
        data_root=str(tmp_path / "campaign-switch-provider"),
        use_llm=False,
    )
    source_runtime = service._runtime(source_campaign)
    target_runtime = service._runtime(target_campaign)
    source_runtime.state_version = 3
    target_runtime.state_version = 17
    build_observations: list[tuple[str, bool, bool, int]] = []

    def observed_state_build(
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        build_observations.append(
            (
                context.campaign_id,
                bool(source_runtime.transaction_lock._is_owned()),
                bool(target_runtime.transaction_lock._is_owned()),
                int(
                    context.metadata.get("_gm_campaign_observed_version") or 0
                ),
            )
        )
        return {
            "campaign": context.campaign_id,
            "private_model_fact": f"{context.campaign_id}-secret",
        }

    class _CampaignSwitchingAgent:
        timeout_seconds = 1.0

        def __init__(self) -> None:
            self.refreshed_state: dict[str, object] = {}
            self.refreshed_revision = -1

        def run(self, *_args: object, **kwargs: object) -> GMToolAgentOutcome:
            context = kwargs["context"]
            state_summary = kwargs["state_summary"]
            assert isinstance(context, GMToolExecutionContext)
            assert isinstance(state_summary, dict)
            GMToolReceiptPolicy.apply_context(
                context,
                state_summary,
                GMToolReceipt.success(
                    "load_campaign",
                    result={"active_campaign_id": target_campaign},
                ),
            )
            provider = kwargs["state_summary_provider"]
            self.refreshed_state = provider()
            self.refreshed_revision = int(
                context.metadata.get("_gm_campaign_observed_version") or 0
            )
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                target="silent",
                mode="gm_agent_silent",
                terminal_action="silent",
            )

    agent = _CampaignSwitchingAgent()
    service.gm_tool_agent = agent
    monkeypatch.setattr(
        service.gm_agent_message_coordinator.state_builder,
        "build",
        observed_state_build,
    )
    service.session_gates.activate(
        source_campaign,
        "group-1",
        "session-1",
        status="adventure",
    )
    gate = service.session_gates.get(
        source_campaign,
        "group-1",
        "session-1",
    )

    response = service.gm_agent_message_coordinator.handle(
        {
            "campaign_id": source_campaign,
            "session_id": "session-1",
            "channel_id": "group-1",
            "speaker": "玩家甲",
            "message": "切换后查看。",
        },
        gate=gate,
        is_private=True,
        explicitly_addressed=True,
        recent_context="",
        record_log=False,
    )

    assert response is not None
    assert build_observations == [
        (source_campaign, True, False, 3),
        (target_campaign, False, True, 17),
    ]
    assert agent.refreshed_state == {
        "campaign": target_campaign,
        "private_model_fact": "campaign-b-secret",
    }
    assert agent.refreshed_revision == 17


def test_delta_rebases_across_campaign_and_never_carries_old_private_base() -> None:
    agent = LLMGMToolAgent(
        _UnusedClient(),
        model="fake",
        registry=GMToolRegistry(),
    )
    context = _context("切换战役。", routing_mode="baseline")
    context.campaign_id = "campaign-a"
    context.metadata.update(
        {
            "speaker_id": "principal-1",
            "_gm_campaign_observed_version": 7,
        }
    )
    first_state = {
        "current_campaign_id": "campaign-a",
        "private": "A_SECRET",
        "scene": {"scene_id": "same-scene"},
    }
    _, _, tracker, cursor = agent._prepare_turn_state_context(
        observed_state=first_state,
        context=context,
        receipts=[],
        tracker=None,
        receipt_cursor=0,
        enabled=True,
    )

    context.campaign_id = "campaign-b"
    second_state = {
        "current_campaign_id": "campaign-b",
        "private": "B_SECRET",
        "scene": {"scene_id": "same-scene"},
    }
    base, delta, _, _ = agent._prepare_turn_state_context(
        observed_state=second_state,
        context=context,
        receipts=[],
        tracker=tracker,
        receipt_cursor=cursor,
        enabled=True,
    )

    assert base == second_state
    assert delta["ops"] == []
    assert delta["reset_reason"] == "profile_changed"
    assert "A_SECRET" not in json.dumps(
        {"base": base, "delta": delta},
        ensure_ascii=False,
    )


def test_shadow_delta_profile_is_identical_to_baseline() -> None:
    state = {
        "gate_status": "adventure",
        "observation": {"profile": "hot_compact", "expanded_domains": []},
        "scene": {"scene_id": "scene-1"},
    }
    baseline = _context("我观察牢门。", routing_mode="baseline")
    shadow = _context("我观察牢门。", routing_mode="shadow")
    shadow.metadata["gm_intent_profile_ids"] = ["check_action"]

    assert LLMGMToolAgent._state_projection_profile(
        baseline,
        state,
    ) == LLMGMToolAgent._state_projection_profile(shadow, state)


def test_intent_rule_projection_omits_unrelated_npc_state(tmp_path: Path) -> None:
    _, _, context, state, _ = _build_view(
        tmp_path / "rule-state-scope",
        message="请查询伤害规则怎么算。",
        routing_mode="intent",
    )

    assert context.metadata["gm_intent_profile_ids"] == ["rule_read"]
    assert "npcs" not in state


def test_required_retry_schemas_match_execution_permission(tmp_path: Path) -> None:
    _, agent, context, _state, _ = _build_view(
        tmp_path / "required-retry",
        message="我观察牢门。",
        routing_mode="intent",
    )
    schemas = agent._available_tool_schemas(
        context,
        required_retry_tool="declare_check_action",
    )
    visible = {
        str(schema.get("name") or "")
        for schema in schemas
        if str(schema.get("name") or "")
    }

    assert visible
    assert all(agent._tool_is_permitted(name, context) for name in visible)


def test_write_lease_timeout_fails_closed_without_observing_live_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FUGMHttpService(data_root=tmp_path / "lease", use_llm=False)

    class _NeverRunAgent:
        timeout_seconds = 0.01

        @staticmethod
        def run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("agent must not run against an uncommitted state")

    service.gm_tool_agent = _NeverRunAgent()
    runtime = service._runtime("lease-campaign")
    with runtime.write_lease_condition:
        runtime.write_lease_owner = "another-message"
    gate = service.session_gates.activate(
        "lease-campaign",
        "group-1",
        "session-1",
        status="adventure",
    )

    def _must_not_build(_context: object) -> dict[str, object]:
        raise AssertionError("state builder must not read provisional runtime")

    monkeypatch.setattr(
        service.gm_agent_message_coordinator.state_builder,
        "build",
        _must_not_build,
    )
    clock_calls = iter((0.0, 10.0))
    monkeypatch.setattr(
        "fu_gm.components.gm_agent_message_coordinator.time.monotonic",
        lambda: next(clock_calls, 10.0),
    )
    response = service.gm_agent_message_coordinator._handle_bound(
        {
            "campaign_id": "lease-campaign",
            "session_id": "session-1",
            "channel_id": "group-1",
            "speaker": "玩家甲",
            "speaker_id": "principal-1",
            "message": "现在发生了什么？",
            "message_id": "lease-timeout-1",
        },
        gate=gate,
        is_private=False,
        explicitly_addressed=True,
        recent_context="",
        record_log=False,
    )

    assert response is not None
    assert response["route"] == "gm_agent_unavailable"
    assert response["retry_safe"] is True
    assert response["tool_receipts"] == []
    assert "没有读取或改变状态" in response["reply"]
