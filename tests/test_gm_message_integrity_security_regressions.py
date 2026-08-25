from __future__ import annotations

import json
from copy import deepcopy

from fu_gm.components.gm_message_integrity import GMMessageIntegrityValidator
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.http_server import FUGMHttpService


class _ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        return self.responses.pop(0)


def _context(
    message: str,
    *,
    speaker: str = "白河",
    campaign_id: str = "完整性安全回归团",
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id="s0",
        channel_id="group-1",
        speaker=speaker,
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": message},
    )


def _pending_map_proposals() -> list[dict[str, object]]:
    return [
        {
            "id": "proposal-map-a",
            "speaker": "白河",
            "summary": "地图使用环形大陆",
            "proposed_updates": {"world_shape": "环形大陆"},
        },
        {
            "id": "proposal-map-b",
            "speaker": "南星",
            "summary": "地图使用碎裂群岛",
            "proposed_updates": {"world_shape": "碎裂群岛"},
        },
    ]


def _confirmation_receipt(
    proposal_id: str,
    subject: str,
    *,
    event_id: str = "",
) -> GMToolReceipt:
    result: dict[str, object] = {
        "proposal_id": proposal_id,
        "proposal_scope_subjects": [subject],
        "proposal_cleared": True,
    }
    if event_id:
        result["source_event"] = {"event_id": event_id}
    return GMToolReceipt.success(
        "confirm_session_zero_proposal",
        result=result,
        state_changed=True,
    )


