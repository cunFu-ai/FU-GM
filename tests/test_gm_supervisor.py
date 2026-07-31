from __future__ import annotations

import json
import tempfile

from fu_gm.components.gm_supervisor import (
    GMCapabilityBroker,
    GMSupervisorMonitor,
)
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Clock


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

    def create_chat_completion(self, **_kwargs: object) -> str:
        if not self.responses:
            raise AssertionError("监督层测试缺少模型响应。")
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def _context(*, gate: str = "adventure") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="supervisor-test",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status=gate,
        directly_addressed=True,
        metadata={
            "current_message": "@时悠 存档",
            "gm_dynamic_capabilities_enabled": True,
        },
    )


def test_dynamic_catalog_starts_small_and_expands_only_selected_domain() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=service.gm_tool_registry,
        )
        context = _context()

        initial = {
            item["name"] for item in agent._available_tool_schemas(context)
        }

        assert initial == {
            "discover_capabilities",
            "inspect_supervisor_state",
        }
        receipt = service.gm_supervisor_tools.discover_capabilities(
            context,
            {
                "domains": ["campaign"],
                "reason": "玩家正在要求保存当前战役。",
            },
        )
        assert receipt.ok

        expanded = {
            item["name"] for item in agent._available_tool_schemas(context)
        }
        assert "save_campaign" in expanded
        assert "load_campaign" in expanded
        assert "start_scene" not in expanded
        assert "perform_check_action" not in expanded
        assert len(expanded) < 12


def test_capability_catalog_stays_semantic_until_domain_is_discovered() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()

        state = service.gm_agent_message_coordinator.state_builder.build(
            context
        )
        catalog = state["supervisor"]["capability_catalog"]

    assert catalog
    assert all("tool_names" not in item for item in catalog)
    assert all(int(item["available_tool_count"]) > 0 for item in catalog)
    assert {item["domain"] for item in catalog} >= {
        "campaign",
        "scene",
        "npc",
        "rules",
        "supervisor",
    }


def test_capability_discovery_accepts_one_domain_singular_alias() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()

        receipt = service.gm_tool_registry.execute(
            "discover_capabilities",
            {
                "domain": "campaign",
                "reason": "只需要处理一次存档。",
            },
            context,
        )

    assert receipt.ok
    assert receipt.result["domains"] == ["campaign"]
    assert "save_campaign" in receipt.result["granted_tool_names"]


def test_system_beat_keeps_its_existing_narrow_trusted_surface() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=service.gm_tool_registry,
        )
        context = _context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "npc_turn",
            }
        )

        names = {
            item["name"] for item in agent._available_tool_schemas(context)
        }

        assert names == {
            "get_scene_state",
            "get_npc_profiles",
            "get_gameplay_state",
            "run_current_npc_turn",
            "resolve_gm_opportunity",
        }


def test_supervisor_detects_terminal_clock_and_conflict_actor_corruption() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    state = {
        "scene": {"active": True, "scene_id": "scene-1"},
        "runtime": {
            "conflict": {
                "active": True,
                "current_actor": "",
                "turn_order": ["伊莉雅", "财团机兵"],
            }
        },
        "gameplay": {"pending_decisions": []},
        "clocks": {
            "active": [
                {
                    "name": "巡逻队逼近",
                    "current": 6,
                    "max_segments": 6,
                    "clock_type": "threat",
                }
            ]
        },
    }

    alerts = monitor.scan(context, state)
    codes = {str(item["code"]) for item in alerts}

    assert "CONFLICT_WITHOUT_ACTOR" in codes
    assert "FULFILLED_CLOCK_STILL_ACTIVE" in codes


def test_repeated_write_failure_opens_bounded_circuit() -> None:
    monitor = GMSupervisorMonitor(failure_threshold=3, circuit_seconds=60)
    context = _context()
    failure = GMToolReceipt.failure(
        "change_clock",
        "CLOCK_NOT_FOUND",
        "命刻不存在。",
        "先读取命刻。",
    )

    monitor.observe_receipts(context, [failure])
    monitor.observe_receipts(context, [failure])
    observation = monitor.observe_receipts(context, [failure])

    assert observation["open_circuits"][0]["tool_name"] == "change_clock"
    admission = monitor.admission_error(
        GMToolDefinition(
            name="change_clock",
            description="test",
            handler=lambda _context, _arguments: GMToolReceipt.success(
                "change_clock"
            ),
            side_effect="write",
        ),
        context,
    )
    assert admission is not None
    assert admission.error_code == "SUPERVISOR_CIRCUIT_OPEN"
    assert admission.retryable is False


