from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import (
    CampaignStateTransaction,
)
from fu_gm.components.gm_tool_pacing_observer import GMToolPacingObserver
from fu_gm.components.table_working_brief import TableWorkingBriefManager
from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_supervisor import (
    GMCapabilityBroker,
    GMSupervisorStateCompressor,
)
from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolFreshnessGuard,
    json_safe_value,
)

SETUP_PROGRESS_TOOL_NAMES = frozenset(
    {
        "commit_session_zero_update",
        "record_prologue_setup_answer",
        "confirm_session_zero_proposal",
        "mark_session_zero_topic_complete",
        "update_hero_draft",
        "confirm_hero_draft",
        "record_safety_boundary",
        "place_world_map_locations",
        "edit_world_map",
        "generate_world_map_preview",
    }
)


class GMAgentMessageHost(Protocol):
    gm_tool_agent: Any
    gm_campaign_tools: Any
    gm_session_zero_tools: Any
    gm_scene_tools: Any
    gm_clock_tools: Any
    gm_dice_tools: Any
    gm_npc_tools: Any
    gm_gameplay_tools: Any
    gm_map_tools: Any
    gm_runtime_tools: Any
    gm_adventure_tools: Any
    gm_dungeon_tools: Any
    gm_reference_tools: Any
    gm_tool_registry: Any
    gm_supervisor: Any
    reply_ledger: Any
    session_gates: Any

    def _message_fields(self, payload: dict[str, Any]) -> tuple[str, str, str, str, str]: ...

    def _external_message_metadata(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    def _adventure_readiness_snapshot(
        self,
        runtime: Any,
        *,
        materialize_confirmed_characters: bool = False,
    ) -> dict[str, Any]: ...

    @staticmethod
    def _player_character_control_map(runtime: Any) -> dict[str, list[str]]: ...

    @staticmethod
    def _truthy(value: object) -> bool: ...


class GMToolStateSnapshotBuilder:
    """Build the authoritative observation supplied to one GM-agent cycle."""

    _SECTIONS = (
        ("session_zero", "gm_session_zero_tools"),
        ("scene", "gm_scene_tools"),
        ("clocks", "gm_clock_tools"),
        ("npcs", "gm_npc_tools"),
        ("gameplay", "gm_gameplay_tools"),
        ("map", "gm_map_tools"),
        ("runtime", "gm_runtime_tools"),
        ("adventure", "gm_adventure_tools"),
        ("dungeon", "gm_dungeon_tools"),
        ("references", "gm_reference_tools"),
    )

    def __init__(self, host: GMAgentMessageHost) -> None:
        self.host = host

    def build(self, context: GMToolExecutionContext) -> dict[str, object]:
        state = self.build_full(context)
        self._grant_active_decision_capabilities(context, state)
        phase_tools = set(
            GMToolAgentCapabilityPolicy.phase_tool_names(
                self.host.gm_tool_registry,
                context,
            )
            or set()
        )
        catalog = GMCapabilityBroker.catalog(
            self.host.gm_tool_registry,
            context,
            phase_tools=phase_tools,
        )
        self.host.gm_supervisor.scan(context, state)
        return GMSupervisorStateCompressor.compress(
            state,
            context=context,
            supervisor=self.host.gm_supervisor.audit_payload(
                context.campaign_id
            ),
            capability_catalog=catalog,
        )

    @staticmethod
    def _grant_active_decision_capabilities(
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> None:
        """Expose the resolver when this speaker owns a blocking choice."""

        gameplay = dict(state.get("gameplay") or {})
        controlled = {
            str(name or "").strip()
            for name in list(gameplay.get("controlled_characters") or [])
            if str(name or "").strip()
        }
        turn_participants = dict(state.get("turn_participants") or {})
        turn_speakers = {
            str(name or "").strip()
            for name in list(turn_participants.get("speakers") or [])
            if str(name or "").strip()
        }
        turn_controls = {
            str(character or "").strip()
            for names in dict(
                turn_participants.get("controlled_characters_by_speaker") or {}
            ).values()
            if isinstance(names, list)
            for character in names
            if str(character or "").strip()
        }
        decisions = dict(dict(state.get("processes") or {}).get("decisions") or {})
        for pending in list(decisions.get("pending") or []):
            if not isinstance(pending, dict) or not bool(pending.get("blocking")):
                continue
            owner = str(pending.get("owner") or "").strip()
            allowed_speakers = {
                str(name or "").strip()
                for name in list(pending.get("allowed_speakers") or [])
                if str(name or "").strip()
            }
            if (
                owner in controlled
                or owner in turn_controls
                or owner == context.speaker
                or context.speaker in allowed_speakers
                or bool(turn_speakers & allowed_speakers)
            ):
                GMCapabilityBroker.grant(
                    context,
                    {"get_gameplay_state", "resolve_rule_window"},
                )
                return

    def build_full(
        self,
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        """Collect all domain observations before making a bounded model view."""

        state = dict(self.host.gm_campaign_tools.state_summary(context))
        for section, attribute in self._SECTIONS:
            service = getattr(self.host, attribute)
            if section == "dungeon":
                state[section] = service.get_dungeon_state(
                    context,
                    {},
                ).result
            else:
                state[section] = service.state_summary(context)
        state["turn_participants"] = self._turn_participant_state(context)
        state["processes"] = self._process_state(context, state)
        return json_safe_value(state)

    def _turn_participant_state(
        self,
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        controls = self.host._player_character_control_map(runtime)
        raw_events = context.metadata.get("current_turn_events")
        events = (
            [item for item in raw_events if isinstance(item, dict)]
            if isinstance(raw_events, list)
            else []
        )
        speakers = list(
            dict.fromkeys(
                str(item.get("speaker") or "").strip()
                for item in events
                if str(item.get("speaker") or "").strip()
            )
        )
        if context.speaker and context.speaker not in speakers:
            speakers.append(context.speaker)
        return {
            "speakers": speakers,
            "controlled_characters_by_speaker": {
                speaker: list(controls.get(speaker, []))
                for speaker in speakers
            },
        }

    def _process_state(
        self,
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> dict[str, object]:
        """Build a compact control-plane view without exposing every component."""

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        frame = app.scene_frame_manager.current_frame
        conflict = app.conflict_manager.state
        windows = list(app.interceptor.decision_window_manager.pending())
        public_windows = {
            str(item.get("window_id") or ""): item
            for item in app.interceptor.decision_window_manager.public_summary()
            if isinstance(item, dict) and str(item.get("window_id") or "")
        }
        clocks = list(app.clock_manager.all())
        pressure_types = {"threat", "villain", "dungeon", "boss"}
        pressure_budget = dict(
            dict(state.get("clocks") or {}).get("pacing_budget") or {}
        )
        journey = app.travel_manager.active_journey
        pending_travel = app.travel_manager.pending_travel_event()
        dungeon = app.dungeon_manager.state
        ledger = app.session_ledger
        arc_state = app.story_arc_manager.state
        episode = arc_state.current_session_progress
        pacing_plan = arc_state.current_pacing_plan
        contract = pacing_plan.dramatic_contract
        active_scene_progress = episode.scene_progress.get(
            str(episode.active_scene_id or "").strip()
        )
        known_frames = [
            *list(getattr(app.scene_frame_manager, "history", []) or []),
            *list(
                getattr(
                    app.scene_frame_manager,
                    "suspended_frames",
                    {},
                ).values()
            ),
        ]
        if frame is not None:
            known_frames.append(frame)
        used_opportunity_keys = {
            str(getattr(item, "session_opportunity_key", "") or "").strip()
            for item in known_frames
            if str(getattr(item, "session_opportunity_key", "") or "").strip()
        }
        unused_opportunities = [
            item
            for item in list(contract.potential_scenes or [])
            if str(item.scene_key or "").strip() not in used_opportunity_keys
        ]
        map_state = dict(state.get("map") or {})
        action_round = app.scene_manager.action_round_snapshot()
        pending_npc_questions = list(
            app.npc_response_windows.pending(frame)
        )
        pending_npc_commitments = list(
            app.scene_frame_manager.npc_deferred_commitment_manager.pending(
                frame
            )
        )
        due_npc_conditions = [
            item
            for item in list(getattr(frame, "open_conditions", []) or [])
            if str(item.get("status") or "open") == "open"
            and str(item.get("player_fulfillment") or "pending")
            == "fulfilled"
        ]

        result = {
            "session": {
                "gate_status": context.gate_status,
                "ledger_active": bool(ledger.active),
                "ledger_settled": bool(ledger.settled),
                "ledger_session_id": str(ledger.session_id or ""),
                "participating_pcs": sorted(ledger.participating_pcs),
                "session_number": int(episode.session_number or 0),
                "stage": str(episode.stage or ""),
                "meaningful_turns": int(episode.meaningful_turns or 0),
                "closure_ready": bool(episode.closure_ready),
                "expected_table_turns": list(
                    pacing_plan.expected_table_turns
                ),
                "scene_lifecycle": {
                    "current_opportunity": {
                        "key": str(
                            getattr(frame, "session_opportunity_key", "")
                            or ""
                        ),
                        "role": str(
                            getattr(frame, "session_opportunity_role", "")
                            or ""
                        ),
                        "title": str(
                            getattr(frame, "session_opportunity_title", "")
                            or ""
                        ),
                        "purpose": str(
                            getattr(frame, "session_opportunity_purpose", "")
                            or ""
                        ),
                    },
                    "current_scene_progress": {
                        "player_actions": int(
                            getattr(active_scene_progress, "player_actions", 0)
                            or 0
                        ),
                        "material_changes": int(
                            getattr(active_scene_progress, "material_changes", 0)
                            or 0
                        ),
                        "has_local_outcome": bool(
                            getattr(active_scene_progress, "has_local_outcome", False)
                        ),
                        "substantial": bool(
                            getattr(active_scene_progress, "substantial", False)
                        ),
                    },
                    "used_opportunity_keys": sorted(used_opportunity_keys),
                    "unused_opportunities": [
                        {
                            "key": str(item.scene_key or ""),
                            "role": str(item.scene_role or ""),
                            "title": str(item.title or ""),
                            "location": str(item.location or ""),
                            "purpose": str(item.purpose or ""),
                            "entry_points": list(item.entry_points[:2]),
                        }
                        for item in unused_opportunities[:5]
                    ],
                    "usage": (
                        "这些是可舍弃、换序的GM私有局面机会，不是固定剧情。"
                        "玩家真正离开当前地点、转向另一处目标，或当前局部问题已经落地时，"
                        "应使用移动或场景切换工具提交新地点；不能只在叙事中声称已经抵达。"
                    ),
                },
            },
            "scene": {
                "authoritative_active": scene is not None,
                "scene_id": str(getattr(scene, "scene_id", "") or ""),
                "scene_type": self._enum_value(
                    getattr(scene, "scene_type", "")
                ),
                "frame_active": frame is not None,
                "frame_scene_id": str(
                    getattr(frame, "source_scene_id", "") or ""
                ),
                "action_round": action_round,
                "suspended_scene_ids": [
                    str(item.scene_id or "")
                    for item in app.scene_manager.suspended_scenes
                    if str(item.scene_id or "")
                ],
            },
            "conflict": {
                "active": bool(conflict.active),
                "round": int(conflict.round_number or 0),
                "current_actor": str(conflict.current_actor() or ""),
                "turn_order": list(conflict.turn_order),
                "current_turn_index": self._safe_int(
                    conflict.current_turn_index
                ),
                "turn_started_actor": str(
                    conflict.turn_started_actor or ""
                ),
                "pending_turn_end_actor": str(
                    conflict.pending_turn_end_actor or ""
                ),
                "current_bonus_actor": str(
                    conflict.current_bonus_actor or ""
                ),
                "queued_turns": list(conflict.queued_turns),
                "queued_turn_kinds": list(
                    conflict.queued_turn_kinds
                ),
                "turn_serial": self._safe_int(
                    conflict.turn_serial
                ),
                "acted_this_round": list(
                    conflict.acted_this_round
                ),
                "held_actions": [
                    {
                        "actor": str(item.get("actor") or ""),
                        "action_type": str(
                            item.get("action_type") or ""
                        ),
                        "window_id": str(
                            item.get("window_id") or ""
                        ),
                        "round_number": self._safe_int(
                            item.get("round_number")
                        ),
                    }
                    for item in list(conflict.held_actions or [])
                    if isinstance(item, dict)
                ],
            },
            "decisions": {
                "pending_count": len(windows),
                "blocking_count": sum(
                    1 for window in windows if window.blocking
                ),
                "pending": [
                    {
                        "window_id": window.window_id,
                        "kind": window.kind,
                        "owner": window.owner,
                        "blocking": bool(window.blocking),
                        "allowed_responders": list(
                            window.allowed_responders
                        ),
                        "allowed_speakers": list(
                            public_windows.get(window.window_id, {}).get(
                                "allowed_speakers",
                                [],
                            )
                        ),
                        "scope_kind": window.scope_kind,
                        "scope_id": window.scope_id,
                        "transaction_id": window.transaction_id,
                        "resume_point": window.resume_point,
                        "action_type": window.action_type,
                        "deferred_turn_actor": str(
                            window.payload.get(
                                "deferred_turn_actor"
                            )
                            or ""
                        ),
                        "deferred_turn_serial": self._safe_int(
                            window.payload.get(
                                "deferred_turn_serial"
                            )
                        ),
                    }
                    for window in windows
                ],
            },
            "npc_interactions": {
                "pending_question_count": len(pending_npc_questions),
                "pending_questions": [
                    {
                        "question_id": str(
                            item.get("question_id") or ""
                        ),
                        "npc": str(item.get("npc") or ""),
                        "addressed_actor": str(
                            item.get("addressed_actor") or ""
                        ),
                        "remaining_item_count": len(
                            app.npc_response_windows.remaining_items(item)
                        ),
                    }
                    for item in pending_npc_questions
                ],
                "pending_commitment_count": len(
                    pending_npc_commitments
                ),
                "pending_commitments": [
                    {
                        "commitment_id": str(
                            item.get("commitment_id") or ""
                        ),
                        "npc": str(item.get("npc") or ""),
                        "trigger_status": str(
                            item.get("trigger_status") or ""
                        ),
                    }
                    for item in pending_npc_commitments
                ],
                "due_condition_count": len(due_npc_conditions),
                "due_conditions": [
                    {
                        "condition_id": str(
                            item.get("condition_id") or ""
                        ),
                        "npc": str(item.get("npc") or ""),
                    }
                    for item in due_npc_conditions
                ],
            },
            "clocks": {
                "active_count": len(clocks),
                "foreground_pressure_names": [
                    clock.name
                    for clock in clocks
                    if clock.clock_type in pressure_types
                    and clock.visibility == "foreground"
                ],
                "auto_advance_names": [
                    clock.name
                    for clock in clocks
                    if bool(clock.auto_advance)
                    and clock.clock_type in pressure_types
                ],
                "scene_scoped": [
                    {
                        "name": clock.name,
                        "scene_id": clock.scene_id,
                        "status": clock.status,
                    }
                    for clock in clocks
                    if clock.scope == "scene"
                ],
                "pacing_budget": pressure_budget,
            },
            "travel": {
                "active": journey is not None,
                "journey_id": str(
                    getattr(journey, "journey_id", "") or ""
                ),
                "origin": str(getattr(journey, "origin", "") or ""),
                "destination": str(
                    getattr(journey, "destination", "") or ""
                ),
                "status": str(getattr(journey, "status", "") or ""),
                "current_day": int(
                    getattr(journey, "current_day", 0) or 0
                ),
                "total_days": int(
                    getattr(journey, "total_days", 0) or 0
                ),
                "resolved_day_numbers": [
                    int(getattr(item, "day", 0) or 0)
                    for item in list(
                        getattr(journey, "day_results", []) or []
                    )
                    if int(getattr(item, "day", 0) or 0) > 0
                ],
                "pending_event": pending_travel is not None,
                "pending_event_day": int(
                    getattr(journey, "pending_event_day", 0) or 0
                ),
                "pending_event_type": self._enum_value(
                    getattr(pending_travel, "event_type", "")
                ),
                "pending_event_tags": [
                    str(item or "")
                    for item in list(
                        getattr(pending_travel, "danger_tags", []) or []
                    )
                    if str(item or "")
                ],
                "suspended_by_dungeon": bool(
                    journey is not None
                    and pending_travel is not None
                    and dungeon.active
                ),
            },
            "dungeon": {
                "active": bool(dungeon.active),
                "name": str(dungeon.name or ""),
                "location": str(dungeon.location or ""),
                "current_area": str(dungeon.current_area or ""),
                "area_names": [
                    str(area.name or "")
                    for area in list(dungeon.areas or [])
                    if str(area.name or "")
                ],
                "danger_clock_names": [
                    str(name or "")
                    for name in list(dungeon.danger_clocks or [])
                    if str(name or "")
                ],
                "missing_danger_clock_names": [
                    str(name or "")
                    for name in list(dungeon.danger_clocks or [])
                    if str(name or "")
                    and not app.clock_manager.exists(str(name))
                ],
                "completion_status": str(
                    dungeon.completion_status or ""
                ),
            },
            "rituals": [
                {
                    "clock_name": clock_name,
                    "name": plan.name,
                    "caster": plan.caster,
                    "caster_exists": app.character_manager.exists(
                        plan.caster
                    ),
                    "scene_id": plan.scene_id,
                    "ready_turn_serial": int(
                        plan.ready_turn_serial or 0
                    ),
                    "clock_exists": app.clock_manager.exists(clock_name),
                    "clock_current": (
                        int(app.clock_manager.get(clock_name).current or 0)
                        if app.clock_manager.exists(clock_name)
                        else 0
                    ),
                    "clock_max_segments": (
                        int(
                            app.clock_manager.get(
                                clock_name
                            ).max_segments
                            or 0
                        )
                        if app.clock_manager.exists(clock_name)
                        else 0
                    ),
                    "clock_status": (
                        str(
                            app.clock_manager.get(clock_name).status
                            or ""
                        )
                        if app.clock_manager.exists(clock_name)
                        else ""
                    ),
                    "ready": bool(
                        app.clock_manager.exists(clock_name)
                        and (
                            app.clock_manager.get(clock_name).status
                            == "ready"
                            or app.clock_manager.get(clock_name).current
                            >= app.clock_manager.get(
                                clock_name
                            ).max_segments
                        )
                    ),
                }
                for clock_name, plan in app.ritual_manager.active_rituals.items()
            ],
            "projects": [
                {
                    "name": project.name,
                    "inventor": project.inventor,
                    "current_progress": int(
                        project.current_progress or 0
                    ),
                    "required_progress": int(
                        project.required_progress or 0
                    ),
                    "completed": bool(project.completed),
                    "persisted": bool(project.persisted),
                    "created_asset_id": str(
                        project.created_asset_id or ""
                    ),
                    "output_type": self._enum_value(
                        project.output_type
                    ),
                    "owner": str(project.owner or project.inventor or ""),
                }
                for project in app.project_manager.projects.values()
            ],
            "map": {
                "has_foundation": bool(
                    map_state.get("has_map_foundation")
                ),
                "map_name": str(map_state.get("map_name") or ""),
                "status": str(map_state.get("status") or ""),
                "current_artifact": bool(
                    map_state.get("current_map_available")
                ),
                "stale_artifact": bool(
                    map_state.get("stale_map_available")
                ),
                "semantic_revision": int(
                    dict(map_state.get("semantic_layout") or {}).get(
                        "revision"
                    )
                    or 0
                ),
            },
            "progression": [
                {
                    "name": character.name,
                    "level": int(character.level or 0),
                    "experience_points": int(
                        character.experience_points or 0
                    ),
                    "can_level_up": bool(
                        app.progression_manager.can_level_up(
                            character.name
                        )
                    ),
                }
                for character in app.character_manager.all()
                if "pc" in character.traits
            ],
        }
        result["attention"] = self._attention_items(result)
        return result

    @staticmethod
    def _attention_items(
        processes: dict[str, object],
    ) -> list[dict[str, object]]:
        """Summarize process obligations without deciding how to resolve them."""

        attention: list[dict[str, object]] = []
        session = dict(processes.get("session") or {})
        conflict = dict(processes.get("conflict") or {})
        decisions = dict(processes.get("decisions") or {})
        npc = dict(processes.get("npc_interactions") or {})
        travel = dict(processes.get("travel") or {})
        dungeon = dict(processes.get("dungeon") or {})
        rituals = [
            item
            for item in list(processes.get("rituals") or [])
            if isinstance(item, dict)
        ]
        map_state = dict(processes.get("map") or {})

        if int(decisions.get("blocking_count") or 0) > 0:
            attention.append(
                {
                    "kind": "blocking_decision",
                    "priority": "required",
                    "count": int(decisions.get("blocking_count") or 0),
                }
            )
        current_actor = str(conflict.get("current_actor") or "").strip()
        if bool(conflict.get("active")) and current_actor:
            attention.append(
                {
                    "kind": "conflict_turn",
                    "priority": "required",
                    "actor": current_actor,
                }
            )
        if int(npc.get("due_condition_count") or 0) > 0:
            attention.append(
                {
                    "kind": "npc_promise_due",
                    "priority": "required",
                    "count": int(npc.get("due_condition_count") or 0),
                }
            )
        if int(npc.get("pending_question_count") or 0) > 0:
            attention.append(
                {
                    "kind": "npc_waiting_for_player",
                    "priority": "wait",
                    "count": int(npc.get("pending_question_count") or 0),
                }
            )
        if bool(travel.get("pending_event")):
            attention.append(
                {
                    "kind": "travel_event_pending",
                    "priority": (
                        "suspended"
                        if bool(
                            travel.get("suspended_by_dungeon")
                            and dungeon.get("active")
                        )
                        else "required"
                    ),
                    "day": int(travel.get("pending_event_day") or 0),
                }
            )
        ready_rituals = [
            str(item.get("name") or item.get("clock_name") or "")
            for item in rituals
            if bool(item.get("ready"))
        ]
        if ready_rituals:
            attention.append(
                {
                    "kind": "ritual_ready",
                    "priority": "required",
                    "names": ready_rituals,
                }
            )
        if bool(session.get("closure_ready")):
            attention.append(
                {
                    "kind": "session_closure_ready",
                    "priority": "advisory",
                }
            )
        if bool(map_state.get("stale_artifact")):
            attention.append(
                {
                    "kind": "map_artifact_stale",
                    "priority": "advisory",
                }
            )
        level_ups = [
            str(item.get("name") or "")
            for item in list(processes.get("progression") or [])
            if isinstance(item, dict) and bool(item.get("can_level_up"))
        ]
        if level_ups:
            attention.append(
                {
                    "kind": "level_up_available",
                    "priority": "advisory",
                    "names": level_ups,
                }
            )
        return attention[:12]

    @staticmethod
    def _enum_value(value: object) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return int(default)


class GMAgentMessageCoordinator:
    """Own one natural-language GM-agent transaction from context to receipt.

    HTTP and AstrBot provide trusted envelope metadata. This coordinator passes
    the untouched current message plus authoritative state to the core GM
    agent, executes its typed tools, and publishes only tool-backed results.
    It deliberately contains no prose keyword router.
    """

    def __init__(
        self,
        host: GMAgentMessageHost,
        *,
        state_builder: GMToolStateSnapshotBuilder | None = None,
        pacing_observer: GMToolPacingObserver | None = None,
        working_brief_manager: TableWorkingBriefManager | None = None,
    ) -> None:
        self.host = host
        self.state_builder = state_builder or GMToolStateSnapshotBuilder(host)
        self.pacing_observer = pacing_observer or GMToolPacingObserver()
        self.working_brief_manager = (
            working_brief_manager or TableWorkingBriefManager()
        )
        self._inspection_focuses: dict[tuple[str, str], dict[str, object]] = {}

    def handle(
        self,
        payload: dict[str, Any],
        *,
        gate: Any,
        is_private: bool,
        explicitly_addressed: bool,
        recent_context: str,
        freshness_guard: GMToolFreshnessGuard | None = None,
        side_effect_lock: Any | None = None,
        record_log: bool = True,
    ) -> dict[str, Any] | None:
        agent = self.host.gm_tool_agent
        if agent is None:
            return None
        campaign_id, session_id, speaker, message, channel_id = self.host._message_fields(payload)
        # Slash commands use dedicated protocol endpoints, not this natural
        # language transaction.
        if str(message or "").lstrip().startswith("/"):
            return None

        runtime = self.host._runtime(campaign_id)
        request_metadata = self._request_metadata(
            payload,
            message=message,
            recent_context=recent_context,
        )
        request_metadata["recent_public_messages"] = self._recent_public_messages(
            runtime,
            campaign_id,
            session_id,
        )
        request_metadata["recent_message_delivery_context"] = (
            self._recent_message_delivery_context(
                campaign_id,
                session_id,
                channel_id,
                current_message_id=str(payload.get("message_id") or ""),
            )
        )
        request_metadata["gm_dynamic_capabilities_enabled"] = True
        inspection_focus = self._inspection_focus(session_id, channel_id)
        if inspection_focus:
            request_metadata["inspection_focus"] = inspection_focus
        context = GMToolExecutionContext(
            campaign_id=campaign_id,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            gate_status=gate.status,
            is_private=is_private,
            directly_addressed=bool(explicitly_addressed),
            metadata=request_metadata,
        )
        outcome = agent.run(
            message,
            recent_context=recent_context,
            context=context,
            state_summary=self.state_builder.build(context),
            state_summary_provider=lambda: self.state_builder.build(context),
            freshness_guard=freshness_guard,
            side_effect_lock=side_effect_lock,
        )
        supervisor_observation = self.host.gm_supervisor.observe_receipts(
            context,
            outcome.receipts,
        )
        if not outcome.handled:
            active_table = str(getattr(gate, "status", "") or "") != "inactive"
            must_reply = bool(explicitly_addressed or is_private)
            outcome.target = (
                "fu_gm"
                if must_reply
                else "silent"
                if active_table
                else "external"
            )
            outcome.reply = (
                "当前主持模型没有完成这次处理，战役状态没有改变。请稍后重试。"
                if must_reply
                else ""
            )
            outcome.reply_parts = []
            outcome.mode = "gm_agent_fail_closed"
            outcome.reason = (
                "核心 GM 事务失败；没有执行工具，也没有进入关键词回退。"
            )
            outcome.stop_astrbot = bool(active_table or must_reply)
            outcome.handled = True

        setup_progressed = gate.status in {"pre_session", "session_zero"} and any(
            receipt.ok
            and receipt.state_changed
            and receipt.tool_name in SETUP_PROGRESS_TOOL_NAMES
            for receipt in outcome.receipts
        )
        if setup_progressed:
            with runtime.transaction_lock:
                setup_state_changed = (
                    runtime.app.session_zero_manager
                    .resume_proactive_nudges_after_setup_progress()
                )
                readiness = self.host._adventure_readiness_snapshot(
                    runtime,
                    materialize_confirmed_characters=False,
                )
                if not bool(readiness.get("ready")):
                    setup_state_changed = (
                        runtime.app.session_zero_manager
                        .clear_chapter_one_transition()
                        or setup_state_changed
                    )
                if setup_state_changed:
                    self.host._autosave_campaign(runtime, campaign_id)

        self._update_inspection_focus(
            session_id,
            channel_id,
            outcome.receipts,
        )
        receipts = [receipt.to_dict() for receipt in outcome.receipts]
        reply_media = self._reply_media(outcome.receipts)
        active_campaign_id = self._active_campaign_id(campaign_id, outcome.receipts)
        deleted_campaign_id = self._deleted_campaign_id(outcome.receipts)
        pacing_runtime = (
            runtime
            if active_campaign_id == campaign_id
            else self.host._runtime(active_campaign_id)
        )
        pacing_observation = self._observe_and_persist_pacing(
            pacing_runtime,
            active_campaign_id,
            context,
            outcome.receipts,
        )
        working_brief_observation = self._observe_and_persist_working_brief(
            pacing_runtime,
            active_campaign_id,
            source_campaign_id=campaign_id,
            context=context,
            outcome=outcome,
        )
        authoritative_gate = self.host.session_gates.get(
            active_campaign_id,
            channel_id,
            session_id,
        )
        metadata = {
            **self.host._external_message_metadata(payload),
            "current_turn_events": list(
                request_metadata.get("current_turn_events") or []
            ),
            "conversation_turn_id": str(
                request_metadata.get("conversation_turn_id") or ""
            ),
            "mode": outcome.mode,
            "agent_trace": list(outcome.trace),
            "tool_receipts": receipts,
            "state_changed": outcome.state_changed,
            "agent_error": outcome.error,
            "active_campaign_id": active_campaign_id,
            "deleted_campaign_id": deleted_campaign_id,
            "agent_target": outcome.target,
            "agent_reason": outcome.reason,
            "agent_terminal_action": outcome.terminal_action,
            "reply_parts": list(outcome.reply_parts),
            "delivery_intent": outcome.delivery.to_dict(),
            "pacing_observation": pacing_observation,
            "working_brief_observation": working_brief_observation,
            "supervisor_observation": supervisor_observation,
        }
        audit_runtime = pacing_runtime if deleted_campaign_id == campaign_id else runtime
        audit_campaign_id = (
            active_campaign_id
            if deleted_campaign_id == campaign_id
            else campaign_id
        )
        audit_log_error = self._append_audit_log(
            audit_runtime,
            campaign_id=audit_campaign_id,
            session_id=session_id,
            speaker=speaker,
            message=message,
            channel_id=channel_id,
            message_id=str(payload.get("message_id") or ""),
            outcome=outcome,
            metadata=metadata,
            record_log=record_log,
        )
        response = {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "active_campaign_id": active_campaign_id,
            "deleted_campaign_id": deleted_campaign_id,
            "target": outcome.target,
            "route": outcome.mode,
            "reply": outcome.reply,
            "reply_parts": list(outcome.reply_parts),
            "reply_media": reply_media,
            "delivery": outcome.delivery.to_dict(),
            "send_reply": outcome.target == "fu_gm"
            and bool(outcome.reply or outcome.reply_parts or reply_media),
            "stop_astrbot": outcome.stop_astrbot,
            "tool_receipts": receipts,
            "agent_trace": list(outcome.trace),
            "agent_error": outcome.error,
            "pacing_observation": pacing_observation,
            "working_brief_observation": working_brief_observation,
            "supervisor_observation": supervisor_observation,
            "decision": {
                "target": outcome.target,
                "mode": outcome.mode,
                "audience": (
                    "gm"
                    if outcome.target == "fu_gm"
                    else "players"
                    if outcome.target == "silent"
                    else "external"
                ),
                "reply_required": outcome.target == "fu_gm"
                and bool(outcome.reply or outcome.reply_parts or reply_media),
                "agent_action": outcome.terminal_action,
                "reason": outcome.reason
                or "时悠根据当前消息、桌面上下文和可用能力自主决定了处理方式。",
                "confidence": 1.0,
                "stop_astrbot": outcome.stop_astrbot,
                "tags": [outcome.mode, *[receipt.tool_name for receipt in outcome.receipts]],
            },
            "gate": asdict(authoritative_gate),
        }
        if audit_log_error:
            response["audit_log_error"] = audit_log_error
        return response

    def _recent_message_delivery_context(
        self,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        *,
        current_message_id: str,
    ) -> list[dict[str, object]]:
        """Expose trusted recent ids so the model never invents quote targets."""

        ledger = getattr(self.host, "reply_ledger", None)
        recent_events = getattr(ledger, "recent_events", None)
        if not callable(recent_events):
            return []
        return [
            {
                "message_id": str(event.message_id or ""),
                "speaker": str(event.speaker or ""),
                "speaker_id": str(event.speaker_id or ""),
                "text": str(event.text or "")[:300],
                "is_current": bool(
                    current_message_id
                    and str(event.message_id or "") == current_message_id
                ),
            }
            for event in recent_events(
                campaign_id,
                session_id,
                channel_id,
                limit=8,
            )
            if str(event.message_id or "").strip()
        ]

    @staticmethod
    def _recent_public_messages(
        runtime: Any,
        campaign_id: str,
        session_id: str,
        *,
        limit: int = 12,
    ) -> list[dict[str, object]]:
        """Expose chronological public turns without flattening attribution."""

        try:
            entries = runtime.log_manager.load_transcript(
                campaign_id,
                session_id,
            )
        except Exception:
            return []
        visible = [
            entry
            for entry in entries
            if str(getattr(entry, "role", "") or "")
            not in {"gm_private", "private", "system_private", "system"}
            and str(getattr(entry, "content", "") or "").strip()
        ]
        return [
            {
                "message_id": str(getattr(entry, "message_id", "") or ""),
                "speaker": str(getattr(entry, "speaker", "") or ""),
                "role": str(getattr(entry, "role", "") or ""),
                "text": str(getattr(entry, "content", "") or "")[:600],
                "created_at": str(getattr(entry, "created_at", "") or ""),
            }
            for entry in visible[-max(0, limit) :]
        ]

    def _observe_and_persist_pacing(
        self,
        runtime: Any,
        campaign_id: str,
        context: GMToolExecutionContext,
        receipts: list[Any],
    ) -> dict[str, object]:
        """Persist post-tool pacing evidence without endangering the turn.

        Domain tools have already committed their authoritative state by this
        point. Pacing is a separate derived write, so a failure rolls back only
        this observation rather than undoing a player action that was already
        delivered successfully.
        """

        with runtime.transaction_lock:
            snapshot = CampaignStateTransaction.capture(
                runtime.app,
                campaign_id,
            )
            previous_saved_path = str(
                getattr(runtime, "last_saved_path", "") or ""
            )
            try:
                observation = self.pacing_observer.observe(
                    runtime,
                    context,
                    receipts,
                )
                if not observation:
                    return {}
                saved_path = self.host._autosave_campaign(
                    runtime,
                    campaign_id,
                )
                return {
                    **observation,
                    "saved_path": saved_path,
                }
            except Exception as exc:
                CampaignStateTransaction.restore(runtime.app, snapshot)
                runtime.last_saved_path = previous_saved_path
                return {
                    "error": str(exc)[:300],
                    "rolled_back": True,
                }

    def _observe_and_persist_working_brief(
        self,
        runtime: Any,
        campaign_id: str,
        *,
        source_campaign_id: str,
        context: GMToolExecutionContext,
        outcome: Any,
    ) -> dict[str, object]:
        """Persist exact declarations and receipt-backed outcomes separately."""

        if campaign_id != source_campaign_id:
            return {}
        frame = runtime.app.scene_frame_manager.current_frame
        if frame is None:
            return {}
        with runtime.transaction_lock:
            snapshot = CampaignStateTransaction.capture(
                runtime.app,
                campaign_id,
            )
            previous_saved_path = str(
                getattr(runtime, "last_saved_path", "") or ""
            )
            try:
                observation = self.working_brief_manager.observe(
                    frame,
                    context,
                    outcome.receipts,
                    target=outcome.target,
                    public_reply=outcome.reply,
                )
                if not observation.get("changed"):
                    return observation
                saved_path = self.host._autosave_campaign(runtime, campaign_id)
                return {**observation, "saved_path": saved_path}
            except Exception as exc:
                CampaignStateTransaction.restore(runtime.app, snapshot)
                runtime.last_saved_path = previous_saved_path
                return {
                    "error": str(exc)[:300],
                    "rolled_back": True,
                }

    @staticmethod
    def _append_audit_log(
        runtime: Any,
        *,
        campaign_id: str,
        session_id: str,
        speaker: str,
        message: str,
        channel_id: str,
        message_id: str,
        outcome: Any,
        metadata: dict[str, Any],
        record_log: bool,
    ) -> str:
        if not record_log:
            return ""
        try:
            current_turn = [
                dict(item)
                for item in list(metadata.get("current_turn_events") or [])
                if isinstance(item, dict)
                and str(item.get("text") or "").strip()
            ]
            reply_parts = [
                str(item or "").strip()
                for item in list(getattr(outcome, "reply_parts", None) or [])
                if str(item or "").strip()
            ]
            if not reply_parts and str(outcome.reply or "").strip():
                reply_parts = [str(outcome.reply).strip()]
            if len(current_turn) > 1 or len(reply_parts) > 1:
                entry_metadata = dict(metadata)
                entry_metadata.pop("current_turn_events", None)
                role = (
                    "user"
                    if outcome.target == "fu_gm"
                    else "table_talk"
                    if outcome.target == "silent"
                    else "user"
                )
                source_messages = current_turn or [
                    {
                        "speaker": speaker,
                        "text": message,
                        "message_id": message_id,
                        "event_id": "",
                    }
                ]
                for item in source_messages:
                    runtime.log_manager.append_message(
                        campaign_id,
                        session_id,
                        speaker=str(item.get("speaker") or "玩家"),
                        content=str(item.get("text") or ""),
                        role=role,
                        channel_id=channel_id,
                        message_id=str(item.get("message_id") or ""),
                        metadata={
                            **entry_metadata,
                            "conversation_turn_id": str(
                                metadata.get("conversation_turn_id") or ""
                            ),
                            "source_event_id": str(item.get("event_id") or ""),
                        },
                    )
                if outcome.target == "fu_gm":
                    for index, part in enumerate(reply_parts, start=1):
                        runtime.log_manager.append_message(
                            campaign_id,
                            session_id,
                            speaker="AI GM",
                            content=part,
                            role="assistant",
                            channel_id=channel_id,
                            message_id=(
                                f"fu-gm-reply:{message_id}:{index}"
                                if message_id
                                else ""
                            ),
                            metadata={
                                **entry_metadata,
                                "reply_part_index": index,
                                "reply_part_count": len(reply_parts),
                            },
                        )
            elif outcome.target == "fu_gm":
                runtime.log_manager.append_turn(
                    campaign_id,
                    session_id,
                    speaker=speaker,
                    message=message,
                    gm_reply=outcome.reply,
                    channel_id=channel_id,
                    message_id=message_id,
                    metadata=metadata,
                )
            else:
                runtime.log_manager.append_message(
                    campaign_id,
                    session_id,
                    speaker=speaker,
                    content=message,
                    role="table_talk" if outcome.target == "silent" else "user",
                    channel_id=channel_id,
                    message_id=message_id,
                    metadata=metadata,
                )
        except Exception as exc:
            recorder = getattr(runtime.log_manager, "record_append_failure", None)
            if callable(recorder):
                try:
                    recorder(
                        campaign_id=campaign_id,
                        session_id=session_id,
                        message_id=message_id,
                        error=exc,
                    )
                except Exception:
                    pass
            return str(exc)[:500]
        return ""

    @staticmethod
    def _reply_media(receipts: list[Any]) -> list[dict[str, object]]:
        media: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for receipt in receipts:
            if not receipt.ok or not isinstance(receipt.result, dict):
                continue
            for item in list(receipt.result.get("reply_media") or []):
                if not isinstance(item, dict):
                    continue
                media_type = str(item.get("type") or "").strip().lower()
                path = str(item.get("path") or "").strip()
                url = str(item.get("url") or "").strip()
                if media_type != "image" or not (path or url):
                    continue
                identity = (media_type, path, url)
                if identity in seen:
                    continue
                seen.add(identity)
                media.append(
                    {
                        "type": media_type,
                        "path": path,
                        "url": url,
                        "alt": str(item.get("alt") or "世界地图").strip(),
                    }
                )
        return media

    def _inspection_focus(self, session_id: str, channel_id: str) -> dict[str, object]:
        key = (str(channel_id or ""), str(session_id or ""))
        focus = self._inspection_focuses.get(key)
        if not focus:
            return {}
        if time.monotonic() - float(focus.get("updated_at") or 0) > 900:
            self._inspection_focuses.pop(key, None)
            return {}
        return {
            "campaign_id": str(focus.get("campaign_id") or ""),
            "slot": str(focus.get("slot") or ""),
        }

    def purge_campaign(self, campaign_id: str) -> None:
        """Drop process-local read focuses that point at a deleted campaign."""

        clean_campaign = str(campaign_id or "").strip()
        if not clean_campaign:
            return
        self._inspection_focuses = {
            key: focus
            for key, focus in self._inspection_focuses.items()
            if str(focus.get("campaign_id") or "").strip() != clean_campaign
        }

    def _update_inspection_focus(
        self,
        session_id: str,
        channel_id: str,
        receipts: list[Any],
    ) -> None:
        key = (str(channel_id or ""), str(session_id or ""))
        for receipt in reversed(receipts):
            if not receipt.ok:
                continue
            if receipt.tool_name in {"load_campaign", "create_campaign"}:
                self._inspection_focuses.pop(key, None)
                return
            if receipt.tool_name not in {
                "inspect_campaign",
                "get_hero_drafts",
                "get_world_state",
            }:
                if receipt.state_changed:
                    self._inspection_focuses.pop(key, None)
                    return
                continue
            campaign_id = str(receipt.result.get("campaign_id") or "").strip()
            source = str(receipt.result.get("source") or "").strip()
            if (
                receipt.tool_name in {"get_hero_drafts", "get_world_state"}
                and source == "live_runtime"
            ):
                self._inspection_focuses.pop(key, None)
                return
            if campaign_id and (
                receipt.tool_name == "inspect_campaign"
                or source == "persisted_snapshot"
            ):
                self._inspection_focuses[key] = {
                    "campaign_id": campaign_id,
                    "slot": str(receipt.result.get("slot") or ""),
                    "updated_at": time.monotonic(),
                }
                return

    def _request_metadata(
        self,
        payload: dict[str, Any],
        *,
        message: str,
        recent_context: str,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            **self.host._external_message_metadata(payload),
            "current_message": message,
            "recent_public_context": recent_context,
        }
        raw_turn_events = payload.get("current_turn_events")
        if isinstance(raw_turn_events, list):
            metadata["current_turn_events"] = [
                dict(item)
                for item in raw_turn_events
                if isinstance(item, dict)
            ]
            metadata["conversation_turn_id"] = str(
                payload.get("conversation_turn_id") or ""
            )
            metadata["turn_force_gm_reply"] = bool(
                payload.get("turn_force_gm_reply")
            )
        forced_mode = str(payload.get("forced_route_mode") or "").strip()
        if forced_mode in {"casual", "game", "pre_session", "session_zero", "safety"}:
            metadata["forced_route_mode"] = forced_mode
        if str(payload.get("batch_parent_id") or "").strip():
            metadata.update(
                {
                    "batch_parent_id": str(payload.get("batch_parent_id") or ""),
                    "batch_index": int(payload.get("batch_index") or 0),
                    "batch_count": int(payload.get("batch_count") or 0),
                    "batch_has_later_messages": bool(
                        payload.get("batch_has_later_messages")
                    ),
                }
            )
        if not self.host._truthy(payload.get("system_gm_beat_request")):
            return metadata
        metadata.update(
            {
                "system_gm_beat_request": True,
                "heartbeat_action": str(payload.get("heartbeat_action") or ""),
                "heartbeat_beat_purpose": str(
                    payload.get("heartbeat_beat_purpose") or ""
                ),
                "heartbeat_instruction": str(payload.get("heartbeat_instruction") or ""),
                "heartbeat_force": self.host._truthy(payload.get("heartbeat_force")),
                "heartbeat_require_material_change": self.host._truthy(
                    payload.get("heartbeat_require_material_change")
                ),
                "heartbeat_require_consequence": self.host._truthy(
                    payload.get("heartbeat_require_consequence")
                ),
                "heartbeat_require_local_change": self.host._truthy(
                    payload.get("heartbeat_require_local_change")
                ),
                "heartbeat_require_local_resolution": self.host._truthy(
                    payload.get("heartbeat_require_local_resolution")
                ),
                "heartbeat_require_signature_image_evolution": self.host._truthy(
                    payload.get("heartbeat_require_signature_image_evolution")
                ),
            }
        )
        idle_episode = payload.get("heartbeat_idle_episode")
        if isinstance(idle_episode, dict):
            metadata["heartbeat_idle_episode"] = dict(idle_episode)
        nudge_target = payload.get("heartbeat_session_zero_target")
        if isinstance(nudge_target, dict):
            metadata["heartbeat_session_zero_target"] = dict(nudge_target)
        supervisor_alerts = payload.get("heartbeat_supervisor_alerts")
        if isinstance(supervisor_alerts, list):
            metadata["heartbeat_supervisor_alerts"] = [
                dict(item)
                for item in supervisor_alerts[:4]
                if isinstance(item, dict)
            ]
        defeat_aftermath = payload.get("heartbeat_defeat_aftermath")
        if isinstance(defeat_aftermath, dict):
            metadata["heartbeat_defeat_aftermath"] = dict(defeat_aftermath)
        return metadata

    @staticmethod
    def _active_campaign_id(campaign_id: str, receipts: list[Any]) -> str:
        switched = next(
            (
                str(receipt.result.get("campaign_id") or "").strip()
                for receipt in reversed(receipts)
                if receipt.ok and receipt.tool_name in {"load_campaign", "create_campaign"}
            ),
            "",
        )
        if switched:
            return switched
        deleted_current = any(
            receipt.ok
            and receipt.tool_name == "delete_save"
            and str(receipt.result.get("deleted_scope") or "") == "campaign"
            and str(receipt.result.get("campaign_id") or "").strip() == campaign_id
            for receipt in receipts
        )
        return "default" if deleted_current else campaign_id

    @staticmethod
    def _deleted_campaign_id(receipts: list[Any]) -> str:
        return next(
            (
                str(receipt.result.get("campaign_id") or "").strip()
                for receipt in reversed(receipts)
                if receipt.ok
                and receipt.tool_name == "delete_save"
                and str(receipt.result.get("deleted_scope") or "") == "campaign"
            ),
            "",
        )
