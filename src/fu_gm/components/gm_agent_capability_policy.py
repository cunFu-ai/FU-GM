from __future__ import annotations

from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry


class GMToolAgentCapabilityPolicy:
    """Expose every tool allowed by the trusted session phase."""

    _COMMON_SCOPES = {
        "discover_capabilities",
        "inspect_supervisor_state",
        "acknowledge_supervisor_alert",
        "reconcile_supervisor_state",
        "list_saves",
        "inspect_campaign",
        "create_campaign",
        "save_campaign",
        "load_campaign",
        "delete_save",
        "get_session_status",
        "set_player_attendance",
        "get_session_zero_readiness",
        "get_hero_drafts",
        "get_hero_state",
        "get_world_state",
        "get_world_map_status",
        "inspect_semantic_map",
        "find_map_location_candidates",
        "place_world_map_locations",
        "generate_world_map_preview",
        "edit_world_map",
        "get_rule_reference",
        "search_rule_references",
        "get_runtime_state",
        "get_progression_state",
        "level_up_character",
        "start_session",
        "pause_session",
        "end_session",
        "record_safety_boundary",
    }
    _SESSION_ZERO_SCOPES = _COMMON_SCOPES | {
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
    _PRE_SESSION_SCOPES = _COMMON_SCOPES | {
        "pause_session_zero_nudges",
    }
    _ADVENTURE_SCOPES = _COMMON_SCOPES | {
        "get_travel_state",
        "travel_party",
        "continue_travel",
        "abort_travel",
        "award_stage_reward",
        "get_dungeon_state",
        "start_dungeon_exploration",
        "finish_dungeon_exploration",
        "get_clocks",
        "create_clock",
        "change_clock",
        "close_clock",
        "get_gameplay_state",
        "create_loyal_companion",
        "learn_chimerist_spell",
        "recall_scene_memory",
        "resolve_tavern_talk",
        "perform_check_action",
        "perform_character_action",
		"perform_scene_action",
		"perform_in_scene_action",
		"commit_story_item_action",
		"move_group_within_scene",
		"move_scene_group",
		"pass_in_scene_action",
		"perform_ritual_project_action",
        "resolve_rule_window",
        "resolve_gm_opportunity",
        "get_npc_profiles",
        "create_npc_profile",
        "introduce_npc",
        "preview_npc_combatant",
        "create_npc_combatant",
        "configure_boss_phases",
        "update_npc_state",
        "revise_npc_profile",
        "decide_npc_response",
        "decide_collective_response",
        "start_scene",
        "focus_scene_branch",
        "transition_scene",
        "end_scene",
        "start_conflict",
        "run_current_npc_turn",
        "end_conflict",
        "get_scene_state",
        "commit_scene_response",
    }
    _GATE_SCOPES = {
        "pre_session": _PRE_SESSION_SCOPES,
        "session_zero": _SESSION_ZERO_SCOPES,
        "adventure": _ADVENTURE_SCOPES,
        "paused": _COMMON_SCOPES,
        "inactive": _COMMON_SCOPES,
    }

    _SYSTEM_BEAT_SCOPES: dict[str, set[str]] = {
        "scene_opening": {
            "get_scene_state",
            "commit_scene_response",
            "get_clocks",
            "create_clock",
            "change_clock",
            "close_clock",
            "get_npc_profiles",
            "introduce_npc",
            "get_gameplay_state",
            "preview_npc_combatant",
            "create_npc_combatant",
            "configure_boss_phases",
            "update_npc_state",
            "decide_npc_action",
            "decide_collective_action",
            "start_scene",
            "transition_scene",
            "end_scene",
            "start_conflict",
            "resolve_gm_opportunity",
        },
        "free_scene_beat": {
            "get_scene_state",
            "commit_scene_response",
            "get_clocks",
            "create_clock",
            "change_clock",
            "close_clock",
            "get_npc_profiles",
            "introduce_npc",
            "update_npc_state",
            "decide_npc_action",
            "decide_collective_action",
            "get_gameplay_state",
            "preview_npc_combatant",
            "create_npc_combatant",
            "configure_boss_phases",
            "start_scene",
            "transition_scene",
            "end_scene",
            "start_conflict",
            "end_conflict",
            "resolve_gm_opportunity",
        },
        "npc_turn": {
            "get_scene_state",
            "get_npc_profiles",
            "get_gameplay_state",
            "run_current_npc_turn",
            "resolve_gm_opportunity",
        },
        "pc_turn_reminder": {"get_gameplay_state"},
        "session_zero_nudge": {
            "get_session_status",
            "get_hero_drafts",
        },
        "supervisor_recovery": {
            "inspect_supervisor_state",
            "reconcile_supervisor_state",
            "get_runtime_state",
            "get_scene_state",
            "get_clocks",
            "get_gameplay_state",
        },
    }

    @classmethod
    def managed_tool_names(cls) -> set[str]:
        """Return the complete built-in capability surface owned by this policy."""

        return set().union(
            *cls._GATE_SCOPES.values(),
            *cls._SYSTEM_BEAT_SCOPES.values(),
        )

    @classmethod
    def phase_tool_names(
        cls,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
    ) -> set[str] | None:
        if context.metadata.get("system_gm_beat_request"):
            action = str(context.metadata.get("heartbeat_action") or "").strip()
            return set(cls._SYSTEM_BEAT_SCOPES.get(action, set()))
        gate = str(context.gate_status or "").strip().lower()
        scope = cls._GATE_SCOPES.get(gate)
        # Persisted campaigns may contain a stale or future gate value. Unknown
        # phases must never expand into the registry's complete write surface.
        return set(scope) if scope is not None else set(cls._COMMON_SCOPES)

    @classmethod
    def schemas(
        cls,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
    ) -> list[dict[str, object]]:
        return registry.schemas(cls.phase_tool_names(registry, context))

    @classmethod
    def schemas_for_names(
        cls,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
        names: set[str],
    ) -> list[dict[str, object]]:
        phase_names = cls.phase_tool_names(registry, context)
        effective = set(names) if phase_names is None else set(names) & phase_names
        return registry.schemas(effective)