def test_model_snapshot_is_bounded_and_keeps_supervisor_catalog() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()
        state = service.gm_agent_message_coordinator.state_builder.build(
            context
        )
        encoded = json.dumps(state, ensure_ascii=False)

        assert "supervisor" in state
        assert state["supervisor"]["capability_catalog"]
        assert len(encoded) < 20000
        assert "runtime" in state
        assert "scene" in state


def test_ordinary_adventure_snapshot_starts_with_compact_control_plane() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()

        state = service.gm_agent_message_coordinator.state_builder.build(
            context
        )

    assert {"scene", "runtime", "processes"} <= set(state)
    assert "npcs" not in state
    assert "gameplay" not in state
    assert "clocks" not in state
    assert "adventure" not in state
    assert "dungeon" not in state
    assert "hero_drafts" not in state
    assert "campaigns" not in state
    assert "recent_scene_history" not in state["scene"]
    assert "world_public_facts" not in state["scene"]
    assert "story_items" not in state["scene"]


def test_discovered_domain_expands_next_snapshot_with_relevant_state() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()

        npc_receipt = service.gm_supervisor_tools.discover_capabilities(
            context,
            {
                "domain": "npc",
                "reason": "玩家正在直接询问当前场景中的NPC。",
            },
        )
        npc_state = (
            service.gm_agent_message_coordinator.state_builder.build(context)
        )

        rules_receipt = service.gm_supervisor_tools.discover_capabilities(
            context,
            {
                "domain": "rules",
                "reason": "需要裁定一次属性检定与命刻互动。",
            },
        )
        expanded_state = (
            service.gm_agent_message_coordinator.state_builder.build(context)
        )

    assert npc_receipt.ok
    assert "npcs" in npc_state
    assert "gameplay" not in npc_state
    assert rules_receipt.ok
    assert {"npcs", "gameplay", "clocks"} <= set(expanded_state)


def test_campaign_and_hero_indexes_expand_only_for_relevant_domains() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        campaign_context = _context()
        hero_context = _context()

        service.gm_supervisor_tools.discover_capabilities(
            campaign_context,
            {
                "domain": "campaign",
                "reason": "玩家要查看存档。",
            },
        )
        campaign_state = (
            service.gm_agent_message_coordinator.state_builder.build(
                campaign_context
            )
        )
        service.gm_supervisor_tools.discover_capabilities(
            hero_context,
            {
                "domain": "session_zero",
                "reason": "玩家要查看自己的角色草稿。",
            },
        )
        hero_state = (
            service.gm_agent_message_coordinator.state_builder.build(
                hero_context
            )
        )

    assert "campaigns" in campaign_state
    assert "hero_drafts" not in campaign_state
    assert "hero_drafts" in hero_state
    assert "campaigns" not in hero_state


def test_scene_domain_expands_compact_scene_to_full_authoritative_view() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()

        service.gm_supervisor_tools.discover_capabilities(
            context,
            {
                "domain": "scene",
                "reason": "需要处理故事物品与跨场景移动。",
            },
        )
        state = service.gm_agent_message_coordinator.state_builder.build(
            context
        )

    assert "recent_scene_history" in state["scene"]
    assert "world_public_facts" in state["scene"]
    assert "story_items" in state["scene"]


def test_system_gm_beat_keeps_only_its_relevant_detailed_domains() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "npc_turn",
            }
        )

        state = service.gm_agent_message_coordinator.state_builder.build(
            context
        )

    assert {
        "scene",
        "runtime",
        "processes",
        "npcs",
        "gameplay",
        "clocks",
    } <= set(state)
    assert "adventure" not in state
    assert "references" not in state
    assert "dungeon" not in state


def test_every_registered_tool_belongs_to_a_capability_domain_or_discovery() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        registered = set(service.gm_tool_registry._tools)

    catalogued = GMCapabilityBroker.all_catalogued_tools() | {
        GMCapabilityBroker.DISCOVERY_TOOL
    }
    assert registered == catalogued