def test_multi_event_python_auto_followup_inherits_confirmation_event_id() -> None:
    observed: list[dict[str, str]] = []
    registry = GMToolRegistry()

    def confirm(
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        observed.append(
            {
                "tool": "confirm",
                "event_id": str(context.metadata.get("source_event_id") or ""),
                "speaker": context.speaker,
            }
        )
        return GMToolReceipt(
            tool_name="confirm_session_zero_proposal",
            ok=True,
            state_changed=True,
            result={
                "proposal_id": "proposal-map",
                "proposal_scope_subjects": ["world_map"],
                "proposal_cleared": True,
                "required_followup_tools": ["create_world_setting"],
                "required_followup_calls": [
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "marker": "confirmed-map",
                            # A signed packet must not be able to retain stale
                            # provenance from a different debounced event.
                            "source_event_id": "event-a",
                        },
                        "python_auto_execute": True,
                    }
                ],
                "required_followup_mode": "all",
                "python_auto_followup_terminal": True,
            },
            public_fallback_reply="地图提案已经确认。",
            lock_public_reply=True,
        )

    def create(
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        observed.append(
            {
                "tool": "create",
                "event_id": str(context.metadata.get("source_event_id") or ""),
                "speaker": context.speaker,
                "marker": str(arguments.get("marker") or ""),
            }
        )
        return GMToolReceipt.success(
            "create_world_setting",
            result={
                "operation": "create",
                "category": "world_shape",
                "visibility": "public",
                "authority": "table_consensus",
            },
            state_changed=True,
            public_reply="地图设定已经写入。",
            lock_public_reply=True,
        )

    registry.register(
        GMToolDefinition(
            name="confirm_session_zero_proposal",
            description="确认待定提案。",
            handler=confirm,
            parameters=(
                GMToolParameter(
                    "proposal_id",
                    "string",
                    "待定提案ID。",
                    required=True,
                ),
            ),
            side_effect="write_pending",
        )
    )
    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="执行Python签发的确定性地图写入。",
            handler=create,
            parameters=(
                GMToolParameter("marker", "string", "签发标记。", required=True),
            ),
            side_effect="write",
        )
    )
    client = _ScriptedClient(
        [
            {
                "decision": "call_tool",
                "tool_name": "confirm_session_zero_proposal",
                "arguments": {
                    "proposal_id": "proposal-map",
                    "source_event_id": "event-b",
                },
            }
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=registry)
    context = GMToolExecutionContext(
        campaign_id="multi-event-confirm",
        session_id="s0",
        channel_id="group-1",
        speaker="南星",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={
            "current_turn_events": [
                {
                    "event_id": "event-a",
                    "speaker": "白河",
                    "text": "我先想想。",
                },
                {
                    "event_id": "event-b",
                    "speaker": "南星",
                    "text": "我赞成这个地图提案。",
                    "is_at_gm": True,
                },
            ]
        },
    )

    outcome = agent.run(
        "我赞成这个地图提案。",
        recent_context="",
        context=context,
        state_summary={
            "session_zero": {
                "pending_proposals": [
                    {
                        "id": "proposal-map",
                        "summary": "地图使用环形大陆",
                        "scope_subjects": ["world_map"],
                    }
                ]
            }
        },
    )

    assert [item["tool"] for item in observed] == ["confirm", "create"]
    assert {item["event_id"] for item in observed} == {"event-b"}
    assert {item["speaker"] for item in observed} == {"南星"}
    assert outcome.trace[0]["python_signed_followups"][0]["arguments"][
        "source_event_id"
    ] == "event-b"
    assert [
        receipt.result["source_event"]["event_id"]
        for receipt in outcome.receipts
    ] == ["event-b", "event-b"]


def test_update_hero_draft_rejects_cross_player_but_allows_owner(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("完整性安全回归团")
    runtime.app.initialize_session_zero(participants=["白河", "南星"])

    created = service.gm_tool_registry.execute(
        "update_hero_draft",
        {"subject": "白河", "patch": {"hero_name": "苍祈"}},
        _context("角色名：苍祈。", speaker="白河"),
    )
    assert created.ok, created.to_dict()

    rejected = service.gm_tool_registry.execute(
        "update_hero_draft",
        {"subject": "白河", "patch": {"theme": "南星代写的主题"}},
        _context("把白河的主题改掉。", speaker="南星"),
    )

    assert not rejected.ok
    assert rejected.error_code == "HERO_DRAFT_UPDATE_NOT_OWNER"
    draft = runtime.app.session_zero_manager.state.world.hero_drafts["白河"]
    assert draft.theme == ""

    accepted = service.gm_tool_registry.execute(
        "update_hero_draft",
        {"subject": "白河", "patch": {"theme": "守护失落之名"}},
        _context("我的主题：守护失落之名。", speaker="白河"),
    )

    assert accepted.ok, accepted.to_dict()
    assert (
        runtime.app.session_zero_manager.state.world.hero_drafts["白河"].theme
        == "守护失落之名"
    )


def test_create_proposal_cannot_be_replaced_by_same_category_delete(tmp_path) -> None:
    service = FUGMHttpService(data_root=str(tmp_path), use_llm=False)
    runtime = service._runtime("完整性安全回归团")
    runtime.app.initialize_session_zero(participants=["白河", "南星"])
    created = service.gm_tool_registry.execute(
        "create_world_setting",
        {
            "category": "kingdoms",
            "name": "旧塔国",
            "value": "以旧塔为中心的城邦。",
            "visibility": "public",
            "authority": "player_confirmed",
            "reason": "白河明确贡献旧塔国。",
        },
        _context("我的国家是旧塔国。", speaker="白河"),
    )
    assert created.ok, created.to_dict()
    proposed = service.gm_tool_registry.execute(
        "propose_session_zero_update",
        {
            "summary": "新增雾港国",
            "world_operations": [
                {
                    "operation": "create",
                    "category": "kingdoms",
                    "name": "雾港国",
                    "value": "以雾港贸易为核心的新国家。",
                    "visibility": "public",
                }
            ],
        },
        _context("我提议新增雾港国，大家觉得呢？", speaker="白河"),
    )
    assert proposed.ok, proposed.to_dict()
    proposal_id = str(proposed.result["proposal"]["id"])
    world = runtime.app.session_zero_manager.state.world
    before_pending = deepcopy(world.pending_proposals)
    before_kingdoms = deepcopy(world.kingdoms)

    rejected = service.gm_tool_registry.execute(
        "confirm_session_zero_proposal",
        {
            "proposal_id": proposal_id,
            "replacement_world_operations": [
                {
                    "operation": "delete",
                    "category": "kingdoms",
                    "name": "旧塔国",
                    "visibility": "public",
                }
            ],
        },
        _context(
            "我赞成这个国家方向，但改成删除旧塔国。",
            speaker="南星",
        ),
    )

    assert not rejected.ok
    assert rejected.error_code == "PROPOSAL_REPLACEMENT_OPERATION_MISMATCH"
    assert world.pending_proposals == before_pending
    assert world.kingdoms == before_kingdoms
    assert "旧塔国" in world.kingdoms
    assert "雾港国" not in world.kingdoms


def test_map_and_group_confirms_in_one_sentence_do_not_mismatch_each_other() -> None:
    state_summary = {
        "pending_proposals": [
            {
                "id": "proposal-map",
                "summary": "地图使用环形大陆",
                "scope_subjects": ["world_map"],
            },
            {
                "id": "proposal-group",
                "summary": "小队是临时守护者",
                "scope_subjects": ["group_concept"],
            },
        ]
    }
    plan = GMMessageIntegrityValidator.plan(
        "我赞成地图用环形大陆，也同意小队叫临时守护者。",
        gate_status="session_zero",
        state_summary=state_summary,
    )
    decision = {
        "decision": "call_tools",
        "calls": [
            {
                "tool_name": "confirm_session_zero_proposal",
                "arguments": {"proposal_id": "proposal-map"},
            },
            {
                "tool_name": "confirm_session_zero_proposal",
                "arguments": {"proposal_id": "proposal-group"},
            },
        ],
    }

    assert GMMessageIntegrityValidator.validate_decision(plan, decision) is None
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [
                _confirmation_receipt("proposal-map", "world_map"),
                _confirmation_receipt("proposal-group", "group_concept"),
            ],
        )
        is None
    )


