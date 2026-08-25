from __future__ import annotations

import tempfile

from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_agent_message_coordinator import (
    GMToolStateSnapshotBuilder,
)
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.kariba_fixture import seed_kariba_ready_campaign


def _registry() -> GMToolRegistry:
    registry = GMToolRegistry()
    for name in (
        "get_scene_state",
        "get_gameplay_state",
        "start_scene",
        "focus_scene_branch",
        "transition_scene",
        "commit_scene_response",
        "move_scene_group",
        "abort_travel",
        "configure_boss_phases",
        "save_campaign",
        "decide_npc_response",
        "resolve_gm_opportunity",
        "run_current_npc_turn",
        "end_conflict",
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
        "get_gameplay_state",
        "start_scene",
        "focus_scene_branch",
        "transition_scene",
        "move_scene_group",
        "abort_travel",
        "configure_boss_phases",
        "save_campaign",
        "decide_npc_response",
        "resolve_gm_opportunity",
        "run_current_npc_turn",
        "end_conflict",
    }


def test_followup_only_tools_are_not_discoverable_on_an_ordinary_message() -> None:
    registry = _registry()
    for name in ("perform_check_action",):
        registry.register(
            GMToolDefinition(
                name=name,
                description=name,
                handler=lambda _context, _arguments, tool=name: GMToolReceipt.success(tool),
            )
        )
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
        metadata={"gm_dynamic_capabilities_enabled": True},
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(registry, context)
    }

    assert "perform_check_action" not in names
    assert "commit_scene_response" not in names


def test_required_followup_temporarily_exposes_scene_response() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
        metadata={
            "gm_dynamic_capabilities_enabled": True,
            "_gm_agent_required_followup_context": {
                "source_tool": "perform_scene_action",
                "required_tools": ["commit_scene_response"],
                "scene_response_followup": {
                    "public_reply": "机关检定已经完成，闸门停在半开的位置。",
                    "public_facts": ["闸门停在半开的位置。"],
                },
            },
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas_for_names(
            _registry(),
            context,
            {"commit_scene_response"},
        )
    }

    assert names == {"commit_scene_response"}


def test_active_scene_hides_start_scene_but_keeps_legal_transitions() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
        metadata={
            "_gm_runtime_scene_state_known": True,
            "_gm_scene_active": True,
            "_gm_conflict_active": False,
        },
    )

    names = GMToolAgentCapabilityPolicy.phase_tool_names(_registry(), context)

    assert "start_scene" not in names
    assert "transition_scene" in names
    assert "end_scene" in names


def test_conflict_hides_all_ordinary_scene_lifecycle_tools() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="玩家",
        gate_status="adventure",
        metadata={
            "_gm_runtime_scene_state_known": True,
            "_gm_scene_active": True,
            "_gm_conflict_active": True,
        },
    )

    names = GMToolAgentCapabilityPolicy.phase_tool_names(_registry(), context)

    assert not (names & GMToolAgentCapabilityPolicy._SCENE_LIFECYCLE_TOOLS)


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


def test_chapter_one_opening_tool_switches_with_flow_mode() -> None:
    registry = GMToolRegistry()
    for name in ("start_session", "start_adventure"):
        registry.register(
            GMToolDefinition(
                name=name,
                description=name,
                handler=lambda _context, _arguments, tool=name: (
                    GMToolReceipt.success(tool)
                ),
            )
        )

    def available(*, flow_mode: str, invited_ready: bool) -> set[str]:
        context = GMToolExecutionContext(
            campaign_id="c",
            session_id="s",
            channel_id="group",
            speaker="玩家",
            gate_status="session_zero",
            metadata={
                "adventure_opening_flow_mode": flow_mode,
                "_gm_chapter_one_invited_ready": invited_ready,
            },
        )
        return {
            item["name"]
            for item in GMToolAgentCapabilityPolicy.schemas(registry, context)
        }

    assert available(flow_mode="legacy", invited_ready=True) == {
        "start_session"
    }
    assert available(flow_mode="optimized", invited_ready=True) == {
        "start_adventure"
    }
    assert available(flow_mode="optimized", invited_ready=False) == set()


def test_optimized_invited_opening_remains_reachable_when_hot_capabilities_are_disabled() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(
            data_root=root,
            use_llm=False,
            adventure_opening_flow_mode="optimized",
        )
        seed_kariba_ready_campaign(
            service,
            campaign_id="hot-disabled-opening",
            session_id="s1",
            channel_id="group-1",
        )
        context = GMToolExecutionContext(
            campaign_id="hot-disabled-opening",
            session_id="s1",
            channel_id="group-1",
            speaker="测试玩家甲",
            gate_status="session_zero",
            metadata={
                "adventure_opening_flow_mode": "optimized",
                "gm_dynamic_capabilities_enabled": True,
                "gm_hot_session_zero_capabilities_enabled": False,
            },
        )

        GMToolStateSnapshotBuilder(service).build(context)
        names = {
            item["name"]
            for item in GMToolAgentCapabilityPolicy.schemas(
                service.gm_tool_registry,
                context,
            )
        }

        assert context.metadata["_gm_chapter_one_invited_ready"] is True
        anchor = context.metadata["conversation_anchor"]
        assert anchor["kind"] == "chapter_one_invitation"
        assert anchor["status"] == "awaiting_semantic_reply"
        assert anchor["blocking"] is False
        assert anchor["player_visible"] is False
        assert "start_adventure" in names
        assert "start_session" not in names
        assert "gm_hot_session_zero_tool_names" not in context.metadata


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