def test_snapshot_alert_resolves_after_authoritative_state_recovers() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    broken = {
        "scene": {"active": True, "scene_id": "scene-1"},
        "runtime": {"conflict": {"active": False}},
        "gameplay": {"pending_decisions": []},
        "clocks": {
            "active": [
                {
                    "name": "潮门关闭",
                    "current": 4,
                    "max_segments": 4,
                    "clock_type": "threat",
                }
            ]
        },
    }

    assert monitor.scan(context, broken)
    healthy = {
        **broken,
        "clocks": {"active": []},
    }

    assert monitor.scan(context, healthy) == []
    recent = monitor.audit_payload(context.campaign_id)["recent_alerts"]
    assert recent[0]["status"] == "resolved"


def test_reopened_alert_is_not_duplicated_in_active_or_recent_lists() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    broken = {
        "scene": {"active": True, "scene_id": "scene-1"},
        "runtime": {"conflict": {"active": False}},
        "gameplay": {"pending_decisions": []},
        "clocks": {
            "active": [
                {
                    "name": "潮门关闭",
                    "current": 4,
                    "max_segments": 4,
                    "clock_type": "threat",
                }
            ]
        },
    }

    monitor.scan(context, broken)
    monitor.scan(context, {**broken, "clocks": {"active": []}})
    reopened = monitor.scan(context, broken)
    payload = monitor.audit_payload(context.campaign_id)

    assert [
        item["code"]
        for item in reopened
        if item["code"] == "FULFILLED_CLOCK_STILL_ACTIVE"
    ] == ["FULFILLED_CLOCK_STILL_ACTIVE"]
    assert [
        item["code"]
        for item in payload["recent_alerts"]
        if item["code"] == "FULFILLED_CLOCK_STILL_ACTIVE"
    ] == ["FULFILLED_CLOCK_STILL_ACTIVE"]


def test_supervisor_detects_cross_component_lifecycle_invariants() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    state = {
        "scene": {
            "active": False,
            "frame_active": True,
            "scene_id": "legacy-frame",
            "frame_source_scene_id": "legacy-frame",
        },
        "runtime": {"conflict": {"active": False}},
        "gameplay": {"pending_decisions": []},
        "clocks": {"active": [], "pacing_budget": {}},
        "processes": {
            "session": {"ledger_active": False},
            "scene": {
                "authoritative_active": False,
                "frame_active": True,
                "frame_scene_id": "legacy-frame",
                "suspended_scene_ids": [],
            },
            "decisions": {
                "pending": [
                    {
                        "window_id": "w1",
                        "kind": "zero_hp",
                        "blocking": True,
                        "allowed_responders": [],
                        "scope_kind": "scene",
                        "scope_id": "expired-scene",
                    }
                ]
            },
            "clocks": {
                "foreground_pressure_names": ["A", "B"],
                "auto_advance_names": ["A", "B"],
                "scene_scoped": [
                    {
                        "name": "旧场景危机",
                        "scene_id": "expired-scene",
                    }
                ],
                "pacing_budget": {
                    "max_foreground_pressure_clocks": 1,
                    "max_auto_advance_clocks": 1,
                },
            },
            "travel": {
                "active": False,
                "pending_event": True,
            },
            "dungeon": {
                "active": True,
                "name": "沉没塔",
            },
            "rituals": [
                {
                    "clock_name": "仪式：风门",
                    "clock_exists": False,
                    "scene_id": "expired-scene",
                }
            ],
        },
    }

    codes = {
        str(item["code"])
        for item in monitor.scan(context, state)
    }

    assert {
        "ADVENTURE_WITHOUT_SCENE",
        "SCENE_FRAME_WITHOUT_SCENE",
        "BLOCKING_DECISION_WITHOUT_RESPONDER",
        "STALE_SCENE_DECISION_WINDOW",
        "SCENE_CLOCK_OUTSIDE_LIFECYCLE",
        "CLOCK_PRESSURE_BUDGET_EXCEEDED",
        "AUTO_CLOCK_BUDGET_EXCEEDED",
        "ACTIVE_DUNGEON_WITHOUT_SCENE",
        "TRAVEL_EVENT_WITHOUT_JOURNEY",
        "RITUAL_CLOCK_MISSING",
        "RITUAL_SCENE_MISMATCH",
        "ADVENTURE_SESSION_LEDGER_INACTIVE",
    } <= codes