def test_new_proposal_message_cannot_also_confirm_an_old_proposal() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我提议地图改成三角大陆，大家觉得呢？",
        gate_status="session_zero",
        state_summary={
            "pending_proposals": [
                {
                    "id": "proposal-old",
                    "summary": "地图使用环形大陆",
                    "scope_subjects": ["world_map"],
                }
            ]
        },
    )
    decision = {
        "decision": "call_tools",
        "calls": [
            {
                "tool_name": "propose_session_zero_update",
                "arguments": {
                    "summary": "地图使用三角大陆",
                    "updates": {"world_shape": "三角大陆"},
                },
            },
            {
                "tool_name": "confirm_session_zero_proposal",
                "arguments": {"proposal_id": "proposal-old"},
            },
        ],
    }

    issue = GMMessageIntegrityValidator.validate_decision(plan, decision)

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_MISCOMMITTED"
    assert issue.details["submitted_proposal_ids"] == ["proposal-old"]


def test_same_subject_pending_proposals_disambiguate_by_speaker_anchor_and_recency() -> None:
    state_summary = {"pending_proposals": _pending_map_proposals()}
    cases = (
        ("我赞成白河的地图提案。", "proposal-map-a"),
        ("我赞成地图使用碎裂群岛。", "proposal-map-b"),
        ("我赞成刚才的地图提案。", "proposal-map-b"),
    )

    for message, expected_id in cases:
        plan = GMMessageIntegrityValidator.plan(
            message,
            gate_status="session_zero",
            state_summary=state_summary,
        )
        assert len(plan.proposal_confirmations) == 1
        requirement = plan.proposal_confirmations[0]
        assert requirement.ambiguous is False
        assert requirement.proposal_ids == (expected_id,)


