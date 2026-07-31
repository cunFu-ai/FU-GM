from __future__ import annotations

import tempfile

from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.http_server import FUGMHttpService


def _registry() -> GMToolRegistry:
    registry = GMToolRegistry()
    for name in (
        "get_scene_state",
        "start_scene",
        "transition_scene",
        "move_scene_group",
        "abort_travel",
        "configure_boss_phases",
        "save_campaign",
        "decide_npc_response",
        "resolve_gm_opportunity",
        "run_current_npc_turn",
        "decide_npc_action",
        "decide_collective_action",
    ):
        registry.register(
            GMToolDefinition(
                name=name,
                description=name,
                handler=lambda _context, _arguments, tool=name: GMToolReceipt.success(tool),
            )
        )
    return registry


def test_adventure_message_receives_adventure_and_management_catalog() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
    )
    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }
    assert names == {
        "get_scene_state",
        "start_scene",
        "transition_scene",
        "move_scene_group",
        "abort_travel",
        "configure_boss_phases",
        "save_campaign",
        "decide_npc_response",
        "resolve_gm_opportunity",
        "run_current_npc_turn",
    }


def test_semantic_plan_can_only_narrow_trusted_phase_scope() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="session_zero",
        metadata={
            "gm_capability_tool_names": ["save_campaign", "start_scene"],
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {"save_campaign"}


def test_receipt_authorized_followup_can_expand_beyond_initial_plan() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
        metadata={"gm_capability_tool_names": ["save_campaign"]},
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas_for_names(
            _registry(),
            context,
            {"start_scene"},
        )
    }

    assert names == {"start_scene"}


def test_session_zero_scope_excludes_adventure_scene_tools() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="session_zero",
    )
    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }
    assert names == {"save_campaign"}


def test_unknown_phase_fails_closed_to_common_tools() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="future_or_corrupt_phase",
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {"save_campaign"}


def test_scene_opening_uses_trusted_scope_not_message_words() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "scene_opening",
            "current_message": "存档，然后开场",
        },
    )
    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }
    assert names == {
        "get_scene_state",
        "start_scene",
        "transition_scene",
        "configure_boss_phases",
        "decide_npc_action",
        "decide_collective_action",
        "resolve_gm_opportunity",
    }


def test_free_scene_beat_exposes_action_tools_but_not_player_response_tool() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "free_scene_beat",
        },
    )
    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }
    assert "decide_npc_action" in names
    assert "decide_collective_action" in names
    assert "configure_boss_phases" in names
    assert "decide_npc_response" not in names


def test_npc_turn_can_finish_a_gm_owned_fumble_opportunity() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "npc_turn",
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {
        "get_scene_state",
        "run_current_npc_turn",
        "resolve_gm_opportunity",
    }


def test_every_registered_tool_has_at_least_one_trusted_capability_scope() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        registered = set(service.gm_tool_suite.registry._tools)

    exposed = set().union(
        *GMToolAgentCapabilityPolicy._GATE_SCOPES.values(),
        *GMToolAgentCapabilityPolicy._SYSTEM_BEAT_SCOPES.values(),
    )

    assert registered - exposed == set()
    assert GMToolAgentCapabilityPolicy.managed_tool_names() == exposed