def test_supervisor_detects_hidden_post_chapter_state_corruption() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    state = {
        "scene": {
            "active": True,
            "scene_id": "scene-1",
        },
        "runtime": {"conflict": {"active": False}},
        "gameplay": {"pending_decisions": []},
        "clocks": {"active": [], "pacing_budget": {}},
        "processes": {
            "session": {
                "ledger_active": True,
                "ledger_session_id": "different-session",
            },
            "scene": {
                "authoritative_active": True,
                "scene_id": "scene-1",
                "scene_type": "dungeon",
                "frame_active": True,
                "frame_scene_id": "scene-1",
                "suspended_scene_ids": [],
                "action_round": {
                    "round_number": 2,
                    "required": ["伊莉雅", "伊莉雅"],
                    "acted": ["苍祈"],
                    "waiting": ["伊莉雅"],
                    "auto_advance_skip_names": [],
                },
            },
            "decisions": {"pending": []},
            "clocks": {
                "foreground_pressure_names": [],
                "auto_advance_names": [],
                "scene_scoped": [],
                "pacing_budget": {},
            },
            "travel": {
                "active": True,
                "status": "event_pending",
                "current_day": 2,
                "total_days": 3,
                "resolved_day_numbers": [1],
                "pending_event": False,
                "pending_event_day": 2,
            },
            "dungeon": {
                "active": True,
                "current_area": "不存在的区域",
                "area_names": ["入口", "核心"],
                "missing_danger_clock_names": ["遗迹坍塌"],
            },
            "rituals": [
                {
                    "clock_name": "仪式：风门",
                    "clock_exists": True,
                    "clock_current": 1,
                    "clock_max_segments": 4,
                    "clock_status": "ready",
                    "ready_turn_serial": 3,
                    "ready": False,
                    "caster_exists": False,
                    "scene_id": "scene-1",
                }
            ],
            "projects": [
                {
                    "name": "断潮桥",
                    "current_progress": 2,
                    "required_progress": 4,
                    "completed": True,
                    "persisted": True,
                    "created_asset_id": "facility:断潮桥",
                },
                {
                    "name": "灵魂罗盘",
                    "current_progress": 3,
                    "required_progress": 3,
                    "completed": True,
                    "persisted": False,
                    "created_asset_id": "",
                },
            ],
        },
    }

    codes = {
        str(item["code"])
        for item in monitor.scan(context, state)
    }

    assert {
        "ACTION_ROUND_STATE_CORRUPT",
        "TRAVEL_PROGRESS_STATE_CORRUPT",
        "TRAVEL_PENDING_EVENT_MISSING",
        "DUNGEON_AREA_STATE_CORRUPT",
        "DUNGEON_DANGER_CLOCK_MISSING",
        "RITUAL_CASTER_MISSING",
        "RITUAL_READY_STATE_MISMATCH",
        "PROJECT_PROGRESS_STATE_CORRUPT",
        "PROJECT_COMPLETION_NOT_PERSISTED",
        "SESSION_LEDGER_ID_MISMATCH",
    } <= codes


def test_supervisor_detects_conflict_resume_and_held_action_corruption() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    state = {
        "scene": {"active": True, "scene_id": "scene-1"},
        "runtime": {
            "conflict": {
                "active": True,
                "current_actor": "伊莉雅",
                "turn_order": ["伊莉雅", "机兵"],
            }
        },
        "gameplay": {"pending_decisions": []},
        "processes": {
            "scene": {
                "authoritative_active": True,
                "scene_id": "scene-1",
                "suspended_scene_ids": [],
                "action_round": {},
            },
            "conflict": {
                "active": True,
                "current_actor": "伊莉雅",
                "turn_order": ["伊莉雅", "机兵"],
                "current_turn_index": 7,
                "turn_started_actor": "伊莉雅",
                "pending_turn_end_actor": "机兵",
                "current_bonus_actor": "",
                "queued_turns": ["机兵"],
                "queued_turn_kinds": [],
                "turn_serial": 8,
                "held_actions": [
                    {
                        "actor": "不在场角色",
                        "action_type": "Guard",
                    }
                ],
            },
            "decisions": {
                "pending": [
                    {
                        "kind": "critical_opportunity",
                        "owner": "伊莉雅",
                        "blocking": True,
                        "allowed_responders": ["伊莉雅"],
                        "deferred_turn_serial": 4,
                    }
                ]
            },
            "clocks": {"scene_scoped": [], "pacing_budget": {}},
            "travel": {"active": False},
            "dungeon": {"active": False},
            "rituals": [],
            "projects": [],
            "session": {
                "ledger_active": True,
                "ledger_session_id": "s1",
            },
        },
    }

    codes = {
        str(item["code"])
        for item in monitor.scan(context, state)
    }

    assert {
        "CONFLICT_TURN_STATE_CORRUPT",
        "TURN_END_WINDOW_MISMATCH",
        "HELD_ACTION_STATE_MISMATCH",
        "BLOCKING_DECISION_RESUME_MISMATCH",
    } <= codes


