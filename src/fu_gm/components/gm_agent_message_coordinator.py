from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Callable, Protocol

from fu_gm.components.campaign_state_transaction import (
    CampaignStateTransaction,
)
from fu_gm.components.gm_tool_pacing_observer import GMToolPacingObserver
from fu_gm.components.table_working_brief import TableWorkingBriefManager
from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_live_run_monitor import (
    bind_live_run,
    emit_live_run_event,
    reset_live_run,
)
from fu_gm.components.gm_intent_capability_router import (
    GMIntentCapabilityRouter,
)
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_supervisor import (
    GMCapabilityBroker,
    GMSupervisorStateCompressor,
)
from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolFreshnessGuard,
    json_safe_value,
)
from fu_gm.llm_client import classify_llm_error

SETUP_PROGRESS_TOOL_NAMES = frozenset(
    {
        "create_world_setting",
        "update_world_setting",
        "delete_world_setting",
        "rename_world_setting",
        "record_prologue_setup_answer",
        "select_first_act",
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
    public_expression_mode: str
    capability_routing_mode: str
    state_context_mode: str
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
        ("world_settings", "gm_world_setting_tools"),
    )

    def __init__(self, host: GMAgentMessageHost) -> None:
        self.host = host
        self.intent_capability_router = GMIntentCapabilityRouter()

    def build(self, context: GMToolExecutionContext) -> dict[str, object]:
        state = self.build_full(context)
        self._annotate_runtime_capability_context(context, state)
        self._grant_session_zero_hot_capabilities(context, state)
        self._grant_adventure_hot_capabilities(context, state)
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
    def _annotate_runtime_capability_context(
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> None:
        """让能力目录服从当前场景，而不是只看宽泛章节阶段。"""

        scene = dict(state.get("scene") or {})
        gameplay = dict(state.get("gameplay") or {})
        conflict = dict(gameplay.get("conflict") or {})
        context.metadata["_gm_runtime_scene_state_known"] = True
        context.metadata["_gm_scene_active"] = bool(scene.get("active"))
        context.metadata["_gm_conflict_active"] = bool(conflict.get("active"))

    def _grant_adventure_hot_capabilities(
        self,
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> None:
        if (
            context.gate_status != "adventure"
            or context.metadata.get("system_gm_beat_request")
        ):
            return
        phase_tools = set(
            GMToolAgentCapabilityPolicy.phase_tool_names(
                self.host.gm_tool_registry,
                context,
            )
            or set()
        )
        mode = self._capability_routing_mode(context)
        if mode in {"shadow", "intent"}:
            self._prepare_intent_capability_plan(
                context,
                state,
                phase_tools=phase_tools,
            )
            if (
                mode == "intent"
                and context.metadata.get("gm_intent_router_status")
                != "planned"
            ):
                # Router failure may only widen back to the already-tested
                # baseline capability set.  An empty plan would strand the
                # agent without the tools the old path guaranteed.
                mode = "baseline"
                context.metadata["gm_intent_effective_mode"] = "baseline"
        if mode == "intent":
            intent_tools = {
                str(name or "").strip()
                for name in list(
                    context.metadata.get("gm_intent_tool_names") or []
                )
                if str(name or "").strip()
            }
            effective = (
                intent_tools
                & phase_tools
                & set(self.host.gm_tool_registry._tools)
            )
            if effective:
                GMCapabilityBroker.grant(context, effective)
            # The compressor treats intent grants as a compact working set,
            # not as an explicit request to expand every overlapping domain.
            context.metadata["gm_hot_adventure_tool_names"] = sorted(effective)
            context.metadata["gm_intent_effective_mode"] = "intent"
            return
        if not context.metadata.get("gm_hot_adventure_capabilities_enabled"):
            return
        hot_tools = GMCapabilityBroker.adventure_hot_tool_names(
            registry=self.host.gm_tool_registry,
            phase_tools=phase_tools,
        )
        if hot_tools:
            GMCapabilityBroker.grant(context, hot_tools)
            context.metadata["gm_hot_adventure_tool_names"] = sorted(hot_tools)

    @staticmethod
    def _capability_routing_mode(
        context: GMToolExecutionContext,
    ) -> str:
        mode = str(
            context.metadata.get("gm_capability_routing_mode") or "baseline"
        ).strip().lower()
        return mode if mode in {"baseline", "shadow", "intent"} else "baseline"

    def _prepare_intent_capability_plan(
        self,
        context: GMToolExecutionContext,
        state: dict[str, object],
        *,
        phase_tools: set[str],
    ) -> None:
        # A plan is fixed for one message transaction. Explicit capability
        # discovery may still expand the grant later, but state refreshes must
        # never keep accumulating newly guessed hot schemas.
        if context.metadata.get("_gm_intent_plan_applied"):
            return
        context.metadata["_gm_intent_plan_applied"] = True
        try:
            plan = self.intent_capability_router.route(
                context,
                state,
                phase_tools,
                self.host.gm_tool_registry._tools,
            )
        except Exception as exc:
            context.metadata.update(
                {
                    "gm_intent_router_status": "fallback_baseline",
                    "gm_intent_router_error_type": type(exc).__name__,
                }
            )
            return
        context.metadata.update(
            {
                "gm_intent_router_status": "planned",
                "gm_intent_profile_ids": list(plan.profile_ids),
                "gm_intent_tool_names": list(plan.tool_names),
                "gm_intent_state_scopes": list(plan.state_scopes),
                "gm_intent_subjects": list(plan.subjects),
                "gm_intent_confidence": float(plan.confidence),
                # Proofs are fixed router reason codes, not player prose.
                "gm_intent_proofs": list(plan.proofs),
                "gm_intent_fallback_discovery": bool(
                    plan.fallback_discovery
                ),
            }
        )

    def _grant_session_zero_hot_capabilities(
        self,
        context: GMToolExecutionContext,
        state: dict[str, object],
    ) -> None:
        gate_status = str(context.gate_status or "").strip()
        if (
            gate_status not in {"inactive", "pre_session", "session_zero"}
            or context.metadata.get("system_gm_beat_request")
        ):
            return
        session_zero = dict(state.get("session_zero") or {})
        readiness = dict(session_zero.get("adventure_readiness") or {})
        transition = dict(session_zero.get("chapter_one_transition") or {})
        invited_ready = bool(
            gate_status == "session_zero"
            and bool(readiness.get("ready"))
            and str(transition.get("status") or "").strip() == "invited"
        )
        context.metadata["_gm_chapter_one_invited_ready"] = invited_ready
        # The marker is a correctness input for optimized phase policy, not a
        # broker/preload optimization.  Disabling hot schemas must never hide
        # both legal Chapter One entry tools.
        if not context.metadata.get(
            "gm_hot_session_zero_capabilities_enabled"
        ):
            return
        phase_tools = set(
            GMToolAgentCapabilityPolicy.phase_tool_names(
                self.host.gm_tool_registry,
                context,
            )
            or set()
        )
        mode = self._capability_routing_mode(context)
        if gate_status == "session_zero" and mode in {"shadow", "intent"}:
            self._prepare_intent_capability_plan(
                context,
                state,
                phase_tools=phase_tools,
            )
            if (
                mode == "intent"
                and context.metadata.get("gm_intent_router_status")
                != "planned"
            ):
                # A classifier failure may only widen back to the legacy
                # Session Zero hot set.  It must never strand creation midway.
                mode = "baseline"
                context.metadata["gm_intent_effective_mode"] = "baseline"

        hot_tools: set[str] = set()
        if gate_status == "session_zero" and mode == "intent":
            intent_tools = {
                str(name or "").strip()
                for name in list(
                    context.metadata.get("gm_intent_tool_names") or []
                )
                if str(name or "").strip()
            }
            hot_tools.update(
                intent_tools
                & phase_tools
                & set(self.host.gm_tool_registry._tools)
            )
            context.metadata["gm_intent_effective_mode"] = "intent"
        elif gate_status in {"pre_session", "session_zero"}:
            hot_tools.update(
                GMCapabilityBroker.session_zero_hot_tool_names(
                    registry=self.host.gm_tool_registry,
                    phase_tools=phase_tools,
                )
            )
        if (
            invited_ready
        ):
            # Once the GM has explicitly invited Chapter One, the player's
            # affirmative reply is the hottest Session Zero transition.  Put
            # the guarded start_session schema directly in the next decision
            # instead of spending model rounds rediscovering the table domain.
            opening_tool = (
                "start_adventure"
                if str(
                    context.metadata.get("adventure_opening_flow_mode")
                    or "legacy"
                ).strip().lower()
                == "optimized"
                else "start_session"
            )
            if (
                opening_tool in phase_tools
                and opening_tool in self.host.gm_tool_registry._tools
            ):
                hot_tools.add(opening_tool)
        if gate_status in {"inactive", "pre_session"}:
            hot_tools.update(
                GMCapabilityBroker.session_zero_entry_hot_tool_names(
                    registry=self.host.gm_tool_registry,
                    phase_tools=phase_tools,
                )
            )
        if hot_tools:
            GMCapabilityBroker.grant(context, hot_tools)
            context.metadata["gm_hot_session_zero_tool_names"] = sorted(
                hot_tools
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
            "player_character_aliases": {
                speaker: list(characters)
                for speaker, characters in controls.items()
                if speaker and characters
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
        windows = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if not bool(window.payload.get("suppress_public_prompt"))
        ]
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
        request_freshness_guard: Callable[[], bool] | None = None,
        side_effect_lock: Any | None = None,
        record_log: bool = True,
    ) -> dict[str, Any] | None:
        """Run one message with a best-effort, process-local live trace.

        The trace is deliberately wrapped around the entire coordinator call,
        including state observation, model work, expression and audit.  Any
        monitor failure falls back to the pre-existing transaction path.
        """

        agent = self.host.gm_tool_agent
        if agent is None:
            return None
        campaign_id, session_id, speaker, message, channel_id = (
            self.host._message_fields(payload)
        )
        if str(message or "").lstrip().startswith("/"):
            return None

        monitor = getattr(self.host, "gm_live_run_monitor", None)
        if monitor is None:
            return self._handle_bound(
                payload,
                gate=gate,
                is_private=is_private,
                explicitly_addressed=explicitly_addressed,
                recent_context=recent_context,
                freshness_guard=freshness_guard,
                request_freshness_guard=request_freshness_guard,
                side_effect_lock=side_effect_lock,
                record_log=record_log,
            )

        run_id = ""
        token = None
        response: dict[str, Any] | None = None
        raised: BaseException | None = None
        try:
            agent_timeout = max(
                1.0,
                float(getattr(agent, "timeout_seconds", 0.0) or 0.0),
            )
            run_id = monitor.start_run(
                campaign_id=campaign_id,
                session_id=session_id,
                channel_id=channel_id,
                conversation_turn_id=str(
                    payload.get("conversation_turn_id") or ""
                ),
                message_id=str(payload.get("message_id") or ""),
                speaker=speaker,
                source_kind=(
                    "system_gm_beat"
                    if self.host._truthy(payload.get("system_gm_beat_request"))
                    else "player_message"
                ),
                is_private=is_private,
                model=str(getattr(agent, "model", "") or ""),
                # The coordinator also performs a bounded write-lease wait
                # and may call the expression model after the core loop.  The
                # wider live budget prevents valid post-processing from being
                # mislabeled as a stuck worker.
                timeout_seconds=max(
                    agent_timeout * 2.0,
                    agent_timeout + 120.0,
                ),
                max_iterations=int(getattr(agent, "max_iterations", 0) or 0),
                message=message,
            )
            token = bind_live_run(monitor, run_id)
        except Exception:
            # Diagnostics must never become a prerequisite for hosting.
            run_id = ""
            token = None

        try:
            response = self._handle_bound(
                payload,
                gate=gate,
                is_private=is_private,
                explicitly_addressed=explicitly_addressed,
                recent_context=recent_context,
                freshness_guard=freshness_guard,
                request_freshness_guard=request_freshness_guard,
                side_effect_lock=side_effect_lock,
                record_log=record_log,
            )
            return response
        except BaseException as exc:
            raised = exc
            raise
        finally:
            if run_id:
                try:
                    if raised is not None:
                        monitor.finish_run(
                            run_id,
                            terminal_reason="exception",
                            status="exception",
                            summary="主持事务异常结束。",
                            public_details={
                                "exception_type": type(raised).__name__,
                            },
                        )
                    elif response is None:
                        monitor.finish_run(
                            run_id,
                            terminal_reason="not_applicable",
                            summary="该消息未进入自然语言主持事务。",
                        )
                    else:
                        route = str(response.get("route") or "")
                        stale = bool(response.get("stale_discarded")) or (
                            route == "gm_agent_stale"
                        )
                        failed = bool(response.get("agent_error")) or bool(
                            response.get("ok") is False
                            or route
                            in {
                                "gm_agent_fail_closed",
                                "gm_agent_unavailable",
                                "gm_agent_unavailable_silent",
                                "gm_agent_unresolved",
                                "gm_agent_unresolved_silent",
                                "gm_agent_message_transaction_rolled_back",
                            }
                        )
                        monitor.finish_run(
                            run_id,
                            terminal_reason=str(
                                route
                                or ("failed" if failed else "completed")
                            ),
                            status=(
                                "stale"
                                if stale
                                else "failed"
                                if failed
                                else "completed"
                            ),
                            summary=(
                                "过期请求已在安全点终止。"
                                if stale
                                else "主持事务已完成。"
                            ),
                            public_details={
                                "target": str(response.get("target") or ""),
                                "route": route,
                                "receipt_count": len(
                                    list(response.get("tool_receipts") or [])
                                ),
                                "state_changed": any(
                                    bool(item.get("state_changed"))
                                    for item in list(
                                        response.get("tool_receipts") or []
                                    )
                                    if isinstance(item, dict)
                                ),
                            },
                        )
                except Exception:
                    pass
            if token is not None:
                reset_live_run(token)

    def _handle_bound(
        self,
        payload: dict[str, Any],
        *,
        gate: Any,
        is_private: bool,
        explicitly_addressed: bool,
        recent_context: str,
        freshness_guard: GMToolFreshnessGuard | None = None,
        request_freshness_guard: Callable[[], bool] | None = None,
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

        emit_live_run_event(
            "coordinator_phase",
            phase="loading_runtime",
            summary="正在加载战役运行时。",
        )
        runtime = self.host._runtime(campaign_id)
        request_metadata = self._request_metadata(
            payload,
            message=message,
            recent_context=recent_context,
        )
        recent_public_messages = self._recent_public_messages(
            runtime,
            campaign_id,
            session_id,
            limit=(48 if request_metadata.get("heartbeat_persona_chat_only") else 12),
        )
        if request_metadata.get("heartbeat_persona_chat_only"):
            recent_public_messages = [
                item
                for item in recent_public_messages
                if str(item.get("role") or "")
                in {"user", "player", "table_talk"}
            ][-8:]
        request_metadata["recent_public_messages"] = recent_public_messages
        request_metadata["recent_private_messages"] = (
            self._recent_private_messages(
                runtime,
                campaign_id,
                session_id,
                limit=12,
            )
            if is_private
            else []
        )
        request_metadata["recent_message_delivery_context"] = (
            self._recent_message_delivery_context(
                campaign_id,
                session_id,
                channel_id,
                current_message_id=str(payload.get("message_id") or ""),
            )
        )
        if is_private and bool(request_metadata.get("anonymous")):
            request_metadata["recent_message_delivery_context"] = []
            raw_turn_events = request_metadata.get("current_turn_events")
            if isinstance(raw_turn_events, list):
                request_metadata["current_turn_events"] = [
                    {
                        "speaker": "匿名玩家",
                        "text": str(item.get("text") or ""),
                        "is_private": True,
                    }
                    for item in raw_turn_events
                    if isinstance(item, dict)
                ]
        request_metadata["gm_dynamic_capabilities_enabled"] = True
        capability_routing_mode = str(
            getattr(
                self.host,
                "capability_routing_mode",
                os.environ.get("FU_GM_CAPABILITY_ROUTING_MODE", "intent"),
            )
            or "intent"
        ).strip().lower()
        request_metadata["gm_capability_routing_mode"] = (
            capability_routing_mode
            if capability_routing_mode in {"baseline", "shadow", "intent"}
            else "baseline"
        )
        state_context_mode = str(
            getattr(
                self.host,
                "state_context_mode",
                os.environ.get(
                    "FU_GM_STATE_CONTEXT_MODE",
                    "summary_delta",
                ),
            )
            or "summary_delta"
        ).strip().lower()
        request_metadata["gm_state_context_mode"] = (
            state_context_mode
            if state_context_mode in {"full", "summary_delta"}
            else "full"
        )
        request_metadata["gm_hot_adventure_capabilities_enabled"] = (
            os.environ.get("FU_GM_ADVENTURE_HOT_CAPABILITIES", "1").lower()
            not in {"0", "false", "no", "disabled", "off"}
        )
        request_metadata["gm_hot_session_zero_capabilities_enabled"] = (
            os.environ.get("FU_GM_SESSION_ZERO_HOT_CAPABILITIES", "1").lower()
            not in {"0", "false", "no", "disabled", "off"}
        )
        request_metadata["adventure_opening_flow_mode"] = str(
            getattr(
                self.host,
                "adventure_opening_flow_mode",
                "legacy",
            )
            or "legacy"
        )
        current_scene = runtime.app.scene_manager.current_scene
        current_scene_type = str(
            getattr(
                getattr(current_scene, "scene_type", ""),
                "value",
                getattr(current_scene, "scene_type", ""),
            )
            or ""
        ).strip()
        if (
            str(getattr(gate, "status", "") or "").strip() == "adventure"
            and (
                current_scene is None
                or current_scene_type == "session_zero"
            )
        ):
            # 管理接口或旧版插件可能已经完成阶段切换，却尚未通过
            # start_session 的同事务 follow-up 建立第一幕。后续首条自然消息
            # 仍应获得与正常开团完全相同的完整场次准备，而不是退化成临时
            # 叙述或只含地点名称的空场景。
            request_metadata["opening_scene_requires_complete_prep"] = True
        inspection_focus = self._inspection_focus(session_id, channel_id)
        if inspection_focus:
            request_metadata["inspection_focus"] = inspection_focus
        def wait_for_committed_state(timeout_seconds: float) -> bool:
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            condition = runtime.write_lease_condition
            with condition:
                while runtime.write_lease_owner:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    condition.wait(timeout=remaining)
            return True

        def new_context_and_state() -> tuple[
            GMToolExecutionContext,
            dict[str, object],
        ]:
            metadata = dict(request_metadata)
            with runtime.transaction_lock:
                metadata["_gm_campaign_observed_version"] = int(
                    runtime.state_version
                )
                current_context = GMToolExecutionContext(
                    campaign_id=campaign_id,
                    session_id=session_id,
                    channel_id=channel_id,
                    speaker=speaker,
                    gate_status=gate.status,
                    is_private=is_private,
                    directly_addressed=bool(explicitly_addressed),
                    metadata=metadata,
                )
                current_state = self.state_builder.build(current_context)
            return current_context, current_state

        def current_state_provider() -> dict[str, object]:
            # Campaign-switch tools can update context.campaign_id during the
            # same Agent run.  Always lock and version the runtime whose state
            # is actually being projected; never read campaign B while holding
            # campaign A's lock.
            current_runtime = self.host._runtime(context.campaign_id)
            with current_runtime.transaction_lock:
                context.metadata["_gm_campaign_observed_version"] = int(
                    current_runtime.state_version
                )
                return self.state_builder.build(context)

        wait_budget = min(
            60.0,
            max(5.0, float(getattr(agent, "timeout_seconds", 30.0) or 30.0)),
        )
        emit_live_run_event(
            "coordinator_phase",
            phase="waiting_write_lease",
            summary="正在等待前一笔权威写事务提交。",
            public_details={"wait_budget_seconds": wait_budget},
        )
        lease_ready = wait_for_committed_state(wait_budget)
        emit_live_run_event(
            "write_lease_wait_finished",
            phase="observing_state",
            summary=(
                "写事务已空闲，开始读取权威状态。"
                if lease_ready
                else "写事务等待达到预算，继续读取当前可用状态。"
            ),
            public_details={"lease_ready": lease_ready},
        )
        if lease_ready:
            context, state_summary = new_context_and_state()
            emit_live_run_event(
                "state_observed",
                phase="running_agent",
                summary="权威状态快照已建立，进入智能体循环。",
                public_details={
                    "observed_version": int(
                        context.metadata.get("_gm_campaign_observed_version") or 0
                    ),
                    "state_sections": sorted(str(key) for key in state_summary),
                },
            )
            outcome = agent.run(
                message,
                recent_context=recent_context,
                context=context,
                state_summary=state_summary,
                state_summary_provider=current_state_provider,
                freshness_guard=freshness_guard,
                commit_freshness_guard=request_freshness_guard,
                side_effect_lock=side_effect_lock,
            )
        else:
            # The live runtime may already contain provisional mutations owned
            # by the timed-out writer.  Reading it here would let another model
            # observe facts that can still roll back.  Fail closed without
            # constructing a state projection.
            context = GMToolExecutionContext(
                campaign_id=campaign_id,
                session_id=session_id,
                channel_id=channel_id,
                speaker=speaker,
                gate_status=gate.status,
                is_private=is_private,
                directly_addressed=bool(explicitly_addressed),
                metadata={
                    **dict(request_metadata),
                    "_gm_write_lease_timeout": True,
                },
            )
            must_reply = bool(explicitly_addressed or is_private)
            outcome = GMToolAgentOutcome(
                handled=True,
                reply=(
                    "前一笔游戏状态事务仍在提交中，本轮没有读取或改变状态。请稍后重试。"
                    if must_reply
                    else ""
                ),
                target="fu_gm" if must_reply else "silent",
                mode=(
                    "gm_agent_unavailable"
                    if must_reply
                    else "gm_agent_unavailable_silent"
                ),
                reason="等待权威写事务提交超时；为避免读取未提交状态而安全终止。",
                error="WRITE_LEASE_TIMEOUT",
                stop_astrbot=True,
                trace=[
                    {
                        "write_lease_timeout": {
                            "fail_closed": True,
                            "state_observed": False,
                        }
                    }
                ],
            )
        version_conflict = context.metadata.get("_gm_campaign_version_conflict")
        is_system_beat = bool(request_metadata.get("system_gm_beat_request"))
        if (
            version_conflict
            and not is_system_beat
            and not any(
                receipt.ok and receipt.state_changed
                for receipt in outcome.receipts
            )
            and wait_for_committed_state(wait_budget)
        ):
            emit_live_run_event(
                "campaign_version_replan",
                phase="observing_state",
                summary="检测到战役版本变化，正在基于最新状态重新规划。",
                public_details={"conflict": str(version_conflict)},
            )
            first_trace = list(outcome.trace)
            context, state_summary = new_context_and_state()
            emit_live_run_event(
                "state_reobserved",
                phase="running_agent",
                summary="最新状态快照已建立，重新进入智能体循环。",
                public_details={
                    "observed_version": int(
                        context.metadata.get("_gm_campaign_observed_version") or 0
                    ),
                },
            )
            outcome = agent.run(
                message,
                recent_context=recent_context,
                context=context,
                state_summary=state_summary,
                state_summary_provider=current_state_provider,
                freshness_guard=freshness_guard,
                commit_freshness_guard=request_freshness_guard,
                side_effect_lock=side_effect_lock,
            )
            outcome.trace.insert(
                0,
                {
                    "campaign_version_replan": {
                        "conflict": version_conflict,
                        "previous_trace_steps": len(first_trace),
                        "new_observed_version": context.metadata.get(
                            "_gm_campaign_observed_version"
                        ),
                    }
                },
            )
        emit_live_run_event(
            "coordinator_phase",
            phase="checking_freshness",
            summary="智能体循环结束，正在检查请求是否仍为最新消息。",
        )
        request_stale = False
        if request_freshness_guard is not None:
            try:
                request_stale = not bool(request_freshness_guard())
            except Exception:
                request_stale = True
        committed_state_change = any(
            receipt.ok and receipt.state_changed
            for receipt in outcome.receipts
        )
        if request_stale and not committed_state_change:
            outcome.handled = True
            outcome.reply = ""
            outcome.reply_parts = []
            outcome.target = "silent"
            outcome.mode = "gm_agent_stale"
            outcome.stop_astrbot = True
            outcome.reason = "生成期间出现了新的群聊消息，已在写入前终止过期请求。"
            outcome.trace.append(
                {
                    "request_freshness": {
                        "stale_discarded": True,
                        "state_changed": False,
                    }
                }
            )
        elif request_stale:
            outcome.trace.append(
                {
                    "request_freshness": {
                        "stale_after_commit": True,
                        "state_changed": True,
                    }
                }
            )
        emit_live_run_event(
            "freshness_checked",
            phase="supervising_receipts",
            summary=(
                "请求已过期，按提交状态决定终止或保留结果。"
                if request_stale
                else "请求仍然有效，继续整理回执。"
            ),
            public_details={
                "request_stale": request_stale,
                "committed_state_change": committed_state_change,
            },
        )
        supervisor_observation = self.host.gm_supervisor.observe_receipts(
            context,
            outcome.receipts,
        )
        opening_prefetch: dict[str, object] = {}
        if str(
            getattr(self.host, "adventure_opening_flow_mode", "legacy")
            or "legacy"
        ) == "optimized":
            invited = next(
                (
                    receipt
                    for receipt in outcome.receipts
                    if receipt.ok
                    and receipt.tool_name == "set_chapter_one_transition"
                    and str(receipt.result.get("posture") or "") == "invited"
                ),
                None,
            )
            if invited is not None and (
                self.host.adventure_opening_prefetcher.model_available(runtime)
            ):
                opening_prefetch = (
                    self.host.adventure_opening_prefetcher.schedule(
                        campaign_id=campaign_id,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                )
            elif invited is not None:
                opening_prefetch = {
                    "status": "disabled",
                    "reason": "session_prep_model_unavailable",
                }
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

        emit_live_run_event(
            "coordinator_phase",
            phase="rendering_expression",
            summary="正在生成并校验公开表达。",
        )
        public_expression = self._apply_public_expression(
            runtime,
            outcome=outcome,
            current_message=(
                ""
                if request_metadata.get("heartbeat_persona_chat_only")
                else message
            ),
            recent_context=recent_context,
            gate_status=str(getattr(gate, "status", "") or ""),
            expression_mode=str(
                getattr(self.host, "public_expression_mode", "core") or "core"
            ),
            core_model=str(
                getattr(
                    getattr(self.host, "gm_agent_runtime", None),
                    "llm_model",
                    "",
                )
                or ""
            ),
        )
        context.metadata["_gm_public_expression"] = dict(public_expression)
        emit_live_run_event(
            "expression_finished",
            phase="updating_observers",
            summary="公开表达阶段完成。",
            public_details={
                "attempted": bool(public_expression.get("attempted")),
                "author": str(public_expression.get("author") or ""),
            },
        )
        isolated_failure = self._is_uncommitted_agent_failure(outcome)

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
        pacing_observation = (
            {}
            if isolated_failure
            else self._observe_and_persist_pacing(
                pacing_runtime,
                active_campaign_id,
                context,
                outcome.receipts,
            )
        )
        working_brief_observation = (
            {}
            if isolated_failure
            else self._observe_and_persist_working_brief(
                pacing_runtime,
                active_campaign_id,
                source_campaign_id=campaign_id,
                context=context,
                outcome=outcome,
            )
        )
        provider_failure = self._is_provider_agent_failure(outcome)
        error_disposition = (
            classify_llm_error(outcome.error)
            if provider_failure
            else None
        )
        agent_error_category = (
            self._agent_failure_category(outcome)
            if isolated_failure and not provider_failure
            else ""
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
            "context_manifest": dict(
                context.metadata.get("_gm_context_manifest") or {}
            ),
            "agent_loop": dict(outcome.loop_diagnostics or {}),
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
            "adventure_opening_prefetch": opening_prefetch,
            "public_expression": public_expression,
            "audit_log_isolated": isolated_failure,
            "retry_safe": isolated_failure,
            "provider_error_category": (
                error_disposition.category
                if error_disposition is not None
                else ""
            ),
            "agent_error_category": agent_error_category,
        }
        audit_runtime = pacing_runtime if deleted_campaign_id == campaign_id else runtime
        audit_campaign_id = (
            active_campaign_id
            if deleted_campaign_id == campaign_id
            else campaign_id
        )
        audit_log_error = ""
        provider_failure_audit: dict[str, object] = {}
        emit_live_run_event(
            "coordinator_phase",
            phase="writing_audit",
            summary="正在写入本地审计记录。",
            public_details={
                "record_log": bool(record_log),
                "isolated_failure": isolated_failure,
            },
        )
        if isolated_failure and record_log:
            try:
                provider_failure_audit = audit_runtime.log_manager.record_provider_failure(
                    audit_campaign_id,
                    session_id,
                    source_event_id=self._current_source_event_id(request_metadata),
                    message_id=str(payload.get("message_id") or ""),
                    mode=outcome.mode,
                    error=outcome.error,
                    error_category=(
                        error_disposition.category
                        if error_disposition is not None
                        else agent_error_category or "agent_unresolved"
                    ),
                    retry_safe=True,
                )
            except Exception as exc:
                audit_log_error = str(exc)[:500]
        else:
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
                is_private=is_private,
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
            "context_manifest": dict(
                context.metadata.get("_gm_context_manifest") or {}
            ),
            "agent_loop": dict(outcome.loop_diagnostics or {}),
            "agent_error": outcome.error,
            "pacing_observation": pacing_observation,
            "working_brief_observation": working_brief_observation,
            "supervisor_observation": supervisor_observation,
            "adventure_opening_prefetch": opening_prefetch,
            "public_expression": public_expression,
            "audit_log_isolated": isolated_failure,
            "retry_safe": isolated_failure,
            "provider_error_category": (
                error_disposition.category
                if error_disposition is not None
                else ""
            ),
            "agent_error_category": agent_error_category,
            "provider_failure_audit": provider_failure_audit,
            "stale_discarded": bool(
                request_stale and not committed_state_change
            ),
            "stale_after_commit": bool(
                request_stale and committed_state_change
            ),
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
        emit_live_run_event(
            "audit_finished",
            phase="building_response",
            summary=(
                "审计写入出现错误，业务响应仍按原事务结果返回。"
                if audit_log_error
                else "审计记录完成，正在组装响应。"
            ),
            public_details={
                "audit_ok": not bool(audit_log_error),
                "route": str(outcome.mode or ""),
                "target": str(outcome.target or ""),
            },
        )
        return response

    @staticmethod
    def _apply_public_expression(
        runtime: Any,
        *,
        outcome: Any,
        current_message: str,
        recent_context: str,
        gate_status: str,
        expression_mode: str = "core",
        core_model: str = "",
    ) -> dict[str, object]:
        """选择普通 GM 回复的最终公开作者。

        主线路径由核心 Agent 直接生成、Grounding 校验并发布最终文本，不再
        追加一次模型改写。显式 ``expressor`` 模式仅作为短期回滚兼容路径。
        规则表达器与 NPC 表达器生成的锁定回执已经拥有明确作者，始终直出。
        """

        if outcome.target != "fu_gm" or not (outcome.reply or outcome.reply_parts):
            return {"attempted": False, "author": "none"}
        locked_tools = [
            receipt.tool_name
            for receipt in outcome.receipts
            if receipt.lock_public_reply
        ]
        if locked_tools:
            return {
                "attempted": False,
                "author": "focused_component",
                "locked_tools": locked_tools,
            }
        if outcome.mode not in {"gm_agent_reply", "gm_agent_tool"}:
            return {
                "attempted": False,
                "author": "system",
                "route_mode": outcome.mode,
            }
        drafts = [
            str(part or "").strip()
            for part in list(outcome.reply_parts or [])
            if str(part or "").strip()
        ]
        if not drafts and str(outcome.reply or "").strip():
            drafts = [str(outcome.reply).strip()]
        normalized_mode = str(expression_mode or "core").strip().lower()
        if normalized_mode != "expressor":
            return {
                "attempted": False,
                "author": "core_gm",
                "model": str(core_model or ""),
                "merged_into_core": True,
                "input_parts": len(drafts),
                "output_parts": len(drafts),
                "expression_mode": "core",
            }
        renderer = getattr(runtime.app, "expressor", None)
        render = getattr(renderer, "render_agent_message", None)
        if not callable(render):
            return {
                "attempted": False,
                "author": "core_gm_legacy",
                "reason": "当前表达器尚未实现普通GM消息接口。",
            }
        try:
            expression_style = GMAgentMessageCoordinator._public_expression_style(
                outcome,
                gate_status=gate_status,
            )
            rendered = render(
                drafts,
                current_message=current_message,
                recent_context=recent_context,
                gate_status=gate_status,
                route_mode=outcome.mode,
                expression_style=expression_style,
            )
            rendered = [
                str(part or "").strip()
                for part in list(rendered or [])
                if str(part or "").strip()
            ]
            if len(rendered) != len(drafts):
                raise ValueError("表达器改变了公开消息段数。")
            outcome.reply_parts = rendered
            outcome.reply = "\n".join(rendered)
            metadata = dict(
                getattr(renderer, "last_agent_message_metadata", {}) or {}
            )
            return {
                "attempted": True,
                "author": str(metadata.get("author") or "expressor"),
                "model": str(
                    metadata.get("model") or getattr(renderer, "model", "")
                ),
                "used_fallback": bool(metadata.get("used_fallback", False)),
                "input_parts": len(drafts),
                "output_parts": len(rendered),
                "expression_style": expression_style,
                "expression_mode": "expressor",
                **(
                    {"error": str(metadata.get("error"))}
                    if metadata.get("error")
                    else {}
                ),
            }
        except Exception as exc:
            return {
                "attempted": True,
                "author": "core_gm_degraded_fallback",
                "used_fallback": True,
                "expression_mode": "expressor",
                "error": str(exc)[:300],
                "input_parts": len(drafts),
                "output_parts": len(drafts),
            }

    @staticmethod
    def _public_expression_style(
        outcome: Any,
        *,
        gate_status: str,
    ) -> str:
        """Select creative immersion from semantic trace, never keywords."""

        if outcome.receipts or outcome.mode != "gm_agent_reply":
            return "plain"
        message_kind = ""
        for step in reversed(list(outcome.trace or [])):
            if not isinstance(step, dict):
                continue
            candidate = str(step.get("message_kind") or "").strip().lower()
            if candidate:
                message_kind = candidate
                break
        if message_kind in {
            "discussion",
            "performed_action",
            "npc_or_world_interaction",
            "gm_request",
            "mixed",
        } and str(gate_status or "") in {
            "inactive",
            "pre_session",
            "session_zero",
            "adventure",
            "paused",
        }:
            return "immersive"
        return "plain"

    @staticmethod
    def _is_uncommitted_agent_failure(outcome: Any) -> bool:
        """识别可以安全重试、但不得进入故事上下文的失败。"""

        if any(
            receipt.ok and receipt.state_changed
            for receipt in list(getattr(outcome, "receipts", None) or [])
        ):
            return False
        mode = str(getattr(outcome, "mode", "") or "").strip()
        return mode in {
            "gm_agent_fail_closed",
            "gm_agent_unavailable",
            "gm_agent_unavailable_silent",
            "gm_agent_unresolved",
            "gm_agent_unresolved_silent",
        }

    @staticmethod
    def _is_provider_agent_failure(outcome: Any) -> bool:
        """Only unavailable modes represent a provider-layer failure.

        Protocol rejection and iteration exhaustion are intentionally isolated
        from the story transcript too, but they must not be labelled as an
        HTTP/provider outage merely because ``classify_llm_error`` cannot map
        their internal error text and returns ``unknown``.
        """

        mode = str(getattr(outcome, "mode", "") or "").strip()
        return mode in {
            "gm_agent_unavailable",
            "gm_agent_unavailable_silent",
        }

    @staticmethod
    def _agent_failure_category(outcome: Any) -> str:
        """Return the last exact protocol code for an isolated agent failure."""

        for step in reversed(list(getattr(outcome, "trace", None) or [])):
            if not isinstance(step, dict):
                continue
            code = str(step.get("protocol_error") or "").strip()
            if code:
                return code
        diagnostics = dict(getattr(outcome, "loop_diagnostics", None) or {})
        terminal = str(diagnostics.get("terminal_reason") or "").strip()
        return terminal or "agent_unresolved"

    @staticmethod
    def _current_source_event_id(metadata: dict[str, Any]) -> str:
        events = [
            item
            for item in list(metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        if events:
            return str(events[-1].get("event_id") or "").strip()
        return str(metadata.get("source_event_id") or "").strip()

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

    @staticmethod
    def _recent_private_messages(
        runtime: Any,
        campaign_id: str,
        session_id: str,
        *,
        limit: int = 12,
    ) -> list[dict[str, object]]:
        """读取同一私聊线程，不把GM私密笔记或公开桌面消息混进来。"""

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
            in {"private", "system_private"}
            and str(getattr(entry, "content", "") or "").strip()
        ]
        return [
            {
                "message_id": str(getattr(entry, "message_id", "") or ""),
                "speaker": str(getattr(entry, "speaker", "") or ""),
                "role": (
                    "assistant"
                    if str(getattr(entry, "role", "") or "") == "system_private"
                    else "user"
                ),
                "source_role": str(getattr(entry, "role", "") or ""),
                "visibility": "private_thread",
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
        is_private: bool,
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
            if is_private:
                private_metadata = {
                    "private": True,
                    "anonymized": True,
                    "mode": str(metadata.get("mode") or ""),
                    "state_changed": bool(metadata.get("state_changed")),
                    "agent_target": str(metadata.get("agent_target") or ""),
                    "agent_terminal_action": str(
                        metadata.get("agent_terminal_action") or ""
                    ),
                }
                source_messages = current_turn or [
                    {"text": message}
                ]
                for item in source_messages:
                    runtime.log_manager.append_message(
                        campaign_id,
                        session_id,
                        speaker="匿名玩家",
                        content=str(item.get("text") or ""),
                        role="private",
                        channel_id="",
                        message_id="",
                        metadata=private_metadata,
                    )
                for part in reply_parts:
                    runtime.log_manager.append_message(
                        campaign_id,
                        session_id,
                        speaker="AI GM",
                        content=part,
                        role="system_private",
                        channel_id="",
                        message_id="",
                        metadata=private_metadata,
                    )
                return ""
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
                "heartbeat_persona_chat_only": self.host._truthy(
                    payload.get("heartbeat_persona_chat_only")
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
