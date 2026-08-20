from __future__ import annotations

import json

import pytest

from fu_gm.components.gm_message_integrity import GMMessageIntegrityValidator
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)


def _world_receipt(
    category: str,
    *,
    tool_name: str = "create_world_setting",
    visibility: str = "public",
    authority: str = "player_confirmed",
    event_id: str = "",
) -> GMToolReceipt:
    result: dict[str, object] = {
        "operation": tool_name.removesuffix("_world_setting"),
        "category": category,
        "visibility": visibility,
        "authority": authority,
    }
    if event_id:
        result["source_event"] = {"event_id": event_id}
    return GMToolReceipt.success(
        tool_name,
        result=result,
        state_changed=True,
    )


@pytest.mark.parametrize(
    "message",
    (
        "不要攻击守卫，我先和他谈判。",
        "别开门，我先检查陷阱。",
        "我不想走左边，去右边。",
    ),
)
def test_ordinary_adventure_negation_is_not_a_safety_declaration(
    message: str,
) -> None:
    plan = GMMessageIntegrityValidator.plan(message, gate_status="adventure")

    assert plan.safety_declarations == ()


def test_mixed_committed_country_and_map_proposal_only_blocks_map_writes() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的国家正式定为岚国。地图要不要做成环形大陆，大家觉得呢？",
        gate_status="session_zero",
    )

    assert plan.world_categories == ("kingdoms",)
    assert plan.proposal_subjects == ("world_map",)
    assert (
        GMMessageIntegrityValidator.validate_decision(
            plan,
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"category": "kingdoms"},
            },
        )
        is None
    )

    for category in ("continent_name", "world_shape"):
        issue = GMMessageIntegrityValidator.validate_decision(
            plan,
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"category": category},
            },
        )
        assert issue is not None
        assert issue.error_code == "SESSION_ZERO_PROPOSAL_MISCOMMITTED"


@pytest.mark.parametrize(
    ("message", "subject"),
    (
        ("大家都同意地图用环形大陆。", "world_map"),
        ("你们都赞成小队叫临时守护者。", "group_concept"),
    ),
)
def test_everyone_agrees_is_confirmation_not_a_new_proposal(
    message: str,
    subject: str,
) -> None:
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
    )

    assert plan.proposal is False
    assert plan.proposal_subjects == ()
    assert plan.proposal_confirmation_subjects == (subject,)


@pytest.mark.parametrize(
    ("tool_name", "visibility", "authority"),
    (
        ("delete_world_setting", "public", "player_confirmed"),
        ("create_world_setting", "gm_private", "player_confirmed"),
        ("create_world_setting", "public", "gm_authored"),
    ),
)
def test_delete_private_or_gm_authored_receipt_does_not_cover_player_contribution(
    tool_name: str,
    visibility: str,
    authority: str,
) -> None:
    plan = GMMessageIntegrityValidator.plan(
        "历史事件：旧塔在三十年前坠落。",
        gate_status="session_zero",
    )
    receipt = _world_receipt(
        "historical_events",
        tool_name=tool_name,
        visibility=visibility,
        authority=authority,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [receipt])

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    assert issue.missing == ("historical_events",)


def test_public_player_confirmed_create_still_covers_world_contribution() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "历史事件：旧塔在三十年前坠落。",
        gate_status="session_zero",
    )

    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [_world_receipt("historical_events")],
        )
        is None
    )


def test_semantic_read_request_does_not_manufacture_world_write_obligation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的王国/国家贡献是什么",
        gate_status="session_zero",
    )

    assert plan.world_categories == ("kingdoms",)
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [],
            semantic_message_kind="gm_request",
        )
        is None
    )

    write_issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [],
        semantic_message_kind="state_contribution",
    )
    assert write_issue is not None
    assert write_issue.error_code == "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"


def test_map_location_receipt_never_covers_a_country_contribution() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "我的国家正式定为岚国。",
        gate_status="session_zero",
    )

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [_world_receipt("map_locations")],
        semantic_message_kind="state_contribution",
    )

    assert issue is not None
    assert issue.missing == ("kingdoms",)