def test_supervisor_accepts_dungeon_nested_in_travel_discovery() -> None:
    monitor = GMSupervisorMonitor()
    context = _context()
    state = {
        "scene": {"active": True, "scene_id": "scene-dungeon"},
        "runtime": {"conflict": {"active": False}},
        "gameplay": {"pending_decisions": []},
        "processes": {
            "scene": {
                "authoritative_active": True,
                "scene_id": "scene-dungeon",
                "scene_type": "dungeon",
                "suspended_scene_ids": [],
                "action_round": {},
            },
            "conflict": {"active": False},
            "decisions": {"pending": []},
            "clocks": {"scene_scoped": [], "pacing_budget": {}},
            "travel": {
                "active": True,
                "status": "event_pending",
                "current_day": 1,
                "total_days": 2,
                "resolved_day_numbers": [1],
                "pending_event": True,
                "pending_event_day": 1,
                "pending_event_type": "discovery",
                "pending_event_tags": ["dungeon"],
                "suspended_by_dungeon": True,
            },
            "dungeon": {
                "active": True,
                "current_area": "入口",
                "area_names": ["入口"],
                "missing_danger_clock_names": [],
            },
            "rituals": [],
            "projects": [],
            "session": {
                "ledger_active": True,
                "ledger_session_id": "s1",
            },
        },
    }

    codes = {
        str(item["code"])
        for item in monitor.scan(context, state)
    }

    assert "DUNGEON_TRAVEL_NESTING_INVALID" not in codes


def test_full_snapshot_exposes_compact_process_control_plane() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context(gate="inactive")
        state = service.gm_agent_message_coordinator.state_builder.build_full(
            context
        )

    assert "dungeon" in state
    assert set(state["processes"]) == {
        "session",
        "scene",
        "conflict",
        "decisions",
        "npc_interactions",
        "clocks",
        "travel",
        "dungeon",
        "rituals",
        "projects",
        "map",
        "progression",
        "attention",
    }
    assert state["processes"]["scene"]["authoritative_active"] is False
    assert state["processes"]["dungeon"]["active"] is False
    assert state["processes"]["scene"]["action_round"] == {
        "round_number": 1,
        "required": [],
        "acted": [],
        "waiting": [],
        "auto_advance_skip_names": [],
    }


def test_process_control_plane_surfaces_obligations_without_resolving_them() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("supervisor-test")
        context = _context()
        window = runtime.app.decision_window_manager.create(
            kind="zero_hp",
            owner="伊莉雅",
            prompt="选择牺牲或放弃抵抗。",
            blocking=True,
            allowed_responders=["伊莉雅"],
        )

        state = (
            service.gm_agent_message_coordinator.state_builder.build_full(
                context
            )
        )

    assert state["processes"]["decisions"]["blocking_count"] == 1
    assert {
        "kind": "blocking_decision",
        "priority": "required",
        "count": 1,
    } in state["processes"]["attention"]
    assert runtime.app.decision_window_manager.get(
        window.window_id
    ).status.value == "pending"


def test_supervisor_inspection_returns_live_process_control_plane() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context(gate="inactive")

        receipt = service.gm_supervisor_tools.inspect_supervisor_state(
            context,
            {},
        )

    assert receipt.ok
    assert "processes" in receipt.result
    assert receipt.result["processes"]["session"]["gate_status"] == "inactive"
    assert receipt.result["processes"]["conflict"]["active"] is False
    assert receipt.public_fallback_reply == (
        "监督检查完成：当前没有活动告警，熔断器均未开启。"
    )
    assert receipt.lock_public_reply is True
    assert receipt.result["terminal_public_result"] is True


