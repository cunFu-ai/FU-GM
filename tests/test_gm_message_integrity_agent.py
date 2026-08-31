from __future__ import annotations

import json
import tempfile
from copy import deepcopy

from fu_gm.components.gm_reply_grounding_verifier import GMReplyGroundingReview
from fu_gm.gm_tool_agent import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
    LLMGMToolAgent,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import HeroDraft


class ScriptedClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]
        self.calls: list[dict[str, object]] = []

    def create_chat_completion(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        if not self.responses:
            raise AssertionError("缺少脚本化模型响应。")
        return self.responses.pop(0)


class SnapshotTransaction:
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


def session_zero_context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="integrity-test",
        session_id="s0",
        channel_id="group-1",
        speaker="澄砚",
        gate_status="session_zero",
        directly_addressed=True,
    )


def test_country_contribution_query_finishes_without_a_write_receipt() -> None:
    client = ScriptedClient(
        [
            {
                "decision": "final",
                "message_kind": "gm_request",
                "audience": "gm",
                "reply": "你目前还没有国家贡献。",
            }
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=GMToolRegistry(),
        max_iterations=2,
    )

    outcome = agent.run(
        "我的王国/国家贡献是什么",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert outcome.reply == "你目前还没有国家贡献。"
    assert outcome.mode == "gm_agent_reply"
    assert len(client.calls) == 1


def test_country_query_keeps_first_semantic_kind_after_read_tool() -> None:
    registry = GMToolRegistry()
    registry.register(
        GMToolDefinition(
            name="get_session_zero_contributions",
            description="测试只读贡献查询。",
            parameters=(),
            side_effect="read",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "get_session_zero_contributions",
                result={
                    "player": "澄砚",
                    "topics": [
                        {
                            "topic": "kingdom",
                            "status": "pending",
                            "values": [],
                        }
                    ],
                },
            ),
        )
    )
    client = ScriptedClient(
        [
            {
                "decision": "call_tool",
                "message_kind": "gm_request",
                "audience": "gm",
                "tool_name": "get_session_zero_contributions",
                "arguments": {},
            },
            {
                "decision": "final",
                "audience": "gm",
                "reply": "你目前还没有国家贡献。",
            },
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        max_iterations=2,
    )

    outcome = agent.run(
        "我的王国/国家贡献是什么",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert outcome.reply == "你目前还没有国家贡献。"
    assert outcome.mode == "gm_agent_tool"
    assert len(client.calls) == 2


_GENERIC_MAP_NAMES = (
    "西部山脉",
    "中央内海",
    "南部海岸",
    "南部驿站",
    "东南群岛",
)
_REFINED_MAP_NAMES = (
    "鸦羽山脉",
    "镜线内海",
    "雾潮海岸",
    "白花碑驿站",
    "潮鸢群岛",
)


def _real_session_zero_context(
    message: str,
    *,
    speaker: str = "澄砚",
) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="integrity-test",
        session_id="s0",
        channel_id="group-1",
        speaker=speaker,
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": message},
    )


def _legacy_map_updates() -> dict[str, object]:
    feature_types = (
        "mountain_range",
        "inland_sea",
        "coast",
        "settlement",
        "archipelago",
    )
    positions = ("west", "center", "south", "south", "southeast")
    return {
        "map_locations": [
            {
                "name": name,
                "description": f"待全桌确认后细化的{name}。",
                "feature_type": feature_type,
                "terrain": "待定",
                "position_hint": position,
            }
            for name, feature_type, position in zip(
                _GENERIC_MAP_NAMES,
                feature_types,
                positions,
            )
        ]
    }


def _replacement_map_operations() -> list[dict[str, object]]:
    specs = (
        ("鸦羽山脉", "mountain_range", "西部", "west"),
        ("镜线内海", "inland_sea", "中央", "center"),
        ("雾潮海岸", "coast", "南岸", "south"),
        ("白花碑驿站", "settlement", "南岸", "south"),
        ("潮鸢群岛", "archipelago", "东南", "southeast"),
    )
    return [
        {
            "operation": "create",
            "category": "map_locations",
            "name": name,
            "value": f"{location}的{name}",
            "attributes": {
                "feature_type": feature_type,
                "position_hint": position,
            },
            "visibility": "public",
        }
        for name, feature_type, location, position in specs
    ]


def _seed_legacy_map_proposal(service: FUGMHttpService) -> str:
    proposal_message = (
        "我先提议地图用西部山脉、中央内海、南部海岸、"
        "南部驿站和东南群岛。"
    )
    proposed = service.gm_tool_registry.execute(
        "propose_session_zero_update",
        {
            "summary": "待定的大陆地图节点",
            "updates": _legacy_map_updates(),
        },
        _real_session_zero_context(proposal_message),
    )
    assert proposed.ok, proposed.to_dict()
    return str(proposed.result["proposal"]["id"])


def world_registry(state: list[str]) -> GMToolRegistry:
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: SnapshotTransaction(state)
    )

    def create_world(_context, arguments):
        category = str(arguments["category"])
        state.append(category)
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
            description="记录一项玩家已经确认的世界设定。",
            handler=create_world,
            parameters=(
                GMToolParameter(
                    "category",
                    "string",
                    "世界设定类别。",
                    required=True,
                    enum=(
                        "kingdoms",
                        "historical_events",
                        "mysteries",
                        "world_threats",
                        "playstyle_themes",
                    ),
                ),
            ),
            side_effect="write",
            max_successful_calls_per_message=12,
        )
    )
    return registry


