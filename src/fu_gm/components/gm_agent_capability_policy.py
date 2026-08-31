from __future__ import annotations

from fu_gm.components.scene_change_authority import SceneChangeAuthorityPolicy
from fu_gm.gm_tool_contracts import GMToolExecutionContext, GMToolRegistry


class GMToolAgentCapabilityPolicy:
    """Expose every tool allowed by the trusted session phase."""

    _FOLLOWUP_ONLY_TOOLS = {
        "commit_scene_response",
        "perform_check_action",
    }
    _RESTRICTED_SYSTEM_TOOLS = {
        "decide_npc_action",
        "decide_collective_action",
    }
    # Forced free-scene beats may establish one new NPC before that NPC acts.
    # introduce_npc remains a normal adventure capability, so it must not be
    # classified as system-only alongside the two restricted decision tools.
    _FORCED_FREE_SCENE_BEAT_TOOLS = _RESTRICTED_SYSTEM_TOOLS | {
        "introduce_npc",
    }
    _SCENE_LIFECYCLE_TOOLS = {
        "start_scene",
        "focus_scene_branch",
        "transition_scene",
        "end_scene",
    }
    _MAP_MUTATION_SCOPES = {
        "find_map_location_candidates",
        "place_world_map_locations",
        "generate_world_map_preview",
        "edit_world_map",
    }
    _WORLD_SETTING_WRITE_SCOPES = {
        "create_world_setting",
        "update_world_setting",
        "delete_world_setting",
        "rename_world_setting",
    }

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
        "get_session_zero_contributions",
        "get_session_zero_readiness",
        "get_hero_drafts",
        "get_hero_state",
        "get_world_state",
        "query_world_settings",
        "get_world_map_status",
        "inspect_semantic_map",
        "get_rule_reference",
        "search_rule_references",
        "roll_dice",
        "get_runtime_state",
        "get_progression_state",
        "level_up_character",
        "start_session",
        "pause_session",
        "end_session",
        "record_safety_boundary",
        "delegate_background_task",
        "list_background_tasks",
        "get_background_task",
        "cancel_background_task",
        "resume_background_task",
    }
    _SESSION_ZERO_SCOPES = (
        _COMMON_SCOPES
        | _MAP_MUTATION_SCOPES
        | _WORLD_SETTING_WRITE_SCOPES
        | {
        "propose_session_zero_update",
        "record_prologue_setup_answer",
        "select_first_act",
        "confirm_session_zero_proposal",
        "mark_session_zero_topic_complete",
        "set_session_zero_nudge_preference",
        "pause_session_zero_nudges",
        "set_chapter_one_transition",
        "start_adventure",
        "update_hero_draft",
        "confirm_hero_draft",
        "create_loyal_companion",
        }
    )
    _SESSION_ZERO_ENTRY_SCOPES = {
        *_WORLD_SETTING_WRITE_SCOPES,
    }
    _PRE_SESSION_SCOPES = _COMMON_SCOPES | _SESSION_ZERO_ENTRY_SCOPES | {
        "pause_session_zero_nudges",
    }
    _ADVENTURE_SCOPES = (
        _COMMON_SCOPES
        | _MAP_MUTATION_SCOPES
        | _WORLD_SETTING_WRITE_SCOPES
        | {
        "get_travel_state",
        "suggest_route_travel_days",
        "travel_party",
        "continue_travel",
        "abort_travel",
        "award_stage_reward",
        "get_dungeon_state",
        "start_dungeon_exploration",
        "finish_dungeon_exploration",
        "get_clocks",
        "create_clock",
        "fill_clock",
        "erase_clock",
        "close_clock",
        "get_gameplay_state",
        "set_equipment_access",
        "create_loyal_companion",
        "learn_chimerist_spell",
        "recall_scene_memory",
        "resolve_tavern_talk",
        "declare_check_action",
        "declare_movement_check",
        "perform_check_action",
        "perform_character_action",
		"perform_scene_action",
		"perform_in_scene_action",
		"commit_story_item_action",
		"commit_scene_fixture_action",
		"move_group_within_scene",
		"move_scene_group",
		"pass_in_scene_action",
		"set_absent_character_mode",
		"perform_ritual_project_action",
        "resolve_rule_window",
        "resolve_gm_opportunity",
        "get_npc_profiles",
        "create_npc_profile",
        "introduce_npc",
        "prepare_npc_combatant",
        "get_npc_combatant_design",
        "finalize_npc_combatant_preparation",
        "commit_npc_combatant_design",
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
        }
    )
    _GATE_SCOPES = {
        "pre_session": _PRE_SESSION_SCOPES,
        "session_zero": _SESSION_ZERO_SCOPES,
        "adventure": _ADVENTURE_SCOPES,
        "paused": _COMMON_SCOPES,
        "inactive": _COMMON_SCOPES | _SESSION_ZERO_ENTRY_SCOPES,
    }

    _SYSTEM_BEAT_SCOPES: dict[str, set[str]] = {
        # 现实群聊冷场不等于虚构时间推进。线上群聊续接只能由模型直接
        # final 或 silent，不能获得任何读写工具后顺手改变局面。
        "adventure_table_nudge": set(),
        "scene_opening": {
            "get_scene_state",
            "get_clocks",
            "get_npc_profiles",
            "get_gameplay_state",
            "start_scene",
        },
        "free_scene_beat": {
            "get_scene_state",
            "get_clocks",
            "get_npc_profiles",
            "get_gameplay_state",
        },
        "npc_turn": {
            "get_scene_state",
            "get_npc_profiles",
            "get_gameplay_state",
            "run_current_npc_turn",
            "resolve_gm_opportunity",
        },
        # 异步多人检定可能在原始工具事务结束后才产生GM机会。这个节拍
        # 只允许读取当前局面并结算该机会，不能借机推进NPC或场景。
        "gm_opportunity": {
            "get_scene_state",
            "get_gameplay_state",
            "resolve_gm_opportunity",
        },
        "conflict_resolution": {
            "get_scene_state",
            "get_gameplay_state",
            "end_conflict",
        },
        "defeat_aftermath": {
            "get_scene_state",
            "get_gameplay_state",
            "start_scene",
            "focus_scene_branch",
            "transition_scene",
        },
        "pc_turn_reminder": {"get_gameplay_state"},
        "session_zero_nudge": {
            "set_chapter_one_transition",
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
            cls._FOLLOWUP_ONLY_TOOLS,
            cls._RESTRICTED_SYSTEM_TOOLS,
        )

    @classmethod
    def phase_tool_names(
        cls,
        registry: GMToolRegistry,
        context: GMToolExecutionContext,
    ) -> set[str] | None:
        if context.metadata.get("system_gm_beat_request"):
            action = str(context.metadata.get("heartbeat_action") or "").strip()
            names = set(cls._SYSTEM_BEAT_SCOPES.get(action, set()))
            if action == "session_zero_nudge":
                raw_target = context.metadata.get(
                    "heartbeat_session_zero_target"
                )
                target = raw_target if isinstance(raw_target, dict) else {}
                if str(target.get("status") or "").strip() != "chapter_one_ready":
                    names.discard("set_chapter_one_transition")
            # 普通闲置心跳仍然只读，避免把现实群聊冷场误当成虚构时间推进。
            # 只有调用方明确请求一次主持节拍时，才允许当前场景中的NPC或
            # 集体通过专用行动工具作出回应；规则层仍会校验其确实在场、
            # 行动合法且没有替玩家作决定。
            if (
                action == "free_scene_beat"
                and context.metadata.get("heartbeat_force") is True
            ):
                names.update(cls._FORCED_FREE_SCENE_BEAT_TOOLS)
        else:
            gate = str(context.gate_status or "").strip().lower()
            scope = cls._GATE_SCOPES.get(gate)
            # Persisted campaigns may contain a stale or future gate value. Unknown
            # phases must never expand into the registry's complete write surface.
            names = set(scope) if scope is not None else set(cls._COMMON_SCOPES)

        opening_flow = str(
            context.metadata.get("adventure_opening_flow_mode") or "legacy"
        ).strip().lower()
        if str(context.gate_status or "").strip().lower() == "session_zero":
            if opening_flow == "optimized":
                names.discard("start_session")
                if context.metadata.get("_gm_chapter_one_invited_ready"):
                    names.add("start_adventure")
                else:
                    names.discard("start_adventure")
            else:
                names.discard("start_adventure")
        else:
            names.discard("start_adventure")

        # 这些工具只能由上一条权威回执临时授权。GMToolAgent 会在收到
        # required_followup_tools 后直接暴露精确 schema，无需让模型在普通
        # 回合的目录里猜测并跳过声明阶段。
        if context.metadata.get("gm_dynamic_capabilities_enabled"):
            names.difference_update(cls._FOLLOWUP_ONLY_TOOLS)

        # 自由环境回应是事务内的收尾能力：普通消息只接受上一条成功
        # 回执的required-followup；系统节拍还可直接送达已到期且带有
        # 精确公开结果的结构化记录。
        if SceneChangeAuthorityPolicy.trusted_required_followup(
            context,
            "commit_scene_response",
        ) or (
            context.metadata.get("system_gm_beat_request")
            and SceneChangeAuthorityPolicy.has_pending_system_beat_authority(context)
        ) or (
            context.metadata.get("system_gm_beat_request")
            and context.metadata.get("gm_authored_scene_opening") is True
            and str(context.metadata.get("heartbeat_action") or "").strip()
            == "scene_opening"
        ) or (
            context.metadata.get("system_gm_beat_request")
            and context.metadata.get("gm_authored_free_scene_beat") is True
            and context.metadata.get("heartbeat_require_material_change") is True
            and str(context.metadata.get("heartbeat_action") or "").strip()
            == "free_scene_beat"
        ):
            names.add("commit_scene_response")

        if context.metadata.get("_gm_runtime_scene_state_known"):
            scene_active = bool(context.metadata.get("_gm_scene_active"))
            conflict_active = bool(context.metadata.get("_gm_conflict_active"))
            if scene_active:
                names.discard("start_scene")
            else:
                names.difference_update(
                    {"focus_scene_branch", "transition_scene", "end_scene"}
                )
            if conflict_active:
                names.difference_update(cls._SCENE_LIFECYCLE_TOOLS)
        return names

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