def test_supervisor_reconcile_archives_terminal_clock_without_touching_story() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("supervisor-test")
        runtime.app.clock_manager.add(
            Clock(
                name="巡逻队包围",
                max_segments=6,
                current=6,
                clock_type="threat",
                completion_consequence="巡逻队已经包围现场。",
            )
        )
        context = _context()
        full_state = (
            service.gm_agent_message_coordinator.state_builder.build_full(
                context
            )
        )
        service.gm_supervisor.scan(context, full_state)
        alert = service.gm_supervisor.autonomous_repair_alerts(
            context.campaign_id
        )[0]

        receipt = service.gm_supervisor_tools.reconcile_supervisor_state(
            context,
            {
                "alert_ids": [alert["alert_id"]],
                "reason": "协调旧存档遗留的完整命刻。",
            },
        )

        assert receipt.ok
        assert receipt.state_changed
        assert not runtime.app.clock_manager.exists("巡逻队包围")
        assert (
            service.gm_supervisor.autonomous_repair_alerts(
                context.campaign_id
            )
            == []
        )


def test_supervisor_recovery_scope_cannot_control_players_or_conflict() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "supervisor_recovery",
            }
        )
        agent = LLMGMToolAgent(
            _UnusedClient(),
            model="fake",
            registry=service.gm_tool_registry,
        )

        names = {
            item["name"] for item in agent._available_tool_schemas(context)
        }

        assert names == {
            "inspect_supervisor_state",
            "reconcile_supervisor_state",
            "get_runtime_state",
            "get_scene_state",
            "get_clocks",
            "get_gameplay_state",
        }
        assert "resolve_rule_window" not in names
        assert "run_current_npc_turn" not in names
        assert "end_conflict" not in names


def test_supervisor_recovery_inspection_is_not_terminal_public_result() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        context = _context()
        context.metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": "supervisor_recovery",
            }
        )

        receipt = service.gm_supervisor_tools.inspect_supervisor_state(
            context,
            {},
        )

    assert receipt.ok
    assert receipt.public_fallback_reply == ""
    assert receipt.lock_public_reply is False
    assert "terminal_public_result" not in receipt.result


def test_idle_heartbeat_repairs_safe_alert_silently() -> None:
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime("supervisor-test")
        runtime.app.clock_manager.add(
            Clock(
                name="潮水没顶",
                max_segments=4,
                current=4,
                clock_type="threat",
                completion_consequence="潮水已经封死退路。",
            )
        )
        service.session_gates.activate(
            "supervisor-test",
            "group-1",
            "s1",
            status="adventure",
        )
        alert_id = (
            "supervisor-test:FULFILLED_CLOCK_STILL_ACTIVE"
        )
        service.gm_tool_agent = LLMGMToolAgent(
            _ScriptedClient(
                [
                    {
                        "decision": "call_tool",
                        "tool_name": "inspect_supervisor_state",
                        "arguments": {},
                        "reason": "先确认告警仍然存在。",
                    },
                    {
                        "decision": "call_tool",
                        "tool_name": "reconcile_supervisor_state",
                        "arguments": {
                            "alert_ids": [alert_id],
                            "reason": "归档已经兑现的残留命刻。",
                        },
                        "reason": "该项属于确定性安全协调。",
                    },
                    {
                        "decision": "final",
                        "reply": "内部维护已经完成。",
                        "reason": "测试模型错误地产生了公开话术。",
                    },
                ]
            ),
            model="fake",
            registry=service.gm_tool_registry,
        )

        result = service._session_heartbeat(
            {
                "campaign_id": "supervisor-test",
                "session_id": "s1",
                "channel_id": "group-1",
                "auto_respond": True,
                "force": True,
            }
        )

        assert result["action"] == "supervisor_recovery"
        assert result["send_reply"] is False
        assert result["reply"] == ""
        assert not runtime.app.clock_manager.exists("潮水没顶")
        assert any(
            item["tool_name"] == "reconcile_supervisor_state"
            and item["ok"]
            for item in result["tool_receipts"]
        )