def test_retryable_failed_hero_confirmation_does_not_release_terminal_gate() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "确认我的角色并正式建卡。",
        gate_status="session_zero",
    )
    failed = GMToolReceipt.failure(
        "confirm_hero_draft",
        "TEMPORARY_HERO_CONFIRM_FAILURE",
        "角色确认暂时没有写入。",
        "在当前事务内重试确认。",
        retryable=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [failed])

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_CONFIRMATION_INCOMPLETE"


def test_retryable_failed_skill_option_update_does_not_release_terminal_gate() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
    )
    failed = GMToolReceipt.failure(
        "update_hero_draft",
        "TEMPORARY_HERO_UPDATE_FAILURE",
        "角色草稿暂时没有写入。",
        "在当前事务内重试更新。",
        retryable=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [failed])

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_OPTION_INCOMPLETE"


def test_my_hero_confirmation_must_match_the_current_speaker() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "确认我的角色并正式建卡。",
        gate_status="session_zero",
        speaker="澄砚",
    )
    wrong_player = GMToolReceipt.success(
        "confirm_hero_draft",
        result={
            "player_name": "南星",
            "hero_name": "另一位英雄",
            "ready": True,
            "confirmed": True,
        },
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(plan, [wrong_player])

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_HERO_CONFIRMATION_INCOMPLETE"

    matching_player = GMToolReceipt.success(
        "confirm_hero_draft",
        result={
            "player_name": "澄砚",
            "hero_name": "苍祈",
            "ready": True,
            "confirmed": True,
        },
        state_changed=True,
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(plan, [matching_player])
        is None
    )


def test_two_confirmation_subjects_need_two_matching_proposal_receipts() -> None:
    state_summary = {
        "session_zero": {
            "pending_proposals": [
                {
                    "id": "proposal-map",
                    "summary": "地图使用环形大陆",
                },
                {
                    "id": "proposal-group",
                    "summary": "小队叫临时守护者",
                },
            ]
        }
    }
    plan = GMMessageIntegrityValidator.plan(
        "我赞成地图用环形大陆，也同意小队叫临时守护者。",
        gate_status="session_zero",
        state_summary=state_summary,
    )
    map_confirmation = GMToolReceipt.success(
        "confirm_session_zero_proposal",
        result={
            "proposal_id": "proposal-map",
            "proposal_scope_subjects": ["world_map"],
            "proposal_scope_categories": ["continent_name", "world_shape"],
            "proposal_cleared": True,
        },
        state_changed=True,
    )

    issue = GMMessageIntegrityValidator.validate_terminal(
        plan,
        [map_confirmation],
    )

    assert issue is not None
    assert issue.error_code == "SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE"
    assert issue.missing == ("group_concept",)

    group_confirmation = GMToolReceipt.success(
        "confirm_session_zero_proposal",
        result={
            "proposal_id": "proposal-group",
            "proposal_scope_subjects": ["group_concept"],
            "proposal_scope_categories": ["group_concept"],
            "proposal_cleared": True,
        },
        state_changed=True,
    )
    assert (
        GMMessageIntegrityValidator.validate_terminal(
            plan,
            [map_confirmation, group_confirmation],
        )
        is None
    )


@pytest.mark.parametrize(
    "message",
    (
        "我确认角色名字叫苍祈，其他还没定。",
        "我确认这个角色的主题是责任。",
    ),
)
def test_confirming_one_hero_field_is_not_whole_sheet_confirmation(
    message: str,
) -> None:
    plan = GMMessageIntegrityValidator.plan(
        message,
        gate_status="session_zero",
    )

    assert plan.hero_confirmation_required is False


def test_local_negation_does_not_cancel_later_explicit_hero_confirmation() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "不要确认阿甲；确认我的角色并正式建卡。",
        gate_status="session_zero",
    )

    assert plan.hero_confirmation_required is True