def test_blank_phase_exposes_map_reads_but_not_map_mutations() -> None:
    registry = _registry()
    map_reads = {"get_world_map_status", "inspect_semantic_map"}
    map_mutations = {
        "find_map_location_candidates",
        "place_world_map_locations",
        "generate_world_map_preview",
        "edit_world_map",
    }
    for name in sorted(map_reads | map_mutations):
        registry.register(
            GMToolDefinition(
                name=name,
                description=name,
                handler=lambda _context, _arguments, tool=name: GMToolReceipt.success(tool),
            )
        )

    def available(gate_status: str) -> set[str]:
        context = GMToolExecutionContext(
            campaign_id="c",
            session_id="s",
            channel_id="private",
            speaker="玩家",
            gate_status=gate_status,
        )
        return {
            item["name"]
            for item in GMToolAgentCapabilityPolicy.schemas(registry, context)
        }

    for gate_status in ("inactive", "pre_session", "paused"):
        names = available(gate_status)
        assert map_reads <= names
        assert not (map_mutations & names)

    for gate_status in ("session_zero", "adventure"):
        names = available(gate_status)
        assert map_reads | map_mutations <= names


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
    assert names == {"get_scene_state", "get_gameplay_state", "start_scene"}


def test_free_scene_beat_without_due_authority_is_read_only() -> None:
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
    assert names == {"get_scene_state", "get_gameplay_state"}


def test_due_storm_result_temporarily_exposes_exact_scene_delivery() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "free_scene_beat",
            "scene_change_authorities": [
                {
                    "event_id": "storm-front-7",
                    "source_kind": "scheduled_event",
                    "status": "due",
                    "public_reply": "暴雨前锋抵达山口，石桥表面已经覆上一层急流。",
                    "public_facts": ["石桥表面已经覆上一层急流。"],
                }
            ],
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert "commit_scene_response" in names


def test_adventure_table_nudge_exposes_no_tools() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "adventure_table_nudge",
        },
    )

    assert GMToolAgentCapabilityPolicy.phase_tool_names(
        _registry(), context
    ) == set()
    assert GMToolAgentCapabilityPolicy.schemas(_registry(), context) == []


def test_session_zero_nudge_cannot_lock_and_replay_readiness_board() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        service = FUGMHttpService(data_root=tmpdir, use_llm=False)
        context = GMToolExecutionContext(
            campaign_id="c",
            session_id="s0",
            channel_id="group",
            speaker="系统主动节拍",
            gate_status="session_zero",
            metadata={
                "system_gm_beat_request": True,
                "heartbeat_action": "session_zero_nudge",
            },
        )
        nudge_names = GMToolAgentCapabilityPolicy.phase_tool_names(
            service.gm_tool_registry,
            context,
        )
        player_context = GMToolExecutionContext(
            campaign_id="c",
            session_id="s0",
            channel_id="group",
            speaker="玩家",
            gate_status="session_zero",
        )
        player_names = GMToolAgentCapabilityPolicy.phase_tool_names(
            service.gm_tool_registry,
            player_context,
        )

    assert "get_session_zero_readiness" not in nudge_names
    assert "get_session_status" not in nudge_names
    assert "get_hero_drafts" not in nudge_names
    assert nudge_names <= {"set_chapter_one_transition"}
    assert "get_session_zero_readiness" in player_names


def test_defeat_aftermath_has_only_scene_recovery_capabilities() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "defeat_aftermath",
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {
        "get_scene_state",
        "get_gameplay_state",
        "start_scene",
        "focus_scene_branch",
        "transition_scene",
    }


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
        "get_gameplay_state",
        "run_current_npc_turn",
        "resolve_gm_opportunity",
    }


def test_gm_opportunity_beat_can_only_read_state_and_resolve_window() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "gm_opportunity",
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {
        "get_scene_state",
        "get_gameplay_state",
        "resolve_gm_opportunity",
    }


def test_conflict_resolution_beat_can_only_read_state_and_end_conflict() -> None:
    context = GMToolExecutionContext(
        campaign_id="c",
        session_id="s",
        channel_id="group",
        speaker="系统主动节拍",
        gate_status="adventure",
        metadata={
            "system_gm_beat_request": True,
            "heartbeat_action": "conflict_resolution",
        },
    )

    names = {
        item["name"]
        for item in GMToolAgentCapabilityPolicy.schemas(_registry(), context)
    }

    assert names == {"get_scene_state", "get_gameplay_state", "end_conflict"}


def test_every_registered_tool_has_at_least_one_trusted_capability_scope() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        registered = set(service.gm_tool_suite.registry._tools)

    exposed = set().union(
        *GMToolAgentCapabilityPolicy._GATE_SCOPES.values(),
        *GMToolAgentCapabilityPolicy._SYSTEM_BEAT_SCOPES.values(),
        GMToolAgentCapabilityPolicy._FOLLOWUP_ONLY_TOOLS,
        GMToolAgentCapabilityPolicy._RESTRICTED_SYSTEM_TOOLS,
    )

    assert registered - exposed == set()
    assert GMToolAgentCapabilityPolicy.managed_tool_names() == exposed