def test_multiple_same_subject_candidates_are_left_for_semantic_resolution() -> None:
    state_summary = {"pending_proposals": _pending_map_proposals()}
    message = "我赞成这个地图提案。"
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
        state_summary=state_summary,
    )

    requirement = plan.proposal_confirmations[0]
    assert requirement.ambiguous is False
    assert set(requirement.proposal_ids) == {
        "proposal-map-a",
        "proposal-map-b",
    }

    client = _ScriptedClient(
        [
            {
                "decision": "ask_user",
                "reply": "你赞成白河的环形大陆，还是南星的碎裂群岛？",
            }
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
    outcome = agent.run(
        message,
        recent_context="",
        context=_context(message, speaker="阿凛"),
        state_summary=state_summary,
    )

    assert outcome.reply == "你赞成白河的环形大陆，还是南星的碎裂群岛？"
    assert all(
        "proposal-map-" not in str(step.get("model_reply") or "")
        for step in outcome.trace
    )


def test_same_debounce_later_approval_dynamically_binds_earlier_proposal_receipt() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成刚才的地图提案。",
        gate_status="session_zero",
        source_event_id="event-b",
        strict_source_event=True,
        prior_source_event_ids=("event-a",),
        speaker="南星",
        state_summary={"session_zero": {"pending_proposals": []}},
    )
    proposed = GMToolReceipt.success(
        "propose_session_zero_update",
        result={
            "proposal": {
                "id": "proposal-live",
                "speaker": "白河",
                "summary": "地图使用环形大陆",
                "world_operations": [
                    {
                        "operation": "create",
                        "category": "world_shape",
                        "name": "大陆轮廓",
                        "value": "环形大陆",
                        "visibility": "public",
                    }
                ],
            },
            "source_event": {"event_id": "event-a"},
        },
        state_changed=True,
    )
    decision = {
        "decision": "call_tool",
        "tool_name": "confirm_session_zero_proposal",
        "arguments": {
            "proposal_id": "proposal-live",
            "source_event_id": "event-b",
        },
    }

    assert (
        GMMessageIntegrityValidator.validate_decision(plan, decision, [proposed])
        is None
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [
                proposed,
                _confirmation_receipt(
                    "proposal-live",
                    "world_map",
                    event_id="event-b",
                ),
            ],
        )
        is None
    )


def test_revision_in_a_later_sentence_still_requires_replacement_operations() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成这个地图提案。细化为鸦羽山脉和镜线内海。",
        gate_status="session_zero",
        state_summary={
            "pending_proposals": [
                {
                    "id": "proposal-map",
                    "summary": "地图使用西部山脉和中央内海",
                    "scope_subjects": ["world_map"],
                }
            ]
        },
    )
    decision = {
        "decision": "call_tool",
        "tool_name": "confirm_session_zero_proposal",
        "arguments": {"proposal_id": "proposal-map"},
    }

    issue = GMMessageIntegrityValidator.validate_decision(plan, decision)

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_REVISION_INCOMPLETE"
    assert issue.missing == ("replacement_world_operations",)


def test_vague_proposal_summary_can_match_authoritative_scope_subjects() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我赞成这个地图提案。",
        gate_status="session_zero",
        state_summary={
            "pending_proposals": [
                {
                    "id": "proposal-vague",
                    "summary": "我刚才说的那个方案",
                    "scope_subjects": ["world_map"],
                }
            ]
        },
    )

    assert len(plan.proposal_confirmations) == 1
    assert plan.proposal_confirmations[0].proposal_ids == ("proposal-vague",)


def test_commit_session_zero_update_applied_fields_cover_world_contribution() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "重大历史事件：旧塔在三十年前坠落。",
        gate_status="session_zero",
    )
    committed = GMToolReceipt.success(
        "commit_session_zero_update",
        result={"applied_fields": ["historical_events"]},
        state_changed=True,
    )

    assert GMMessageIntegrityValidator.validate_terminal(plan, [committed]) is None


def test_tactical_discomfort_is_not_persisted_as_a_safety_boundary() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "这个包抄战术让我不舒服，我们换成正面谈判吧。",
        gate_status="adventure",
    )

    assert plan.safety_declarations == ()


def test_explicit_single_attribute_and_skill_option_can_share_one_patch() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "力量改成d10；拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
        source_event_id="event-hero",
    )
    decision = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "source_event_id": "event-hero",
            "subject": "白河",
            "patch": {
                "attributes": {"力量": 10},
                "skill_options": {"拟兽系仪式": ["洞察+意志"]},
            },
        },
    }

    assert plan.hero_attributes_explicit is True
    assert GMMessageIntegrityValidator.validate_decision(plan, decision) is None


def test_strict_multi_event_plan_rejects_receipt_with_empty_source_event() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "重大历史事件：旧塔在三十年前坠落。",
        gate_status="session_zero",
        source_event_id="event-a",
        strict_source_event=True,
    )
    unbound = GMToolReceipt.success(
        "create_world_setting",
        result={
            "operation": "create",
            "category": "historical_events",
            "visibility": "public",
            "authority": "player_confirmed",
        },
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [unbound])

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("historical_events",)