def test_correct_skill_option_can_share_patch_with_authorized_base_attributes() -> None:
    plan = GMMessageIntegrityValidator.plan(
        "基础属性：敏捷8、洞察8、力量10、意志6；"
        "拟兽系仪式的施法属性我选洞察+意志。",
        gate_status="session_zero",
        source_event_id="event-hero",
    )
    decision = {
        "decision": "call_tool",
        "tool_name": "update_hero_draft",
        "arguments": {
            "source_event_id": "event-hero",
            "subject": "澄砚",
            "patch": {
                "attributes": {
                    "敏捷": 8,
                    "洞察": 8,
                    "力量": 10,
                    "意志": 6,
                },
                "skill_options": {"拟兽系仪式": ["洞察+意志"]},
            },
        },
    }

    assert GMMessageIntegrityValidator.validate_decision(plan, decision) is None


class _ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]

    def create_chat_completion(self, **_kwargs: object) -> str:
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        return self.responses.pop(0)


class _SnapshotTransaction:
    def __init__(self, state: list[str]) -> None:
        self.state = state
        self.before = list(state)
        self.active = True

    def commit(self) -> None:
        self.active = False

    def rollback(self) -> None:
        if self.active:
            self.state[:] = self.before
            self.active = False


def _multi_event_world_registry(state: list[str]) -> GMToolRegistry:
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: _SnapshotTransaction(state)
    )

    def create_world(
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        category = str(arguments["category"])
        event_id = str(context.metadata.get("source_event_id") or "")
        state.append(f"{event_id}:{category}")
        return GMToolReceipt.success(
            "create_world_setting",
            result={
                "operation": "create",
                "category": category,
                "visibility": "public",
                "authority": "player_confirmed",
            },
            state_changed=True,
        )

    registry.register(
        GMToolDefinition(
            name="create_world_setting",
            description="记录玩家已经确认的世界贡献。",
            handler=create_world,
            parameters=(
                GMToolParameter(
                    "category",
                    "string",
                    "世界设定类别。",
                    required=True,
                    enum=("kingdoms", "historical_events"),
                ),
            ),
            side_effect="write",
            max_successful_calls_per_message=8,
        )
    )
    return registry


def test_each_event_in_multi_message_turn_keeps_its_own_integrity_plan() -> None:
    state: list[str] = []
    client = _ScriptedClient(
        [
            {
                "decision": "call_tools",
                "calls": [
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "category": "kingdoms",
                            "source_event_id": "event-a",
                        },
                    },
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {
                            "category": "kingdoms",
                            "source_event_id": "event-b",
                        },
                    },
                ],
                "terminal_decision": "final",
                "reply": "两位的国家都记下了。",
            },
            {"decision": "final", "reply": "已经完整处理。"},
            {"decision": "final", "reply": "已经完整处理。"},
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=_multi_event_world_registry(state),
        max_iterations=1,
    )
    context = GMToolExecutionContext(
        campaign_id="multi-event-integrity",
        session_id="s0",
        channel_id="group-1",
        speaker="南星",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={
            "current_turn_events": [
                {
                    "event_id": "event-a",
                    "speaker": "澄砚",
                    "text": (
                        "我的国家是岚国。"
                        "重大历史事件：旧塔在三十年前坠落。"
                    ),
                },
                {
                    "event_id": "event-b",
                    "speaker": "南星",
                    "text": "我的国家是海国。",
                },
            ]
        },
    )

    outcome = agent.run(
        "我的国家是海国。",
        recent_context="",
        context=context,
        state_summary={},
    )

    assert outcome.mode == "gm_agent_message_transaction_rolled_back"
    assert "SESSION_ZERO_CONTRIBUTION_INCOMPLETE" in outcome.error
    assert outcome.reply == "刚才这件事没有处理完整，存档没有改动。麻烦再说一次。"
    assert "回执" not in outcome.reply
    assert "回滚" not in outcome.reply
    assert state == []
