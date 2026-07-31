from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
    GMToolRegistry,
)


@dataclass(frozen=True)
class GMCapabilityDomain:
    name: str
    label: str
    description: str
    tools: frozenset[str]


class GMCapabilityBroker:
    """Publish a small semantic catalog instead of every tool schema at once."""

    DISCOVERY_TOOL = "discover_capabilities"
    SUPERVISOR_READ_TOOL = "inspect_supervisor_state"
    SUPERVISOR_ACK_TOOL = "acknowledge_supervisor_alert"

    _DOMAINS = (
        GMCapabilityDomain(
            "campaign",
            "战役与存读档",
            "新建、查看、保存、读取或删除战役及存档槽。",
            frozenset(
                {
                    "list_saves",
                    "inspect_campaign",
                    "create_campaign",
                    "save_campaign",
                    "load_campaign",
                    "delete_save",
                }
            ),
        ),
        GMCapabilityDomain(
            "table",
            "开团与桌面管理",
            "查询阶段、处理在离席、安全边界、开始、暂停或结束一场游戏。",
            frozenset(
                {
                    "get_session_status",
                    "set_player_attendance",
                    "record_safety_boundary",
                    "start_session",
                    "pause_session",
                    "end_session",
                }
            ),
        ),
        GMCapabilityDomain(
            "session_zero",
            "第零章与角色创建",
            "共同创建世界、确认方案、查询缺项、编辑或确认角色草稿。",
            frozenset(
                {
                    "get_session_zero_readiness",
                    "get_hero_drafts",
                    "get_hero_state",
                    "get_world_state",
                    "propose_session_zero_update",
                    "commit_session_zero_update",
                    "confirm_session_zero_proposal",
                    "mark_session_zero_topic_complete",
                    "set_session_zero_nudge_preference",
                    "pause_session_zero_nudges",
                    "update_hero_draft",
                    "confirm_hero_draft",
                    "create_loyal_companion",
                }
            ),
        ),
        GMCapabilityDomain(
            "scene",
            "场景与镜头",
            "查看、建立、切换、聚焦或结束场景，并提交环境变化与场内移动。",
            frozenset(
                {
                    "get_scene_state",
                    "start_scene",
                    "focus_scene_branch",
                    "transition_scene",
                    "end_scene",
                    "commit_scene_response",
                    "perform_in_scene_action",
                    "move_group_within_scene",
                    "move_scene_group",
                    "pass_in_scene_action",
                    "commit_story_item_action",
                }
            ),
        ),
        GMCapabilityDomain(
            "clock",
            "命刻",
            "查看、建立、推进、倒转或关闭目标、威胁和长期命刻。",
            frozenset({"get_clocks", "create_clock", "change_clock", "close_clock"}),
        ),
        GMCapabilityDomain(
            "npc",
            "NPC与集体",
            "查看或建立NPC档案，让NPC或集体回应、行动，并维护其状态。",
            frozenset(
                {
                    "get_npc_profiles",
                    "create_npc_profile",
                    "introduce_npc",
                    "update_npc_state",
                    "revise_npc_profile",
                    "decide_npc_response",
                    "decide_collective_response",
                    "decide_npc_action",
                    "decide_collective_action",
                }
            ),
        ),
        GMCapabilityDomain(
            "rules",
            "检定与角色行动",
            "执行属性检定、调查、妨碍、攻击、法术、装备、技能、休息、购物和仪式工程。",
            frozenset(
                {
                    "get_gameplay_state",
                    "perform_check_action",
                    "perform_character_action",
                    "perform_scene_action",
                    "perform_ritual_project_action",
                    "resolve_rule_window",
                    "resolve_gm_opportunity",
                    "learn_chimerist_spell",
                    "recall_scene_memory",
                    "resolve_tavern_talk",
                }
            ),
        ),
        GMCapabilityDomain(
            "conflict",
            "冲突、敌人与首领",
            "建立战斗档案、配置首领阶段、开始冲突、执行NPC回合或结束冲突。",
            frozenset(
                {
                    "get_gameplay_state",
                    "preview_npc_combatant",
                    "create_npc_combatant",
                    "configure_boss_phases",
                    "start_conflict",
                    "run_current_npc_turn",
                    "end_conflict",
                    "resolve_rule_window",
                    "resolve_gm_opportunity",
                }
            ),
        ),
        GMCapabilityDomain(
            "map",
            "世界地图",
            "查看、理解、放置、编辑或绘制世界地图及其地点。",
            frozenset(
                {
                    "get_world_map_status",
                    "inspect_semantic_map",
                    "find_map_location_candidates",
                    "place_world_map_locations",
                    "generate_world_map_preview",
                    "edit_world_map",
                }
            ),
        ),
        GMCapabilityDomain(
            "travel",
            "旅行",
            "查看路线与交通、开始或继续旅行、处理中断或放弃旅程。",
            frozenset(
                {
                    "get_travel_state",
                    "travel_party",
                    "continue_travel",
                    "abort_travel",
                }
            ),
        ),
        GMCapabilityDomain(
            "dungeon",
            "地下城",
            "查看、开始或结束地下城探索。",
            frozenset(
                {
                    "get_dungeon_state",
                    "start_dungeon_exploration",
                    "finish_dungeon_exploration",
                }
            ),
        ),
        GMCapabilityDomain(
            "reward",
            "奖励、成长与规则查询",
            "发放阶段奖励、查询或提升等级，并查阅技能、法术和装备规则。",
            frozenset(
                {
                    "award_stage_reward",
                    "get_progression_state",
                    "level_up_character",
                    "get_rule_reference",
                    "search_rule_references",
                }
            ),
        ),
        GMCapabilityDomain(
            "supervisor",
            "总控与诊断",
            "查看时悠的压缩驾驶舱、异常告警和工具熔断，并协调可安全修复的组件状态。",
            frozenset(
                {
                    SUPERVISOR_READ_TOOL,
                    SUPERVISOR_ACK_TOOL,
                    "reconcile_supervisor_state",
                    "get_runtime_state",
                }
            ),
        ),
    )
    _BY_NAME = {domain.name: domain for domain in _DOMAINS}

    @classmethod
    def domain_names(cls) -> tuple[str, ...]:
        return tuple(domain.name for domain in cls._DOMAINS)

    @classmethod
    def all_catalogued_tools(cls) -> set[str]:
        return set().union(*(domain.tools for domain in cls._DOMAINS))

    @classmethod
    def catalog(
        cls,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        *,
        phase_tools: set[str],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        registered = set(registry._tools)
        for domain in cls._DOMAINS:
            tools = sorted(domain.tools & phase_tools & registered)
            if not tools:
                continue
            rows.append(
                {
                    "domain": domain.name,
                    "label": domain.label,
                    "purpose": domain.description,
                    "available_tool_count": len(tools),
                }
            )
        return rows

    @classmethod
    def tools_for_domains(
        cls,
        domains: Iterable[str],
        *,
        registry: GMToolRegistry,
        phase_tools: set[str],
    ) -> set[str]:
        selected: set[str] = set()
        for raw_name in domains:
            domain = cls._BY_NAME.get(str(raw_name or "").strip())
            if domain is not None:
                selected.update(domain.tools)
        return selected & set(registry._tools) & phase_tools

    @classmethod
    def domains_for_tools(
        cls,
        tool_names: Iterable[str],
    ) -> list[str]:
        requested = {
            str(name or "").strip()
            for name in tool_names
            if str(name or "").strip()
        }
        return [
            domain.name
            for domain in cls._DOMAINS
            if requested & set(domain.tools)
        ]

    @classmethod
    def initial_tool_names(
        cls,
        *,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        phase_tools: set[str],
    ) -> set[str]:
        if context.metadata.get("system_gm_beat_request"):
            return set(phase_tools) & set(registry._tools)
        return {
            name
            for name in (
                cls.DISCOVERY_TOOL,
                cls.SUPERVISOR_READ_TOOL,
            )
            if name in phase_tools and name in registry._tools
        }

    @classmethod
    def granted_tool_names(
        cls,
        context: GMToolExecutionContext,
    ) -> set[str]:
        return {
            str(name or "").strip()
            for name in list(
                context.metadata.get("gm_discovered_tool_names") or []
            )
            if str(name or "").strip()
        }

    @classmethod
    def grant(
        cls,
        context: GMToolExecutionContext,
        names: Iterable[str],
    ) -> set[str]:
        granted = cls.granted_tool_names(context)
        granted.update(
            str(name or "").strip()
            for name in names
            if str(name or "").strip()
        )
        context.metadata["gm_discovered_tool_names"] = sorted(granted)
        return granted


@dataclass
class GMSupervisorAlert:
    alert_id: str
    campaign_id: str
    code: str
    severity: str
    component: str
    summary: str
    suggested_domains: list[str] = field(default_factory=list)
    tool_hints: list[str] = field(default_factory=list)
    created_at: str = ""
    last_seen_at: str = ""
    occurrences: int = 1
    status: str = "open"


@dataclass
class _CircuitState:
    tool_name: str
    error_code: str
    failures: int
    opened_at_monotonic: float
    reopen_after_monotonic: float
    last_message: str = ""


class GMSupervisorMonitor:
    """Event-driven control plane for GM-visible process health.

    The monitor never mutates game objects. It observes authoritative snapshots
    and receipts, raises bounded alerts, and can temporarily reject a repeatedly
    failing write capability through the existing admission guard.
    """

    _SNAPSHOT_ALERT_CODES = frozenset(
        {
            "ADVENTURE_WITHOUT_SCENE",
            "SCENE_FRAME_WITHOUT_SCENE",
            "CONFLICT_WITHOUT_ACTOR",
            "CONFLICT_ACTOR_OUTSIDE_ORDER",
            "CONFLICT_TURN_STATE_CORRUPT",
            "TURN_END_WINDOW_MISMATCH",
            "HELD_ACTION_STATE_MISMATCH",
            "BLOCKING_DECISION_RESUME_MISMATCH",
            "MULTIPLE_BLOCKING_DECISIONS",
            "BLOCKING_DECISION_WITHOUT_RESPONDER",
            "STALE_SCENE_DECISION_WINDOW",
            "FULFILLED_CLOCK_STILL_ACTIVE",
            "SCENE_FRAME_FOCUS_MISMATCH",
            "SCENE_CLOCK_OUTSIDE_LIFECYCLE",
            "CLOCK_PRESSURE_BUDGET_EXCEEDED",
            "AUTO_CLOCK_BUDGET_EXCEEDED",
            "ACTIVE_DUNGEON_WITHOUT_SCENE",
            "DUNGEON_SCENE_MISMATCH",
            "TRAVEL_EVENT_WITHOUT_JOURNEY",
            "RITUAL_CLOCK_MISSING",
            "RITUAL_SCENE_MISMATCH",
            "RITUAL_CASTER_MISSING",
            "RITUAL_READY_STATE_MISMATCH",
            "ACTION_ROUND_STATE_CORRUPT",
            "ACTION_ROUND_OUTSIDE_SCENE",
            "TRAVEL_PROGRESS_STATE_CORRUPT",
            "TRAVEL_PENDING_EVENT_MISSING",
            "DUNGEON_AREA_STATE_CORRUPT",
            "DUNGEON_DANGER_CLOCK_MISSING",
            "DUNGEON_TRAVEL_NESTING_INVALID",
            "PROJECT_PROGRESS_STATE_CORRUPT",
            "PROJECT_COMPLETION_NOT_PERSISTED",
            "ADVENTURE_SESSION_LEDGER_INACTIVE",
            "INACTIVE_TABLE_WITH_OPEN_LEDGER",
            "SESSION_LEDGER_ID_MISMATCH",
        }
    )
    _AUTONOMOUS_REPAIR_CODES = frozenset(
        {
            "FULFILLED_CLOCK_STILL_ACTIVE",
            "SCENE_FRAME_FOCUS_MISMATCH",
        }
    )

    def __init__(
        self,
        *,
        max_events: int = 200,
        failure_threshold: int = 3,
        circuit_seconds: float = 120.0,
    ) -> None:
        self.max_events = max(40, int(max_events))
        self.failure_threshold = max(2, int(failure_threshold))
        self.circuit_seconds = max(10.0, float(circuit_seconds))
        self._alerts: dict[str, GMSupervisorAlert] = {}
        self._alert_order: list[str] = []
        self._failure_runs: dict[tuple[str, str, str], int] = {}
        self._circuits: dict[tuple[str, str], _CircuitState] = {}
        self._event_counter = 0

    def scan(
        self,
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> list[dict[str, object]]:
        campaign_id = str(context.campaign_id or "").strip()
        seen_codes: set[str] = set()
        scene = dict(state.get("scene") or {})
        runtime = dict(state.get("runtime") or {})
        gameplay = dict(state.get("gameplay") or {})
        clocks = dict(state.get("clocks") or {})
        processes = dict(state.get("processes") or {})
        process_scene = dict(processes.get("scene") or {})
        process_decisions = dict(processes.get("decisions") or {})
        process_clocks = dict(processes.get("clocks") or {})
        process_travel = dict(processes.get("travel") or {})
        process_dungeon = dict(processes.get("dungeon") or {})
        process_session = dict(processes.get("session") or {})
        process_projects = [
            item
            for item in list(processes.get("projects") or [])
            if isinstance(item, dict)
        ]
        conflict = {
            **dict(
                runtime.get("conflict")
                or gameplay.get("conflict")
                or {}
            ),
            **dict(processes.get("conflict") or {}),
        }
        process_pending = [
            item
            for item in list(process_decisions.get("pending") or [])
            if isinstance(item, dict)
        ]

        if context.gate_status == "adventure" and not bool(scene.get("active")):
            seen_codes.add("ADVENTURE_WITHOUT_SCENE")
            self._emit(
                campaign_id,
                code="ADVENTURE_WITHOUT_SCENE",
                severity="warning",
                component="scene",
                summary="冒险阶段没有活动场景；需要开场或确认会话是否应暂停。",
                suggested_domains=["scene", "table"],
                tool_hints=["start_scene", "pause_session"],
            )
        if (
            bool(scene.get("frame_active") or process_scene.get("frame_active"))
            and not bool(
                process_scene.get("authoritative_active")
                or scene.get("active")
            )
        ):
            seen_codes.add("SCENE_FRAME_WITHOUT_SCENE")
            self._emit(
                campaign_id,
                code="SCENE_FRAME_WITHOUT_SCENE",
                severity="warning",
                component="scene",
                summary="GM场景框架仍在活动，但权威场景已经不存在。",
                suggested_domains=["scene", "supervisor"],
                tool_hints=[
                    "get_scene_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )

        action_round = dict(process_scene.get("action_round") or {})
        required = self._nonempty_strings(
            action_round.get("required")
        )
        acted = self._nonempty_strings(action_round.get("acted"))
        waiting = self._nonempty_strings(action_round.get("waiting"))
        expected_waiting = [
            name for name in dict.fromkeys(required) if name not in set(acted)
        ]
        round_state_corrupt = bool(
            len(required) != len(set(required))
            or len(acted) != len(set(acted))
            or len(waiting) != len(set(waiting))
            or any(name not in set(required) for name in acted)
            or waiting != expected_waiting
        )
        if round_state_corrupt:
            seen_codes.add("ACTION_ROUND_STATE_CORRUPT")
            self._emit(
                campaign_id,
                code="ACTION_ROUND_STATE_CORRUPT",
                severity="critical",
                component="scene",
                summary="自由场景行动轮的参与、已行动或等待名单彼此矛盾，自动命刻可能错过或重复推进。",
                suggested_domains=["scene", "supervisor"],
                tool_hints=[
                    "get_scene_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )
        if (
            (required or acted or waiting)
            and not bool(process_scene.get("authoritative_active"))
            and not list(process_scene.get("suspended_scene_ids") or [])
        ):
            seen_codes.add("ACTION_ROUND_OUTSIDE_SCENE")
            self._emit(
                campaign_id,
                code="ACTION_ROUND_OUTSIDE_SCENE",
                severity="warning",
                component="scene",
                summary="没有当前或暂存场景时仍残留自由场景行动轮进度。",
                suggested_domains=["scene", "supervisor"],
                tool_hints=[
                    "get_scene_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )

        if bool(conflict.get("active")):
            current_actor = str(conflict.get("current_actor") or "").strip()
            turn_order = [
                str(item or "").strip()
                for item in list(conflict.get("turn_order") or [])
                if str(item or "").strip()
            ]
            if not current_actor:
                seen_codes.add("CONFLICT_WITHOUT_ACTOR")
                self._emit(
                    campaign_id,
                    code="CONFLICT_WITHOUT_ACTOR",
                    severity="critical",
                    component="conflict",
                    summary="冲突处于活动状态，但没有当前行动者。",
                    suggested_domains=["conflict"],
                    tool_hints=["get_gameplay_state", "end_conflict"],
                )
            elif turn_order and current_actor not in turn_order:
                seen_codes.add("CONFLICT_ACTOR_OUTSIDE_ORDER")
                self._emit(
                    campaign_id,
                    code="CONFLICT_ACTOR_OUTSIDE_ORDER",
                    severity="critical",
                    component="conflict",
                    summary=f"当前行动者【{current_actor}】不在冲突行动顺序中。",
                    suggested_domains=["conflict"],
                    tool_hints=["get_gameplay_state"],
                )
            current_index = self._safe_int(
                conflict.get("current_turn_index")
            )
            turn_started_actor = str(
                conflict.get("turn_started_actor") or ""
            ).strip()
            current_bonus_actor = str(
                conflict.get("current_bonus_actor") or ""
            ).strip()
            queued_turns = self._nonempty_strings(
                conflict.get("queued_turns")
            )
            queued_turn_kinds = [
                str(item or "").strip()
                for item in list(
                    conflict.get("queued_turn_kinds") or []
                )
            ]
            structural_turn_error = bool(
                (turn_order and not (0 <= current_index < len(turn_order)))
                or len(queued_turns) != len(queued_turn_kinds)
                or (
                    current_bonus_actor
                    and current_bonus_actor != current_actor
                )
                or (
                    turn_started_actor
                    and turn_started_actor != current_actor
                    and not any(
                        bool(item.get("blocking"))
                        for item in process_pending
                    )
                )
            )
            if structural_turn_error:
                seen_codes.add("CONFLICT_TURN_STATE_CORRUPT")
                self._emit(
                    campaign_id,
                    code="CONFLICT_TURN_STATE_CORRUPT",
                    severity="critical",
                    component="conflict",
                    summary="冲突的行动索引、已开始回合或奖励回合队列彼此矛盾。",
                    suggested_domains=["conflict", "supervisor"],
                    tool_hints=[
                        "get_gameplay_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )

            pending_turn_end_actor = str(
                conflict.get("pending_turn_end_actor") or ""
            ).strip()
            acceleration_windows = [
                item
                for item in process_pending
                if str(item.get("kind") or "")
                == "acceleration_benefit"
            ]
            turn_end_window_mismatch = bool(
                pending_turn_end_actor
                and (
                    pending_turn_end_actor
                    != (turn_started_actor or current_actor)
                    or not any(
                        str(item.get("owner") or "")
                        == pending_turn_end_actor
                        and bool(item.get("blocking"))
                        for item in acceleration_windows
                    )
                )
            ) or bool(
                not pending_turn_end_actor
                and any(
                    bool(item.get("blocking"))
                    for item in acceleration_windows
                )
            )
            if turn_end_window_mismatch:
                seen_codes.add("TURN_END_WINDOW_MISMATCH")
                self._emit(
                    campaign_id,
                    code="TURN_END_WINDOW_MISMATCH",
                    severity="critical",
                    component="conflict",
                    summary="加速术的回合末暂停标记与其待决窗口不一致，行动顺序可能无法恢复。",
                    suggested_domains=["rules", "conflict"],
                    tool_hints=[
                        "get_gameplay_state",
                        "resolve_rule_window",
                    ],
                )

            held_actions = [
                item
                for item in list(conflict.get("held_actions") or [])
                if isinstance(item, dict)
            ]
            held_windows = [
                item
                for item in process_pending
                if str(item.get("kind") or "") == "held_action"
            ]
            held_actors = [
                str(item.get("actor") or "").strip()
                for item in held_actions
                if str(item.get("actor") or "").strip()
            ]
            held_window_owners = [
                str(item.get("owner") or "").strip()
                for item in held_windows
                if str(item.get("owner") or "").strip()
            ]
            if (
                len(held_actors) != len(set(held_actors))
                or set(held_actors) != set(held_window_owners)
                or any(actor not in set(turn_order) for actor in held_actors)
            ):
                seen_codes.add("HELD_ACTION_STATE_MISMATCH")
                self._emit(
                    campaign_id,
                    code="HELD_ACTION_STATE_MISMATCH",
                    severity="warning",
                    component="conflict",
                    summary="回合外缓存行动与其确认窗口或当前参战者不一致。",
                    suggested_domains=["rules", "conflict"],
                    tool_hints=["get_gameplay_state"],
                )

            turn_serial = self._safe_int(
                conflict.get("turn_serial")
            )
            resume_windows = [
                item
                for item in process_pending
                if bool(item.get("blocking"))
                and self._safe_int(
                    item.get("deferred_turn_serial")
                )
                > 0
            ]
            if any(
                self._safe_int(
                    item.get("deferred_turn_serial")
                )
                != turn_serial
                for item in resume_windows
            ):
                seen_codes.add("BLOCKING_DECISION_RESUME_MISMATCH")
                self._emit(
                    campaign_id,
                    code="BLOCKING_DECISION_RESUME_MISMATCH",
                    severity="critical",
                    component="decision_window",
                    summary="阻塞选择保存的回合恢复点已经不等于当前回合事务。",
                    suggested_domains=["rules", "conflict", "supervisor"],
                    tool_hints=[
                        "get_gameplay_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )

        pending = [
            item
            for item in list(gameplay.get("pending_decisions") or [])
            if isinstance(item, dict) and bool(item.get("blocking"))
        ]
        if len(pending) > 1:
            seen_codes.add("MULTIPLE_BLOCKING_DECISIONS")
            self._emit(
                campaign_id,
                code="MULTIPLE_BLOCKING_DECISIONS",
                severity="warning",
                component="decision_window",
                summary=f"当前同时存在 {len(pending)} 个阻塞待决窗口，应确认回应者与恢复顺序。",
                suggested_domains=["rules"],
                tool_hints=["get_gameplay_state", "resolve_rule_window"],
            )
        if any(
            bool(item.get("blocking"))
            and not [
                responder
                for responder in list(
                    item.get("allowed_responders") or []
                )
                if str(responder or "").strip()
            ]
            for item in process_pending
        ):
            seen_codes.add("BLOCKING_DECISION_WITHOUT_RESPONDER")
            self._emit(
                campaign_id,
                code="BLOCKING_DECISION_WITHOUT_RESPONDER",
                severity="critical",
                component="decision_window",
                summary="有阻塞待决窗口没有合法回应者，规则事务将无法恢复。",
                suggested_domains=["rules"],
                tool_hints=[
                    "get_gameplay_state",
                    "resolve_rule_window",
                ],
            )

        valid_scene_ids = {
            str(process_scene.get("scene_id") or "").strip(),
            *{
                str(item or "").strip()
                for item in list(
                    process_scene.get("suspended_scene_ids") or []
                )
            },
        }
        valid_scene_ids.discard("")
        stale_scene_decisions = [
            item
            for item in process_pending
            if str(item.get("scope_kind") or "") == "scene"
            and str(item.get("scope_id") or "").strip()
            and str(item.get("scope_id") or "").strip()
            not in valid_scene_ids
        ]
        if stale_scene_decisions:
            seen_codes.add("STALE_SCENE_DECISION_WINDOW")
            self._emit(
                campaign_id,
                code="STALE_SCENE_DECISION_WINDOW",
                severity="critical",
                component="decision_window",
                summary="有场景级待决窗口不再属于当前或暂存场景。",
                suggested_domains=["rules", "scene"],
                tool_hints=[
                    "get_gameplay_state",
                    "get_scene_state",
                ],
            )

        active_clocks = [
            item
            for item in list(clocks.get("active") or [])
            if isinstance(item, dict)
        ]
        terminal = [
            item
            for item in active_clocks
            if str(item.get("clock_type") or "") != "ritual"
            and int(item.get("current") or 0) >= int(item.get("max_segments") or 0) > 0
        ]
        if terminal:
            seen_codes.add("FULFILLED_CLOCK_STILL_ACTIVE")
            self._emit(
                campaign_id,
                code="FULFILLED_CLOCK_STILL_ACTIVE",
                severity="critical",
                component="clock",
                summary="有已填满的非仪式命刻仍留在活动列表，后果可能重复兑现。",
                suggested_domains=["supervisor"],
                tool_hints=[
                    self.SUPERVISOR_READ_TOOL,
                    "reconcile_supervisor_state",
                ],
            )

        scene_id = str(scene.get("scene_id") or "").strip()
        frame_id = str(scene.get("frame_source_scene_id") or "").strip()
        if scene_id and frame_id and scene_id != frame_id:
            seen_codes.add("SCENE_FRAME_FOCUS_MISMATCH")
            self._emit(
                campaign_id,
                code="SCENE_FRAME_FOCUS_MISMATCH",
                severity="warning",
                component="scene",
                summary="当前镜头与GM场景框架指向不同分支。",
                suggested_domains=["supervisor"],
                tool_hints=[
                    self.SUPERVISOR_READ_TOOL,
                    "reconcile_supervisor_state",
                ],
            )

        leaked_scene_clocks = [
            item
            for item in list(process_clocks.get("scene_scoped") or [])
            if isinstance(item, dict)
            and str(item.get("scene_id") or "").strip()
            and str(item.get("scene_id") or "").strip()
            not in valid_scene_ids
        ]
        if leaked_scene_clocks:
            seen_codes.add("SCENE_CLOCK_OUTSIDE_LIFECYCLE")
            self._emit(
                campaign_id,
                code="SCENE_CLOCK_OUTSIDE_LIFECYCLE",
                severity="critical",
                component="clock",
                summary="有场景级命刻不再属于当前或暂存场景，可能跨场景错误推进。",
                suggested_domains=["clock", "scene"],
                tool_hints=["get_clocks", "get_scene_state"],
            )

        pacing_budget = dict(
            process_clocks.get("pacing_budget")
            or clocks.get("pacing_budget")
            or {}
        )
        foreground_count = len(
            list(process_clocks.get("foreground_pressure_names") or [])
        )
        foreground_limit = int(
            pacing_budget.get("max_foreground_pressure_clocks") or 0
        )
        if foreground_limit > 0 and foreground_count > foreground_limit:
            seen_codes.add("CLOCK_PRESSURE_BUDGET_EXCEEDED")
            self._emit(
                campaign_id,
                code="CLOCK_PRESSURE_BUDGET_EXCEEDED",
                severity="warning",
                component="clock",
                summary=(
                    f"当前有 {foreground_count} 条前台压力命刻，"
                    f"超过本阶段预算 {foreground_limit} 条。"
                ),
                suggested_domains=["clock"],
                tool_hints=["get_clocks", "close_clock"],
            )
        auto_count = len(
            list(process_clocks.get("auto_advance_names") or [])
        )
        auto_limit = int(
            pacing_budget.get("max_auto_advance_clocks") or 0
        )
        if auto_limit > 0 and auto_count > auto_limit:
            seen_codes.add("AUTO_CLOCK_BUDGET_EXCEEDED")
            self._emit(
                campaign_id,
                code="AUTO_CLOCK_BUDGET_EXCEEDED",
                severity="warning",
                component="clock",
                summary=(
                    f"当前有 {auto_count} 条自动推进命刻，"
                    f"超过本阶段预算 {auto_limit} 条。"
                ),
                suggested_domains=["clock"],
                tool_hints=["get_clocks", "close_clock"],
            )

        dungeon_active = bool(process_dungeon.get("active"))
        if dungeon_active and not bool(
            process_scene.get("authoritative_active")
            or scene.get("active")
        ):
            seen_codes.add("ACTIVE_DUNGEON_WITHOUT_SCENE")
            self._emit(
                campaign_id,
                code="ACTIVE_DUNGEON_WITHOUT_SCENE",
                severity="critical",
                component="dungeon",
                summary="地下城探索仍在活动，但没有承载它的权威场景。",
                suggested_domains=["dungeon", "scene"],
                tool_hints=[
                    "get_dungeon_state",
                    "get_scene_state",
                ],
            )
        elif (
            dungeon_active
            and not bool(conflict.get("active"))
            and str(process_scene.get("scene_type") or "")
            != "dungeon"
        ):
            seen_codes.add("DUNGEON_SCENE_MISMATCH")
            self._emit(
                campaign_id,
                code="DUNGEON_SCENE_MISMATCH",
                severity="warning",
                component="dungeon",
                summary="地下城探索处于活动状态，但当前非冲突场景不是地下城场景。",
                suggested_domains=["dungeon", "scene"],
                tool_hints=[
                    "get_dungeon_state",
                    "get_scene_state",
                ],
            )

        if bool(process_travel.get("pending_event")) and not bool(
            process_travel.get("active")
        ):
            seen_codes.add("TRAVEL_EVENT_WITHOUT_JOURNEY")
            self._emit(
                campaign_id,
                code="TRAVEL_EVENT_WITHOUT_JOURNEY",
                severity="critical",
                component="travel",
                summary="存在等待处理的旅行事件，但没有活动旅程。",
                suggested_domains=["travel"],
                tool_hints=["get_travel_state"],
            )
        if bool(process_travel.get("active")):
            current_day = int(process_travel.get("current_day") or 0)
            total_days = int(process_travel.get("total_days") or 0)
            status = str(process_travel.get("status") or "")
            resolved_days = [
                int(item)
                for item in list(
                    process_travel.get("resolved_day_numbers") or []
                )
                if isinstance(item, int) or str(item).isdigit()
            ]
            if (
                total_days <= 0
                or current_day < 0
                or current_day > total_days
                or status not in {"traveling", "event_pending"}
                or len(resolved_days) != len(set(resolved_days))
                or any(day < 1 or day > current_day for day in resolved_days)
                or len(resolved_days) != current_day
            ):
                seen_codes.add("TRAVEL_PROGRESS_STATE_CORRUPT")
                self._emit(
                    campaign_id,
                    code="TRAVEL_PROGRESS_STATE_CORRUPT",
                    severity="critical",
                    component="travel",
                    summary="活动旅程的旅行日、已结算日期或状态彼此矛盾。",
                    suggested_domains=["travel", "supervisor"],
                    tool_hints=[
                        "get_travel_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )
            pending_event = bool(process_travel.get("pending_event"))
            pending_day = int(
                process_travel.get("pending_event_day") or 0
            )
            if (
                (status == "event_pending") != pending_event
                or (pending_event and pending_day not in resolved_days)
                or (not pending_event and pending_day > 0)
            ):
                seen_codes.add("TRAVEL_PENDING_EVENT_MISSING")
                self._emit(
                    campaign_id,
                    code="TRAVEL_PENDING_EVENT_MISSING",
                    severity="critical",
                    component="travel",
                    summary="旅程标记为等待事件，但找不到对应旅行日，或事件状态没有正确清除。",
                    suggested_domains=["travel", "supervisor"],
                    tool_hints=[
                        "get_travel_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )

        if dungeon_active:
            if bool(process_travel.get("active")) and not (
                str(process_travel.get("status") or "") == "event_pending"
                and bool(process_travel.get("pending_event"))
                and str(process_travel.get("pending_event_type") or "")
                == "discovery"
                and "dungeon"
                in self._nonempty_strings(
                    process_travel.get("pending_event_tags")
                )
            ):
                seen_codes.add("DUNGEON_TRAVEL_NESTING_INVALID")
                self._emit(
                    campaign_id,
                    code="DUNGEON_TRAVEL_NESTING_INVALID",
                    severity="critical",
                    component="dungeon",
                    summary="地下城与旅程同时活动，但旅程并非挂起在地下城发现事件上。",
                    suggested_domains=[
                        "dungeon",
                        "travel",
                        "supervisor",
                    ],
                    tool_hints=[
                        "get_dungeon_state",
                        "get_travel_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )
            area_names = self._nonempty_strings(
                process_dungeon.get("area_names")
            )
            current_area = str(
                process_dungeon.get("current_area") or ""
            ).strip()
            if current_area and (
                not area_names or current_area not in set(area_names)
            ):
                seen_codes.add("DUNGEON_AREA_STATE_CORRUPT")
                self._emit(
                    campaign_id,
                    code="DUNGEON_AREA_STATE_CORRUPT",
                    severity="critical",
                    component="dungeon",
                    summary=f"地下城当前位置【{current_area}】不在其区域结构中。",
                    suggested_domains=["dungeon", "supervisor"],
                    tool_hints=[
                        "get_dungeon_state",
                        self.SUPERVISOR_READ_TOOL,
                    ],
                )
            missing_danger_clocks = self._nonempty_strings(
                process_dungeon.get("missing_danger_clock_names")
            )
            if missing_danger_clocks:
                seen_codes.add("DUNGEON_DANGER_CLOCK_MISSING")
                self._emit(
                    campaign_id,
                    code="DUNGEON_DANGER_CLOCK_MISSING",
                    severity="critical",
                    component="dungeon",
                    summary=(
                        "活动地下城找不到危险命刻："
                        + "、".join(missing_danger_clocks[:4])
                    ),
                    suggested_domains=["dungeon", "clock"],
                    tool_hints=["get_dungeon_state", "get_clocks"],
                )

        active_rituals = [
            item
            for item in list(processes.get("rituals") or [])
            if isinstance(item, dict)
        ]
        if any(not bool(item.get("clock_exists")) for item in active_rituals):
            seen_codes.add("RITUAL_CLOCK_MISSING")
            self._emit(
                campaign_id,
                code="RITUAL_CLOCK_MISSING",
                severity="critical",
                component="ritual",
                summary="有活动仪式计划找不到对应命刻，仪式无法继续或最终施法。",
                suggested_domains=["rules", "clock"],
                tool_hints=[
                    "get_gameplay_state",
                    "get_clocks",
                ],
            )
        if any(not bool(item.get("caster_exists")) for item in active_rituals):
            seen_codes.add("RITUAL_CASTER_MISSING")
            self._emit(
                campaign_id,
                code="RITUAL_CASTER_MISSING",
                severity="critical",
                component="ritual",
                summary="有活动仪式的施法者已不在角色档案中。",
                suggested_domains=["rules", "supervisor"],
                tool_hints=[
                    "get_gameplay_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )
        ritual_ready_mismatch = [
            item
            for item in active_rituals
            if bool(item.get("clock_exists"))
            and (
                (
                    str(item.get("clock_status") or "") == "ready"
                    and int(item.get("clock_current") or 0)
                    < int(item.get("clock_max_segments") or 0)
                )
                or (
                    int(item.get("ready_turn_serial") or 0) > 0
                    and not bool(item.get("ready"))
                )
            )
        ]
        if ritual_ready_mismatch:
            seen_codes.add("RITUAL_READY_STATE_MISMATCH")
            self._emit(
                campaign_id,
                code="RITUAL_READY_STATE_MISMATCH",
                severity="critical",
                component="ritual",
                summary="活动仪式的命刻进度、就绪状态与就绪回合记录彼此矛盾。",
                suggested_domains=["rules", "clock"],
                tool_hints=["get_gameplay_state", "get_clocks"],
            )
        ritual_scene_mismatch = [
            item
            for item in active_rituals
            if str(item.get("scene_id") or "").strip()
            and str(item.get("scene_id") or "").strip()
            not in valid_scene_ids
        ]
        if ritual_scene_mismatch:
            seen_codes.add("RITUAL_SCENE_MISMATCH")
            self._emit(
                campaign_id,
                code="RITUAL_SCENE_MISMATCH",
                severity="critical",
                component="ritual",
                summary="有冲突仪式仍绑定到已经离开的场景。",
                suggested_domains=["rules", "scene"],
                tool_hints=[
                    "get_gameplay_state",
                    "get_scene_state",
                ],
            )

        corrupt_projects = [
            item
            for item in process_projects
            if int(item.get("required_progress") or 0) <= 0
            or int(item.get("current_progress") or 0) < 0
            or int(item.get("current_progress") or 0)
            > int(item.get("required_progress") or 0)
            or (
                bool(item.get("completed"))
                != (
                    int(item.get("current_progress") or 0)
                    >= int(item.get("required_progress") or 0) > 0
                )
            )
        ]
        if corrupt_projects:
            seen_codes.add("PROJECT_PROGRESS_STATE_CORRUPT")
            self._emit(
                campaign_id,
                code="PROJECT_PROGRESS_STATE_CORRUPT",
                severity="critical",
                component="project",
                summary="有工程的当前进度、所需进度与完成标记彼此矛盾。",
                suggested_domains=["rules", "supervisor"],
                tool_hints=[
                    "get_gameplay_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )
        unpersisted_projects = [
            item
            for item in process_projects
            if bool(item.get("completed"))
            and (
                not bool(item.get("persisted"))
                or not str(item.get("created_asset_id") or "").strip()
            )
        ]
        if unpersisted_projects:
            seen_codes.add("PROJECT_COMPLETION_NOT_PERSISTED")
            self._emit(
                campaign_id,
                code="PROJECT_COMPLETION_NOT_PERSISTED",
                severity="critical",
                component="project",
                summary="有已完成工程没有对应的持久化产物，不能只靠叙述补写。",
                suggested_domains=["rules", "supervisor"],
                tool_hints=[
                    "get_gameplay_state",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )

        ledger_active = bool(process_session.get("ledger_active"))
        if (
            process_session
            and context.gate_status == "adventure"
            and not ledger_active
        ):
            seen_codes.add("ADVENTURE_SESSION_LEDGER_INACTIVE")
            self._emit(
                campaign_id,
                code="ADVENTURE_SESSION_LEDGER_INACTIVE",
                severity="critical",
                component="session",
                summary="桌面处于冒险阶段，但场次资源与经验账本没有活动。",
                suggested_domains=["table"],
                tool_hints=[
                    "get_session_status",
                    "pause_session",
                ],
            )
        if (
            process_session
            and context.gate_status == "inactive"
            and ledger_active
        ):
            seen_codes.add("INACTIVE_TABLE_WITH_OPEN_LEDGER")
            self._emit(
                campaign_id,
                code="INACTIVE_TABLE_WITH_OPEN_LEDGER",
                severity="critical",
                component="session",
                summary="桌面已停止，但场次账本仍然打开，结团与经验结算可能未完成。",
                suggested_domains=["table"],
                tool_hints=["get_session_status", "end_session"],
            )
        ledger_session_id = str(
            process_session.get("ledger_session_id") or ""
        ).strip()
        request_session_id = str(context.session_id or "").strip()
        if (
            ledger_active
            and ledger_session_id
            and request_session_id
            and ledger_session_id != request_session_id
        ):
            seen_codes.add("SESSION_LEDGER_ID_MISMATCH")
            self._emit(
                campaign_id,
                code="SESSION_LEDGER_ID_MISMATCH",
                severity="critical",
                component="session",
                summary=(
                    f"当前消息属于场次【{request_session_id}】，"
                    f"但资源与经验账本仍绑定【{ledger_session_id}】。"
                ),
                suggested_domains=["table", "supervisor"],
                tool_hints=[
                    "get_session_status",
                    self.SUPERVISOR_READ_TOOL,
                ],
            )
        self._resolve_cleared_snapshot_alerts(campaign_id, seen_codes)
        return self.active_alerts(campaign_id)

    @staticmethod
    def _nonempty_strings(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return [
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        ]

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return int(default)

    def autonomous_repair_alerts(
        self,
        campaign_id: str,
    ) -> list[dict[str, object]]:
        """Return only anomalies whose repair is deterministic and reversible.

        Corrupt initiative, competing decision windows, and missing scenes can
        require human judgement. They remain visible in the audit panel but are
        deliberately excluded from background intervention.
        """

        return [
            item
            for item in self.active_alerts(campaign_id)
            if str(item.get("code") or "") in self._AUTONOMOUS_REPAIR_CODES
        ]

    def observe_receipts(
        self,
        context: GMToolExecutionContext,
        receipts: Iterable[GMToolReceipt],
    ) -> dict[str, object]:
        campaign_id = str(context.campaign_id or "").strip()
        observed: list[dict[str, object]] = []
        for receipt in receipts:
            tool_name = str(receipt.tool_name or "").strip()
            if not tool_name:
                continue
            if receipt.ok:
                self._clear_failures(campaign_id, tool_name)
                observed.append({"tool_name": tool_name, "status": "ok"})
                continue
            error_code = str(receipt.error_code or "TOOL_REJECTED").strip()
            key = (campaign_id, tool_name, error_code)
            failures = self._failure_runs.get(key, 0) + 1
            self._failure_runs[key] = failures
            observed.append(
                {
                    "tool_name": tool_name,
                    "status": "failed",
                    "error_code": error_code,
                    "consecutive_failures": failures,
                }
            )
            if (
                failures >= self.failure_threshold
                and not self._never_circuit(tool_name)
            ):
                now = time.monotonic()
                self._circuits[(campaign_id, tool_name)] = _CircuitState(
                    tool_name=tool_name,
                    error_code=error_code,
                    failures=failures,
                    opened_at_monotonic=now,
                    reopen_after_monotonic=now + self.circuit_seconds,
                    last_message=str(receipt.message or "")[:240],
                )
                self._emit(
                    campaign_id,
                    code=f"TOOL_CIRCUIT_OPEN:{tool_name}",
                    severity="critical",
                    component="tool_runtime",
                    summary=(
                        f"能力【{tool_name}】连续 {failures} 次以 "
                        f"{error_code} 失败，已暂时停止自动重试。"
                    ),
                    suggested_domains=["supervisor"],
                    tool_hints=[self.SUPERVISOR_READ_TOOL],
                )
        return {
            "observed": observed,
            "open_circuits": self.circuit_snapshot(campaign_id),
        }

    def admission_error(
        self,
        definition: GMToolDefinition,
        context: GMToolExecutionContext,
    ) -> GMToolReceipt | None:
        if definition.side_effect == "read":
            return None
        campaign_id = str(context.campaign_id or "").strip()
        key = (campaign_id, definition.name)
        circuit = self._circuits.get(key)
        if circuit is None:
            return None
        now = time.monotonic()
        if now >= circuit.reopen_after_monotonic:
            self._circuits.pop(key, None)
            self._clear_failures(campaign_id, definition.name)
            return None
        remaining = max(1, int(circuit.reopen_after_monotonic - now))
        return GMToolReceipt.failure(
            definition.name,
            "SUPERVISOR_CIRCUIT_OPEN",
            f"该能力连续失败，已暂停自动调用约 {remaining} 秒。",
            "不要改用其他写工具绕过；先读取总控告警或等待熔断恢复。",
            retryable=False,
            result={
                "tool_name": definition.name,
                "last_error_code": circuit.error_code,
                "remaining_seconds": remaining,
            },
        )

    def acknowledge(
        self,
        campaign_id: str,
        alert_id: str,
        *,
        note: str = "",
    ) -> GMSupervisorAlert | None:
        alert = self._alerts.get(str(alert_id or "").strip())
        if alert is None or alert.campaign_id != str(campaign_id or "").strip():
            return None
        alert.status = "acknowledged"
        if note:
            alert.summary = f"{alert.summary} 处理备注：{str(note).strip()[:180]}"
        return alert

    def resolve(
        self,
        campaign_id: str,
        alert_id: str,
        *,
        note: str = "",
    ) -> GMSupervisorAlert | None:
        alert = self._alerts.get(str(alert_id or "").strip())
        if alert is None or alert.campaign_id != str(campaign_id or "").strip():
            return None
        alert.status = "resolved"
        if note:
            alert.summary = f"{alert.summary} 处理备注：{str(note).strip()[:180]}"
        return alert

    def active_alerts(self, campaign_id: str) -> list[dict[str, object]]:
        clean_campaign = str(campaign_id or "").strip()
        return [
            asdict(self._alerts[alert_id])
            for alert_id in reversed(self._alert_order)
            if alert_id in self._alerts
            and self._alerts[alert_id].campaign_id == clean_campaign
            and self._alerts[alert_id].status == "open"
        ][:32]

    def circuit_snapshot(self, campaign_id: str) -> list[dict[str, object]]:
        clean_campaign = str(campaign_id or "").strip()
        now = time.monotonic()
        result: list[dict[str, object]] = []
        for (stored_campaign, tool_name), circuit in list(self._circuits.items()):
            if now >= circuit.reopen_after_monotonic:
                self._circuits.pop((stored_campaign, tool_name), None)
                self._clear_failures(stored_campaign, tool_name)
                continue
            if stored_campaign != clean_campaign:
                continue
            result.append(
                {
                    "tool_name": tool_name,
                    "error_code": circuit.error_code,
                    "failures": circuit.failures,
                    "remaining_seconds": max(
                        1,
                        int(circuit.reopen_after_monotonic - now),
                    ),
                    "last_message": circuit.last_message,
                }
            )
        return result

    def audit_payload(self, campaign_id: str) -> dict[str, object]:
        clean_campaign = str(campaign_id or "").strip()
        recent = [
            asdict(self._alerts[alert_id])
            for alert_id in reversed(self._alert_order)
            if alert_id in self._alerts
            and self._alerts[alert_id].campaign_id == clean_campaign
        ][:40]
        return {
            "active_alerts": [
                item for item in recent if item.get("status") == "open"
            ],
            "recent_alerts": recent,
            "open_circuits": self.circuit_snapshot(clean_campaign),
        }

    def _emit(
        self,
        campaign_id: str,
        *,
        code: str,
        severity: str,
        component: str,
        summary: str,
        suggested_domains: list[str],
        tool_hints: list[str],
    ) -> GMSupervisorAlert:
        dedupe_key = f"{campaign_id}:{code}"
        now = datetime.now(timezone.utc).isoformat()
        existing = self._alerts.get(dedupe_key)
        if existing is not None:
            existing.status = "open"
            existing.severity = severity
            existing.component = component
            existing.summary = summary
            existing.suggested_domains = list(suggested_domains)
            existing.tool_hints = list(tool_hints)
            existing.last_seen_at = now
            existing.occurrences += 1
            self._alert_order = [
                alert_id
                for alert_id in self._alert_order
                if alert_id != dedupe_key
            ]
            self._alert_order.append(dedupe_key)
            return existing
        self._event_counter += 1
        alert = GMSupervisorAlert(
            alert_id=dedupe_key,
            campaign_id=campaign_id,
            code=code,
            severity=severity,
            component=component,
            summary=summary,
            suggested_domains=list(suggested_domains),
            tool_hints=list(tool_hints),
            created_at=now,
            last_seen_at=now,
        )
        self._alerts[dedupe_key] = alert
        self._alert_order.append(dedupe_key)
        if len(self._alert_order) > self.max_events:
            expired = self._alert_order.pop(0)
            self._alerts.pop(expired, None)
        return alert

    def _clear_failures(self, campaign_id: str, tool_name: str) -> None:
        for key in list(self._failure_runs):
            if key[0] == campaign_id and key[1] == tool_name:
                self._failure_runs.pop(key, None)
        self._circuits.pop((campaign_id, tool_name), None)
        alert = self._alerts.get(
            f"{campaign_id}:TOOL_CIRCUIT_OPEN:{tool_name}"
        )
        if alert is not None and alert.status == "open":
            alert.status = "resolved"

    def _resolve_cleared_snapshot_alerts(
        self,
        campaign_id: str,
        seen_codes: set[str],
    ) -> None:
        for alert in self._alerts.values():
            if (
                alert.campaign_id == campaign_id
                and alert.status == "open"
                and alert.code in self._SNAPSHOT_ALERT_CODES
                and alert.code not in seen_codes
            ):
                alert.status = "resolved"

    @staticmethod
    def _never_circuit(tool_name: str) -> bool:
        return tool_name in {
            GMCapabilityBroker.DISCOVERY_TOOL,
            GMCapabilityBroker.SUPERVISOR_READ_TOOL,
            "record_safety_boundary",
            "save_campaign",
        }

    @property
    def SUPERVISOR_READ_TOOL(self) -> str:
        return GMCapabilityBroker.SUPERVISOR_READ_TOOL


class GMSupervisorStateCompressor:
    """Build the small private dashboard supplied to the GM each iteration."""

    _BASE_ADVENTURE_SECTIONS = {
        "scene",
            "runtime",
            "processes",
    }
    _SETUP_SECTIONS = {
        "session_zero",
        "map",
        "processes",
        "runtime",
        "references",
    }
    _DOMAIN_SECTIONS = {
        "campaign": set(),
        "table": {"runtime"},
        "session_zero": {"session_zero", "gameplay"},
        "scene": {"scene", "runtime"},
        "clock": {"clocks", "scene"},
        "npc": {"npcs", "scene"},
        "rules": {"gameplay", "scene", "clocks"},
        "conflict": {
            "gameplay",
            "runtime",
            "npcs",
            "clocks",
        },
        "map": {"map"},
        "travel": {"adventure", "scene"},
        "dungeon": {"dungeon", "scene", "clocks"},
        "reward": {"adventure", "gameplay", "references"},
        "supervisor": {"processes", "runtime"},
    }

    @classmethod
    def compress(
        cls,
        state: dict[str, object],
        *,
        context: GMToolExecutionContext,
        supervisor: dict[str, object],
        capability_catalog: list[dict[str, object]],
    ) -> dict[str, object]:
        setup_phase = context.gate_status in {
            "pre_session",
            "session_zero",
        }
        system_beat = bool(
            context.metadata.get("system_gm_beat_request")
        )
        granted_domains: set[str] = set()
        if setup_phase:
            sections = set(cls._SETUP_SECTIONS)
        elif system_beat:
            sections = set(cls._BASE_ADVENTURE_SECTIONS)
            for row in capability_catalog:
                domain = str(row.get("domain") or "").strip()
                sections.update(cls._DOMAIN_SECTIONS.get(domain, set()))
        else:
            sections = set(cls._BASE_ADVENTURE_SECTIONS)
            granted_domains = set(
                GMCapabilityBroker.domains_for_tools(
                    GMCapabilityBroker.granted_tool_names(context)
                )
            )
            for domain in granted_domains:
                sections.update(cls._DOMAIN_SECTIONS.get(domain, set()))
        inspection_focus = bool(context.metadata.get("inspection_focus"))
        if inspection_focus:
            sections = sections | {"session_zero", "map"}

        top_level_keys = {
            "current_campaign_id",
            "message_campaign_id",
            "inspection_focus",
            "gate_status",
        }
        if setup_phase or inspection_focus or "campaign" in granted_domains:
            top_level_keys.add("campaigns")
        if (
            setup_phase
            or inspection_focus
            or "session_zero" in granted_domains
        ):
            top_level_keys.add("hero_drafts")
        result = {
            key: cls._bounded(value)
            for key, value in state.items()
            if key in top_level_keys
        }
        detailed_scene = bool(
            system_beat
            or granted_domains
            & {
                "scene",
                "clock",
                "npc",
                "rules",
                "conflict",
                "travel",
                "dungeon",
            }
        )
        for section in sections:
            if section in state:
                value = state[section]
                if section == "scene" and not detailed_scene:
                    value = cls._compact_scene(value)
                result[section] = cls._bounded(value)
        result["supervisor"] = {
            **cls._bounded(supervisor),
            "capability_catalog": capability_catalog,
            "usage": (
                "processes是各组件的紧凑控制面，只用于判断当前由谁接手以及是否需要干预；"
                "processes.attention只列义务与等待项，不授权代替玩家选择；"
                "需要细节时再调用对应读取工具。需要当前未开放的能力时，"
                "先调用discover_capabilities，通常一次只选1到2个domain；"
                "只有同一条玩家消息确实需要跨域事务时才增加；"
                "不要要求玩家改用命令。告警是GM私有信息，不得原样发给玩家。"
            ),
        }
        return result

    @staticmethod
    def _compact_scene(value: object) -> object:
        if not isinstance(value, dict):
            return value
        keys = {
            "active",
            "frame_active",
            "scene_id",
            "frame_source_scene_id",
            "name",
            "location",
            "participants",
            "participant_locations",
            "participant_positions",
            "participant_activities",
            "suspended_scenes",
            "objective",
            "current_pressure",
            "public_facts",
            "revealed_clues",
            "recent_beats",
            "unresolved_requests",
            "visible_elements",
            "npc_functions",
            "pending_npc_questions",
            "open_conditions",
            "pending_npc_commitments",
            "settled_exchanges",
            "private_situation",
        }
        return {
            key: item
            for key, item in value.items()
            if key in keys
        }

    @classmethod
    def _bounded(
        cls,
        value: object,
        *,
        depth: int = 0,
    ) -> object:
        if depth >= 7:
            if isinstance(value, (dict, list, tuple)):
                return "…"
            return str(value)[:240]
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, item in list(value.items())[:80]:
                result[str(key)] = cls._bounded(item, depth=depth + 1)
            if len(value) > 80:
                result["_omitted"] = len(value) - 80
            return result
        if isinstance(value, (list, tuple)):
            limit = 24 if depth <= 2 else 12
            result = [
                cls._bounded(item, depth=depth + 1)
                for item in list(value)[-limit:]
            ]
            if len(value) > limit:
                result.insert(0, {"_earlier_omitted": len(value) - limit})
            return result
        if isinstance(value, str):
            return value if len(value) <= 600 else value[:600] + "…"
        return value