def request_payload(client: ScriptedClient, call_index: int) -> dict[str, object]:
    return json.loads(client.calls[call_index]["messages"][-1].content)


def test_partial_world_write_is_repaired_inside_the_same_agent_run() -> None:
    state: list[str] = []
    client = ScriptedClient(
        [
            {
                "decision": "call_tools",
                "calls": [
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {"category": "kingdoms"},
                    },
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {"category": "mysteries"},
                    },
                    {
                        "tool_name": "create_world_setting",
                        "arguments": {"category": "world_threats"},
                    },
                ],
                "terminal_decision": "final",
                "reply": "都记下了。",
            },
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"category": "historical_events"},
            },
            {
                "decision": "final",
                "reply": "这次四类贡献都已经记下。",
            },
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=world_registry(state),
        max_iterations=4,
    )

    outcome = agent.run(
        (
            "我贡献一个国家：潮钟国。重大历史事件：老王病倒后海图被抵押。"
            "奥秘：海图为何会流血？威胁：财团会吞并所有港口。"
        ),
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert outcome.reply == "这次四类贡献都已经记下。"
    assert state == [
        "kingdoms",
        "mysteries",
        "world_threats",
        "historical_events",
    ]
    retry = request_payload(client, 1)
    assert retry["history"][-1]["protocol_error"]["error_code"] == (
        "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    )
    assert retry["history"][-1]["protocol_error"]["missing"] == [
        "historical_events"
    ]
    assert not any(receipt.result.get("rolled_back") for receipt in outcome.receipts)


def test_unrepaired_world_write_rolls_back_with_explicit_public_error() -> None:
    state: list[str] = []
    client = ScriptedClient(
        [
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"category": "kingdoms"},
            },
            {"decision": "final", "reply": "已经处理。"},
            {"decision": "final", "reply": "已经处理。"},
            {"decision": "final", "reply": "已经处理。"},
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=world_registry(state),
        max_iterations=2,
    )

    outcome = agent.run(
        "我贡献一个国家：潮钟国。重大历史事件：老王病倒后海图被抵押。",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert state == []
    assert outcome.mode == "gm_agent_message_transaction_rolled_back"
    assert "SESSION_ZERO_CONTRIBUTION_INCOMPLETE" in outcome.error
    assert outcome.reply == "刚才这件事没有处理完整，存档没有改动。麻烦再说一次。"
    assert "回执" not in outcome.reply
    assert "回滚" not in outcome.reply
    assert len(outcome.receipts) == 1
    assert outcome.receipts[0].state_changed is False
    assert outcome.receipts[0].result["rolled_back"] is True


def test_playstyle_preference_without_receipt_is_repaired_with_crud() -> None:
    state: list[str] = []
    client = ScriptedClient(
        [
            {"decision": "final", "reply": "记下了。"},
            {
                "decision": "call_tool",
                "tool_name": "create_world_setting",
                "arguments": {"category": "playstyle_themes"},
            },
            {"decision": "final", "reply": "玩法偏好已经正式记下。"},
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=world_registry(state),
        max_iterations=3,
    )

    outcome = agent.run(
        "时悠，请正式记进玩法偏好：我希望第一章至少有一场冲突不靠战斗解决。",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert state == ["playstyle_themes"]
    assert outcome.reply == "玩法偏好已经正式记下。"
    retry = request_payload(client, 1)
    assert retry["history"][-1]["protocol_error"]["error_code"] == (
        "SESSION_ZERO_CONTRIBUTION_INCOMPLETE"
    )
    assert retry["history"][-1]["protocol_error"]["missing"] == [
        "playstyle_themes"
    ]


def test_explicit_group_proposal_is_persisted_inside_the_same_agent_run() -> None:
    state: list[str] = []
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: SnapshotTransaction(state)
    )
    registry.register(
        GMToolDefinition(
            name="propose_session_zero_update",
            description="保存尚待全桌确认的第零章提案。",
            handler=lambda _context, _arguments: (
                state.append("pending_group_proposal")
                or GMToolReceipt.success(
                    "propose_session_zero_update",
                    result={
                        "proposal": {
                            "proposed_updates": {
                                "group_concept": "白花碑驿站的临时守护者"
                            }
                        }
                    },
                    state_changed=True,
                )
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            {"decision": "final", "reply": "大家觉得呢？"},
            {
                "decision": "call_tool",
                "tool_name": "propose_session_zero_update",
                "arguments": {},
            },
            {"decision": "final", "reply": "这条仍是待定提案。"},
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        max_iterations=3,
    )

    outcome = agent.run(
        "小队我先提个还没定的方向：大家是在白花碑驿站临时结成的守护者。"
        "你们觉得合适吗？",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert state == ["pending_group_proposal"]
    assert outcome.mode != "gm_agent_message_transaction_rolled_back"
    retry = request_payload(client, 1)
    assert retry["history"][-1]["protocol_error"]["error_code"] == (
        "SESSION_ZERO_PROPOSAL_INCOMPLETE"
    )


def test_explicit_group_agreement_is_confirmed_inside_the_same_agent_run() -> None:
    state: list[str] = []
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: SnapshotTransaction(state)
    )
    registry.register(
        GMToolDefinition(
            name="confirm_session_zero_proposal",
            description="确认一个已经存在的第零章待定提案。",
            handler=lambda _context, _arguments: (
                state.append("group_proposal_confirmed")
                or GMToolReceipt.success(
                    "confirm_session_zero_proposal",
                    result={"proposal_id": "proposal-group-1"},
                    state_changed=True,
                )
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            {"decision": "final", "reply": "好，就这么定。"},
            {
                "decision": "call_tool",
                "tool_name": "confirm_session_zero_proposal",
                "arguments": {},
            },
            {"decision": "final", "reply": "小队方向已经正式确认。"},
        ]
    )
    agent = LLMGMToolAgent(
        client,
        model="fake",
        registry=registry,
        max_iterations=3,
    )

    outcome = agent.run(
        "我赞成白河的小队方向。我们就是在白花碑驿站结成的临时守护者。",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert state == ["group_proposal_confirmed"]
    assert outcome.mode != "gm_agent_message_transaction_rolled_back"
    retry = request_payload(client, 1)
    assert retry["history"][-1]["protocol_error"]["error_code"] == (
        "SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE"
    )


def test_known_map_proposal_blocks_direct_crud_then_auto_applies_replacement() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("integrity-test")
        runtime.app.initialize_session_zero(participants=["澄砚", "白河"])
        proposal_id = _seed_legacy_map_proposal(service)
        message = (
            "我赞成这个地图提案，但细化为：西部山脉改成鸦羽山脉，"
            "中央内海改成镜线内海，南部海岸改成雾潮海岸，"
            "南部驿站改成白花碑驿站，东南群岛改成潮鸢群岛。"
        )
        direct_arguments = {
            key: value
            for key, value in _replacement_map_operations()[0].items()
            if key != "operation"
        }
        direct_arguments.update(
            {
                "authority": "table_consensus",
                "reason": "玩家赞成地图提案并细化。",
            }
        )
        client = ScriptedClient(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "create_world_setting",
                    "arguments": direct_arguments,
                },
                {
                    "decision": "call_tool",
                    "tool_name": "confirm_session_zero_proposal",
                    "arguments": {
                        "proposal_id": proposal_id,
                        "replacement_world_operations": (
                            _replacement_map_operations()
                        ),
                    },
                },
                {
                    "decision": "final",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "reply": "这版地图就这样定下了。",
                },
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=service.gm_tool_registry,
            max_iterations=3,
        )
        tool_context = _real_session_zero_context(message, speaker="白河")
        state_summary = {
            "session_zero": service.gm_session_zero_tools.state_summary(
                tool_context
            )
        }

        outcome = agent.run(
            message,
            recent_context="",
            context=tool_context,
            state_summary=state_summary,
        )

        assert len(client.calls) == 3
        assert outcome.mode == "gm_agent_tool"
        assert outcome.reply == "这版地图就这样定下了。"
        assert [receipt.tool_name for receipt in outcome.receipts] == [
            "confirm_session_zero_proposal",
            *("create_world_setting" for _ in _REFINED_MAP_NAMES),
        ]
        preflight = next(
            item
            for item in outcome.trace
            if item.get("protocol_error")
            == "SESSION_ZERO_PROPOSAL_CONFIRMATION_MISCOMMITTED"
        )
        assert preflight["message_integrity"]["phase"] == "pre_execution"
        world = runtime.app.world_state.world_profile
        assert world.pending_proposals == []
        assert set(runtime.app.world_state.map_locations) == set(
            _REFINED_MAP_NAMES
        )
        assert not (
            set(runtime.app.world_state.map_locations)
            & set(_GENERIC_MAP_NAMES)
        )


def test_revision_retry_commits_map_consensus_and_explicit_world_shape() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("integrity-test")
        runtime.app.initialize_session_zero(participants=["澄砚", "白河", "南星"])
        proposed = service.gm_tool_registry.execute(
            "propose_session_zero_update",
            {
                "summary": "白钟大陆与待定的大陆地图节点",
                "updates": {
                    "continent_name": "白钟大陆",
                    **_legacy_map_updates(),
                },
            },
            _real_session_zero_context(
                "我先提议大陆叫白钟大陆，并采用一组待定地图节点。",
                speaker="白河",
            ),
        )
        assert proposed.ok, proposed.to_dict()
        proposal_id = str(proposed.result["proposal"]["id"])
        message = (
            "我赞成白河刚才的轮廓，就按白钟大陆来：西侧叫鸦羽山脉，"
            "中央是镜线内海，南岸放雾潮海岸和白花碑驿站，"
            "东南是潮鸢群岛。它就是普通的类地球大陆，不用异形世界。"
        )
        replacement = [
            {
                "operation": "create",
                "category": "continent_name",
                "name": "白钟大陆",
                "value": "白钟大陆",
                "visibility": "public",
            },
            *_replacement_map_operations(),
            {
                "operation": "create",
                "category": "world_shape",
                "name": "世界形态",
                "value": "普通的类地球大陆，非异形世界。",
                "visibility": "public",
            },
        ]
        client = ScriptedClient(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "confirm_session_zero_proposal",
                    "arguments": {"proposal_id": proposal_id},
                },
                {
                    "decision": "call_tools",
                    "calls": [
                        {
                            "tool_name": "confirm_session_zero_proposal",
                            "arguments": {
                                "proposal_id": proposal_id,
                                "replacement_world_operations": replacement,
                            },
                        }
                    ],
                },
                {
                    "decision": "final",
                    "message_kind": "gm_request",
                    "audience": "gm",
                    "reply": "这版世界轮廓就这样定下了。",
                },
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=service.gm_tool_registry,
            max_iterations=3,
        )
        tool_context = _real_session_zero_context(message, speaker="南星")
        state_summary = {
            "session_zero": service.gm_session_zero_tools.state_summary(
                tool_context
            )
        }

        outcome = agent.run(
            message,
            recent_context="",
            context=tool_context,
            state_summary=state_summary,
        )

        assert len(client.calls) == 3
        assert outcome.mode == "gm_agent_tool"
        assert outcome.reply == "这版世界轮廓就这样定下了。"
        assert [receipt.tool_name for receipt in outcome.receipts] == [
            "confirm_session_zero_proposal",
            *("create_world_setting" for _ in range(7)),
        ]
        assert [
            receipt.result.get("authority")
            for receipt in outcome.receipts[1:]
        ] == [
            *("table_consensus" for _ in range(6)),
            "player_confirmed",
        ]
        world = runtime.app.world_state.world_profile
        assert world.pending_proposals == []
        assert world.continent_name == "白钟大陆"
        assert world.world_shape == "普通的类地球大陆，非异形世界。"
        assert set(runtime.app.world_state.map_locations) == set(
            _REFINED_MAP_NAMES
        )


def test_resolved_revision_error_does_not_mask_later_semantic_blocker() -> None:
    class RejectingProposalVerifier:
        def verify_tool_proposal(self, **_kwargs) -> GMReplyGroundingReview:
            return GMReplyGroundingReview(
                valid=False,
                category="gm_must_repair",
                unsupported_claims=("当前修订中的地名没有公开依据",),
                correction_hint="只保留玩家逐字确认的地名。",
            )

    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("integrity-test")
        runtime.app.initialize_session_zero(participants=["白河", "南星"])
        proposal_id = _seed_legacy_map_proposal(service)
        message = (
            "我赞成地图提案，但细化为鸦羽山脉、镜线内海、"
            "雾潮海岸、白花碑驿站和潮鸢群岛。"
        )
        client = ScriptedClient(
            [
                {
                    "decision": "call_tool",
                    "tool_name": "confirm_session_zero_proposal",
                    "arguments": {"proposal_id": proposal_id},
                },
                {
                    "decision": "call_tool",
                    "tool_name": "confirm_session_zero_proposal",
                    "arguments": {
                        "proposal_id": proposal_id,
                        "replacement_world_operations": (
                            _replacement_map_operations()
                        ),
                    },
                },
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=service.gm_tool_registry,
            reply_grounding_verifier=RejectingProposalVerifier(),
            max_iterations=2,
        )
        tool_context = _real_session_zero_context(message, speaker="南星")

        outcome = agent.run(
            message,
            recent_context="",
            context=tool_context,
            state_summary={
                "session_zero": service.gm_session_zero_tools.state_summary(
                    tool_context
                )
            },
        )

        assert len(client.calls) == 2
        assert outcome.mode == "gm_agent_unresolved"
        assert "当前修订中的地名没有公开依据" in outcome.reply
        assert "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED" in outcome.error
        assert "SESSION_ZERO_PROPOSAL_REVISION_INCOMPLETE" not in outcome.error
        assert runtime.app.world_state.world_profile.pending_proposals
        assert runtime.app.world_state.map_locations == {}


def test_failed_replacement_followup_rolls_back_and_restores_pending_proposal() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("integrity-test")
        runtime.app.initialize_session_zero(participants=["澄砚", "白河"])
        existing_message = "我先记录镜线内海这个地点。"
        existing = service.gm_tool_registry.execute(
            "create_world_setting",
            {
                "category": "map_locations",
                "name": "镜线内海",
                "value": "早已存在的镜线内海。",
                "attributes": {
                    "feature_type": "inland_sea",
                    "terrain": "内海",
                    "position_hint": "center",
                },
                "visibility": "public",
                "authority": "player_confirmed",
                "reason": "玩家明确贡献。",
            },
            _real_session_zero_context(existing_message),
        )
        assert existing.ok, existing.to_dict()
        proposal_id = _seed_legacy_map_proposal(service)
        message = (
            "我赞成地图提案，但细化为鸦羽山脉、镜线内海、"
            "雾潮海岸、白花碑驿站和潮鸢群岛。"
        )
        client = ScriptedClient(
            [
                {
                    "decision": "call_tools",
                    "calls": [
                        {
                            "tool_name": "confirm_session_zero_proposal",
                            "arguments": {
                                "proposal_id": proposal_id,
                                "replacement_world_operations": (
                                    _replacement_map_operations()
                                ),
                            },
                        }
                    ],
                },
                {"decision": "final", "reply": "提案已经落实。"},
                {"decision": "final", "reply": "提案已经落实。"},
                {"decision": "final", "reply": "提案已经落实。"},
            ]
        )
        agent = LLMGMToolAgent(
            client,
            model="fake",
            registry=service.gm_tool_registry,
            max_iterations=2,
        )
        tool_context = _real_session_zero_context(message, speaker="白河")
        state_summary = {
            "session_zero": service.gm_session_zero_tools.state_summary(
                tool_context
            )
        }

        outcome = agent.run(
            message,
            recent_context="",
            context=tool_context,
            state_summary=state_summary,
        )

        world = runtime.app.world_state.world_profile
        assert outcome.mode == "gm_agent_message_transaction_rolled_back"
        assert outcome.reply == "刚才这件事没有处理完整，存档没有改动。麻烦再说一次。"
        assert "回滚" not in outcome.reply
        assert [item["id"] for item in world.pending_proposals] == [
            proposal_id
        ]
        assert set(runtime.app.world_state.map_locations) == {"镜线内海"}
        assert "鸦羽山脉" not in runtime.app.world_state.map_locations
        assert any(
            receipt.error_code == "WORLD_PROPOSAL_CREATE_TARGET_EXISTS"
            for receipt in outcome.receipts
        )
        assert all(
            not receipt.state_changed
            for receipt in outcome.receipts
        )


def test_model_owns_hero_confirmation_tool_choice_in_same_run() -> None:
    state: list[str] = []
    registry = GMToolRegistry(
        transaction_factory=lambda *_args: SnapshotTransaction(state)
    )
    registry.register(
        GMToolDefinition(
            name="update_hero_draft",
            description="更新角色草稿。",
            handler=lambda _context, _arguments: (
                state.append("updated")
                or GMToolReceipt.success(
                    "update_hero_draft",
                    result={"ready": True, "player_name": "澄砚"},
                    state_changed=True,
                )
            ),
            side_effect="write",
        )
    )
    registry.register(
        GMToolDefinition(
            name="confirm_hero_draft",
            description="确认已经完整的角色草稿。",
            handler=lambda _context, _arguments: (
                state.append("confirmed")
                or GMToolReceipt.success(
                    "confirm_hero_draft",
                    result={"player_name": "澄砚"},
                    state_changed=True,
                )
            ),
            side_effect="write",
        )
    )
    client = ScriptedClient(
        [
            {
                "decision": "call_tool",
                "tool_name": "update_hero_draft",
                "arguments": {},
            },
            {
                "decision": "call_tool",
                "tool_name": "confirm_hero_draft",
                "arguments": {},
            },
            {"decision": "final", "reply": "角色已经正式建卡。"},
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=registry)

    outcome = agent.run(
        "我确认角色并正式建卡。",
        recent_context="",
        context=session_zero_context(),
        state_summary={},
    )

    assert state == ["updated", "confirmed"]
    assert outcome.reply == "角色已经正式建卡。"
    followup = request_payload(client, 1)
    assert "protocol_error" not in followup["history"][-1]


def test_ordinary_adventure_multi_verb_message_has_no_completeness_gate() -> None:
    client = ScriptedClient(
        [
            {
                "decision": "final",
                "reply": "你先检查了门锁，随后靠在门边警戒。",
            }
        ]
    )
    agent = LLMGMToolAgent(client, model="fake", registry=GMToolRegistry())
    context = session_zero_context()
    context.gate_status = "adventure"

    outcome = agent.run(
        "我先调查门锁，然后守在门边。",
        recent_context="",
        context=context,
        state_summary={},
    )

    assert outcome.reply == "你先检查了门锁，随后靠在门边警戒。"
    assert len(client.calls) == 1
    assert not any("message_integrity" in item for item in outcome.trace)


def test_real_session_zero_handler_rejects_skill_option_as_base_attributes() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("角色完整性团")
        runtime.app.initialize_session_zero(participants=["澄砚"])
        runtime.app.world_state.world_profile.hero_drafts["澄砚"] = HeroDraft(
            player_name="澄砚",
            hero_name="苍祈",
            classes={"拟兽使": 5},
            attributes={"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6},
            skills={"拟兽系仪式": 1},
        )
        message = "拟兽系仪式的施法属性我选洞察+意志"
        context = GMToolExecutionContext(
            campaign_id="角色完整性团",
            session_id="s0",
            channel_id="group-1",
            speaker="澄砚",
            gate_status="session_zero",
            directly_addressed=True,
            metadata={"current_message": message},
        )
        before_attributes = json.dumps(
            runtime.app.world_state.world_profile.hero_drafts["澄砚"].attributes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        wrong = service.gm_tool_registry.execute(
            "update_hero_draft",
            {
                "subject": "苍祈",
                "patch": {"attributes": {"洞察": 10, "意志": 10}},
            },
            context,
        )

        after_wrong_attributes = json.dumps(
            runtime.app.world_state.world_profile.hero_drafts["澄砚"].attributes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert wrong.ok is False
        assert wrong.error_code == "HERO_SKILL_OPTION_MAPPED_TO_BASE_ATTRIBUTES"
        assert wrong.state_changed is False
        assert after_wrong_attributes == before_attributes

        correct = service.gm_tool_registry.execute(
            "update_hero_draft",
            {
                "subject": "苍祈",
                "patch": {
                    "skill_options": {"拟兽系仪式": ["洞察+意志"]},
                },
            },
            context,
        )

        draft = runtime.app.world_state.world_profile.hero_drafts["澄砚"]
        assert correct.ok is True, correct.to_dict()
        assert correct.state_changed is True
        assert draft.skill_options == {"拟兽系仪式": ["洞察+意志"]}
        assert draft.attributes == deepcopy(
            {"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6}
        )
