from __future__ import annotations

import re
import tempfile
from pathlib import Path

from fu_gm.components.campaign_state_transaction import (
    CampaignStateTransaction,
)
from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, EnemyRank, SceneType


CAMPAIGN_ID = "tool-contract-matrix"


def _context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=CAMPAIGN_ID,
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={"current_message": "检查工具合同。"},
    )


def _read_arguments() -> dict[str, dict[str, object]]:
    return {
        "discover_capabilities": {
            "domains": ["supervisor"],
            "reason": "测试按需能力目录。",
        },
        "inspect_supervisor_state": {},
        "list_saves": {},
        "inspect_campaign": {"campaign_id": CAMPAIGN_ID},
        "get_session_status": {},
        "get_hero_drafts": {"scope": "all"},
        "get_hero_state": {"scope": "all"},
        "get_world_state": {},
        "query_world_settings": {},
        "get_session_zero_contributions": {},
        "get_session_zero_readiness": {},
        "get_scene_state": {},
        "get_clocks": {},
        "get_npc_profiles": {"include_private": True},
        "get_npc_combatant_design": {"name": "尚未准备的合同NPC"},
        "get_gameplay_state": {},
        "recall_scene_memory": {"actor": "伊莉雅"},
        "get_world_map_status": {},
        "inspect_semantic_map": {},
        "get_runtime_state": {},
        "get_travel_state": {},
        "suggest_route_travel_days": {
            "origin": "起点",
            "destination": "终点",
            "travel_mode": "land",
        },
        "get_progression_state": {},
        "get_dungeon_state": {},
        "get_rule_reference": {"kind": "skill", "name": "碎骨"},
        "search_rule_references": {"kind": "skill", "text": "碎骨"},
        "list_background_tasks": {"include_completed": True},
        "get_background_task": {"task_id": "bg-not-found"},
    }


def test_every_read_tool_is_runtime_pure() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime(CAMPAIGN_ID)
        service._mark_current_campaign(CAMPAIGN_ID)
        arguments = _read_arguments()
        read_names = {
            name
            for name, definition in service.gm_tool_registry._tools.items()
            if definition.side_effect == "read"
        }
        assert read_names == set(arguments)

        for name in sorted(read_names):
            before = CampaignStateTransaction.capture(runtime.app, CAMPAIGN_ID)
            map_before = service.gm_map_tools.capture_transaction_state(
                CAMPAIGN_ID
            )
            current_before = service.current_campaign_id

            receipt = service.gm_tool_registry.execute(
                name,
                arguments[name],
                _context(),
            )

            after = CampaignStateTransaction.capture(runtime.app, CAMPAIGN_ID)
            map_after = service.gm_map_tools.capture_transaction_state(
                CAMPAIGN_ID
            )
            assert not receipt.state_changed, name
            assert after == before, name
            assert map_after == map_before, name
            assert service.current_campaign_id == current_before, name


def test_combined_agent_state_summary_is_pure_in_live_conflict() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime(CAMPAIGN_ID)
        app = runtime.app
        app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=45,
                max_mp=35,
                mp=35,
                traits=["pc"],
            )
        )
        app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                max_hp=70,
                hp=70,
                max_mp=40,
                mp=40,
                traits=["enemy", "construct"],
            )
        )
        app.conflict_manager.register_enemy(
            "财团机兵",
            EnemyRank.SOLDIER,
        )
        app.start_scene(
            "风铃廊伏击",
            SceneType.CONFLICT,
            location="风铃廊",
            participants=["伊莉雅", "财团机兵"],
        )
        app.conflict_manager.start_scene(
            "风铃廊伏击",
            ["财团机兵", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["财团机兵"],
        )
        app.scene_frame_manager.current_frame = None
        app.scene_frame_manager.history = []
        app._surfaced_topic_memory_paths = {"memory/already-used.md"}
        before = CampaignStateTransaction.capture(app, CAMPAIGN_ID)
        map_before = service.gm_map_tools.capture_transaction_state(CAMPAIGN_ID)

        state = service.gm_agent_message_coordinator.state_builder.build(
            _context()
        )

        after = CampaignStateTransaction.capture(app, CAMPAIGN_ID)
        map_after = service.gm_map_tools.capture_transaction_state(CAMPAIGN_ID)
        assert state["runtime"]["conflict"]["current_npc_tactical_snapshot"]
        assert after == before
        assert map_after == map_before
        assert app.scene_frame_manager.current_frame is None
        assert app._surfaced_topic_memory_paths == {"memory/already-used.md"}


def test_all_tool_failures_are_normalized_as_non_mutating() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        service._runtime(CAMPAIGN_ID)

        for name in service.gm_tool_registry._tools:
            receipt = service.gm_tool_registry.execute(
                name,
                [],
                _context(),
            )
            assert not receipt.ok, name
            assert not receipt.state_changed, name
            assert receipt.error_code == "INVALID_ARGUMENTS", name


def test_tool_side_effects_and_scopes_form_a_closed_contract() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        registry = service.gm_tool_registry
        registered = set(registry._tools)

    assert {
        definition.side_effect
        for definition in registry._tools.values()
    } <= {"read", "write_pending", "write", "replace_state"}
    assert registered == GMToolAgentCapabilityPolicy.managed_tool_names()

    player_scopes = set().union(
        *GMToolAgentCapabilityPolicy._GATE_SCOPES.values()
    )
    system_scopes = set().union(
        *GMToolAgentCapabilityPolicy._SYSTEM_BEAT_SCOPES.values()
    )
    assert system_scopes - player_scopes == set()
    assert GMToolAgentCapabilityPolicy._RESTRICTED_SYSTEM_TOOLS.isdisjoint(
        system_scopes | player_scopes
    )


def test_literal_followup_tool_names_are_registered() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "fu_gm"
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        registered = set(service.gm_tool_registry._tools)

    referenced: set[str] = set()
    pattern = re.compile(
        r'"(?:allowed_followup_tools|required_followup_tools)"\s*:\s*'
        r"\[(?P<body>[^\]]*)\]"
    )
    for path in root.glob("gm_*_tools.py"):
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            referenced.update(
                re.findall(r'["\']([a-z][a-z0-9_]*)["\']', match.group("body"))
            )

    assert referenced
    assert referenced - registered == set()
