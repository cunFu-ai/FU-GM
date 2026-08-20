from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_domain_tool_services_depend_on_contracts_not_agent_loop() -> None:
    tool_files = sorted((ROOT / "src" / "fu_gm").glob("gm_*_tools.py"))
    assert tool_files
    for path in tool_files:
        source = path.read_text(encoding="utf-8")
        assert "from fu_gm.gm_tool_agent import" not in source, path.name
        assert "from fu_gm.gm_tool_contracts import" in source, path.name


def test_typed_tool_registration_has_a_transport_independent_composition_root() -> None:
    suite = (
        ROOT / "src" / "fu_gm" / "components" / "gm_tool_suite.py"
    ).read_text(encoding="utf-8")
    http = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    assert "class GMToolSuite" in suite
    assert "GMToolStateTransactionFactory(host)" in suite
    assert "self.gm_tool_suite = GMToolSuite.build(self)" in http
    for service in (
        "GMCampaignToolService",
        "GMSessionZeroToolService",
        "GMSceneToolService",
        "GMClockToolService",
        "GMDiceToolService",
        "GMNPCToolService",
        "GMGameplayToolService",
        "GMMapToolService",
        "GMRuntimeToolService",
        "GMAdventureToolService",
        "GMReferenceToolService",
    ):
        assert service not in http


def test_contract_layer_has_no_llm_or_http_dependency() -> None:
    source = (ROOT / "src" / "fu_gm" / "gm_tool_contracts.py").read_text(
        encoding="utf-8"
    )
    assert "llm_client" not in source
    assert "http_server" not in source
    assert "gm_tool_agent" not in source


def test_structured_player_turn_never_reenters_legacy_semantic_recovery() -> None:
    source = (
        ROOT / "src" / "fu_gm" / "components" / "structured_turn_executor.py"
    ).read_text(encoding="utf-8")
    assert "_recover_" not in source
    assert ".action_brain." not in source
    assert "interceptor.resolve(action)" in source


def test_typed_gameplay_tools_use_structured_not_legacy_turn_entry() -> None:
    source = (ROOT / "src" / "fu_gm" / "gm_gameplay_tools.py").read_text(
        encoding="utf-8"
    )
    assert "run_structured_turn(" in source
    assert ".run_turn(" not in source


def test_action_round_progression_lives_in_narrow_coordinators() -> None:
    conflict = (
        ROOT
        / "src"
        / "fu_gm"
        / "components"
        / "conflict_action_round_coordinator.py"
    ).read_text(encoding="utf-8")
    free_scene = (
        ROOT
        / "src"
        / "fu_gm"
        / "components"
        / "scene_action_round_coordinator.py"
    ).read_text(encoding="utf-8")
    assert 'event_timing="action_round_end"' in conflict
    assert 'event_timing="action_round_end"' in free_scene
    assert "record_action(" in free_scene


def test_typed_npc_tools_never_reenter_action_brain() -> None:
    source = (ROOT / "src" / "fu_gm" / "gm_npc_tools.py").read_text(
        encoding="utf-8"
    )
    assert ".action_brain" not in source
    assert "npc_decision_planner" not in source
    assert "public_segments" in source
    assert "不会再次调用NPC模型" in source


def test_live_natural_language_has_one_authoritative_core_agent() -> None:
    http_source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    route_start = http_source.index("def _message_route(")
    route_end = http_source.index(
        "def _player_character_control_map(",
        route_start,
    )
    route_block = http_source[route_start:route_end]
    assert "return self.gm_natural_message_router.route(payload)" in route_block
    assert "message_arbiter" not in route_block
    assert "semantic_preflight" not in route_block
    assert "compatibility" not in route_block

    router = (
        ROOT / "src" / "fu_gm" / "components" / "gm_natural_message_router.py"
    ).read_text(encoding="utf-8")
    fail_closed = router.index('"route": "gm_agent_fail_closed"')
    assert "single_agent_path" in router[fail_closed : fail_closed + 1600]
    assert "envelope.current_message" in router
    assert "envelope.routing_payload(primary_payload)" in router
    assert 'routing_payload["current_turn_events"]' in router
    assert "message_arbiter" not in router
    assert "semantic_preflight" not in router


def test_buffered_group_messages_use_a_narrow_actor_preserving_router() -> None:
    http_source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    router = (
        ROOT
        / "src"
        / "fu_gm"
        / "components"
        / "gm_batched_message_router.py"
    ).read_text(encoding="utf-8")
    assert "self.gm_batched_message_router.route(payload, raw_batch)" in http_source
    assert "item_payload[\"speaker\"]" in router
    assert "item_payload[\"message\"]" in router
    assert 'primary["current_turn_messages"] = turn_messages' in router
    assert '"single_semantic_turn"' in router
    contracts = (
        ROOT / "src" / "fu_gm" / "gm_tool_contracts.py"
    ).read_text(encoding="utf-8")
    assert '"SOURCE_EVENT_REQUIRED"' in contracts
    assert '"source_event_id"' in contracts
    assert "message_arbiter" not in router
    assert "semantic_preflight" not in router
    assert "ActionBrain" not in router


def test_typed_agent_stack_never_uses_legacy_route_summary_as_authority() -> None:
    paths = [
        ROOT / "src" / "fu_gm" / "gm_tool_agent.py",
        ROOT / "src" / "fu_gm" / "gm_tool_execution.py",
        ROOT / "src" / "fu_gm" / "gm_tool_protocol.py",
        ROOT / "src" / "fu_gm" / "gm_npc_tools.py",
        ROOT / "src" / "fu_gm" / "components" / "gm_agent_message_coordinator.py",
        ROOT / "src" / "fu_gm" / "components" / "gm_natural_message_router.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "semantic_route_decision" not in source, path.name


def test_runtime_has_no_second_semantic_authority() -> None:
    runtime_paths = [
        ROOT / "src" / "fu_gm" / "gm_tool_agent.py",
        ROOT / "src" / "fu_gm" / "gm_tool_execution.py",
        ROOT / "src" / "fu_gm" / "gm_tool_contracts.py",
        ROOT / "src" / "fu_gm" / "gm_npc_tools.py",
        ROOT / "src" / "fu_gm" / "components" / "npc_combat_rules.py",
        ROOT / "src" / "fu_gm" / "components" / "gm_agent_runtime.py",
    ]
    forbidden = (
        "semantic_call_auditor",
        "semantic_terminal_auditor",
        "semantic_side_effect_guard",
        "semantic_profile",
        "GMToolSemanticGuardVerdict",
        "GMTerminalDecisionVerdict",
        "NPCPlayerResponseManager",
    )
    for path in runtime_paths:
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path.name}: {token}"

    removed_modules = (
        "npc_voice.py",
        "npc_bargain_semantic_reviewer.py",
        "npc_contract_semantic_reviewer.py",
        "npc_fidelity_reviewer.py",
        "scene_beat_fidelity_reviewer.py",
        "session_zero_update_semantic_reviewer.py",
        "gm_terminal_decision_semantic_auditor.py",
    )
    for name in removed_modules:
        assert not (ROOT / "src" / "fu_gm" / "components" / name).exists()
        assert not (ROOT / "src" / "fu_gm" / name).exists()


def test_npc_voice_renderer_is_expression_only_and_cannot_write_game_state() -> None:
    source = (
        ROOT / "src" / "fu_gm" / "components" / "npc_voice_renderer.py"
    ).read_text(encoding="utf-8")

    assert "content_segments" in source
    assert "NPC_VOICE_AUDIT_SYSTEM_PROMPT" in source
    for forbidden in (
        "WorldState",
        "SceneManager",
        "CharacterManager",
        "GMToolRegistry",
        "GMToolReceipt",
        "update_npc_state",
        "remember_npc_event",
        "record_condition",
        "record_settled_exchange",
    ):
        assert forbidden not in source


def test_active_agent_components_do_not_depend_on_legacy_semantic_stack() -> None:
    paths = [
        ROOT / "src" / "fu_gm" / "gm_tool_agent.py",
        *(ROOT / "src" / "fu_gm" / "components").glob("gm_agent_*.py"),
        ROOT / "src" / "fu_gm" / "components" / "gm_natural_message_router.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "from fu_gm.action_brain import",
            "from fu_gm.message_arbiter import",
            "from fu_gm.scene_orchestrator import",
            "from fu_gm.http_server import",
        ):
            assert forbidden not in source, f"{path.name}: {forbidden}"


def test_active_agent_loop_stays_below_previous_monolithic_size() -> None:
    path = ROOT / "src" / "fu_gm" / "gm_tool_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    assert run.end_lineno - run.lineno + 1 < 250
    handlers = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("_handle_")
    }
    assert handlers
    assert max(handlers.values()) < 160


def test_typed_message_coordinator_has_no_legacy_router_dependency() -> None:
    source = (
        ROOT / "src" / "fu_gm" / "components" / "gm_agent_message_coordinator.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "message_arbiter",
        "LLMMessageArbiter",
        "MessageRouteDecision",
        "RoutePolicyContext",
        "semantic_preflight",
    ):
        assert forbidden not in source


def test_legacy_message_router_is_absent_from_runtime_source() -> None:
    source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "LegacyMessageArbiterRuntime",
        "LegacyMessageRouteAdapter",
        "legacy_message_route_adapter",
        "self.message_arbiter",
        "semantic_preflight",
    ):
        assert forbidden not in source
    assert not (
        ROOT
        / "src"
        / "fu_gm"
        / "components"
        / "legacy_message_route_adapter.py"
    ).exists()
    assert not (
        ROOT
        / "src"
        / "fu_gm"
        / "components"
        / "legacy_message_arbiter_runtime.py"
    ).exists()


def test_natural_language_decision_compatibility_routers_are_removed() -> None:
    for name in (
        "decision_response_router.py",
        "pending_decision_message_router.py",
        "gm_beat_generation_policy.py",
    ):
        assert not (ROOT / "src" / "fu_gm" / "components" / name).exists()

    orchestrator = (ROOT / "src" / "fu_gm" / "scene_orchestrator.py").read_text(
        encoding="utf-8"
    )
    assert "DecisionResponseRouter" not in orchestrator
    assert "PendingDecisionMessageRouter" not in orchestrator
    for retired_intent_parser in (
        "_apply_session_zero_creation_intent",
        "_looks_like_confirm_hero_intent",
        "_looks_like_create_hero_intent",
        "_actor_from_recent_chat",
        "_enemy_target_from_chat",
    ):
        assert retired_intent_parser not in orchestrator

    envelope = (
        ROOT / "src" / "fu_gm" / "components" / "gm_message_envelope.py"
    ).read_text(encoding="utf-8")
    assert "compatibility_message" not in envelope
    assert "contextualized_message" not in envelope


def test_typed_tools_do_not_call_legacy_semantic_scene_entries() -> None:
    paths = [
        *(ROOT / "src" / "fu_gm").glob("gm_*_tools.py"),
        ROOT / "src" / "fu_gm" / "components" / "structured_turn_executor.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for forbidden in ("run_turn(", "run_gm_beat(", "run_scene_opening("):
            assert forbidden not in source, f"{path.name}: {forbidden}"


def test_rule_handlers_do_not_reclassify_natural_language_authority() -> None:
    campaign = (ROOT / "src" / "fu_gm" / "gm_campaign_tools.py").read_text(
        encoding="utf-8"
    )
    session_zero = (
        ROOT / "src" / "fu_gm" / "gm_session_zero_tools.py"
    ).read_text(encoding="utf-8")
    gameplay = (ROOT / "src" / "fu_gm" / "gm_gameplay_tools.py").read_text(
        encoding="utf-8"
    )
    npc = (ROOT / "src" / "fu_gm" / "gm_npc_tools.py").read_text(
        encoding="utf-8"
    )
    assert "campaign_id not in current_message" not in campaign
    assert "slot not in current_message" not in campaign
    assert "_draft_ownership_error" not in session_zero
    assert "HERO_SKILL_NOT_SUPPORTED_BY_MESSAGE" not in session_zero
    assert "compact_evidence" not in session_zero
    assert "actor not in current_message" not in gameplay
    assert "persona.name in message_context" not in npc
    assert "alias in message_context" not in npc
    assert "_established_collective_evidence" not in npc
    assert "SequenceMatcher" not in npc


def test_explicit_chat_endpoint_is_an_alias_of_the_single_router() -> None:
    source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _chat(")
    end = source.index("def _message_route(", start)
    block = source[start:end]
    assert "response = self._message_route(routed_payload)" in block
    assert "resolved_mode" not in block
    assert "action_brain" not in block


def test_explicit_session_zero_message_is_an_alias_of_the_single_router() -> None:
    source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _session_zero_message(")
    end = source.index("def _end_session(", start)
    block = source[start:end]
    assert "response = self._message_route(routed_payload)" in block
    assert "discuss_session_zero" not in block
    assert "session_zero_facilitator" not in block


def test_system_gm_beat_is_a_trusted_envelope_not_a_fake_player_mention() -> None:
    source = (ROOT / "src" / "fu_gm" / "http_server.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _invoke_system_gm_agent(")
    end = source.index("def _format_turn_input(", start)
    block = source[start:end]
    assert '"system_gm_beat_request": True' in block
    assert '"force_gm_reply": True' not in block
    assert "explicitly_addressed=False" in block


def test_astrbot_reports_group_arrival_before_command_or_route_filtering() -> None:
    source = (
        ROOT
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "main.py"
    ).read_text(encoding="utf-8")
    passive_start = source.index("async def passive_prefix_router(")
    passive_end = source.index("async def _command_payload(", passive_start)
    passive = source[passive_start:passive_end]
    assert passive.index("await self._mark_channel_activity(event)") < passive.index(
        "if not self._natural_routing_enabled_for(event, raw):"
    )
    assert passive.index("await self._mark_channel_activity(event)") < passive.index(
        "await self._should_buffer_natural_message(event, raw)"
    )

    mark_start = source.index("async def _mark_channel_activity(")
    mark_end = source.index("def _has_channel_sender(", mark_start)
    mark = source[mark_start:mark_end]
    assert "await self._report_message_activity(" in mark
    assert "if self._is_private_event(event):\n            return" not in mark

    report_start = source.index("async def _report_message_activity(")
    report_end = source.index("async def _route_natural_turn(", report_start)
    report = source[report_start:report_end]
    assert '"is_private": bool(payload.get("is_private"))' in report

    command_start = source.index("async def _command_payload(")
    command_end = source.index("def _payload(", command_start)
    command = source[command_start:command_end]
    assert command.index("await self._mark_channel_activity(event)") < command.index(
        "return self._payload("
    )


def test_mutating_gameplay_adventure_and_runtime_tools_share_one_transaction_boundary() -> None:
    coordinator = (
        ROOT / "src" / "fu_gm" / "components" / "campaign_state_transaction.py"
    )
    assert coordinator.exists()
    for name in ("gm_gameplay_tools.py", "gm_adventure_tools.py", "gm_runtime_tools.py"):
        source = (ROOT / "src" / "fu_gm" / name).read_text(encoding="utf-8")
        assert "CampaignStateTransaction" in source, name
        assert ".memory_store.build_snapshot(" not in source, name
        assert ".memory_store.apply_snapshot(" not in source, name


def test_structured_turn_carries_exact_player_message_without_text_extraction() -> None:
    orchestrator = (ROOT / "src" / "fu_gm" / "scene_orchestrator.py").read_text(
        encoding="utf-8"
    )
    executor = (
        ROOT / "src" / "fu_gm" / "components" / "structured_turn_executor.py"
    ).read_text(encoding="utf-8")
    publisher = (
        ROOT / "src" / "fu_gm" / "components" / "resolved_turn_publisher.py"
    ).read_text(encoding="utf-8")
    gameplay = (ROOT / "src" / "fu_gm" / "gm_gameplay_tools.py").read_text(
        encoding="utf-8"
    )

    assert "player_message: str" in orchestrator
    assert "recent_public_context: str = \"\"" in orchestrator
    assert "player_message=clean_player_message" in executor
    assert "player_message=str(player_message or \"\").strip()" in publisher
    assert "_current_player_chat" not in publisher
    assert "recent_public_context=recent_context" in gameplay
    assert "speaker=context.speaker" in gameplay


def test_core_gm_owns_npc_dialogue_and_combat_choice() -> None:
    root = ROOT / "src" / "fu_gm"
    assert not (root / "npc_director.py").exists()
    assert not (root / "components" / "npc_decision_planner.py").exists()

    combat_rules = (root / "components" / "npc_combat_rules.py").read_text(
        encoding="utf-8"
    )
    turn_executor = (root / "components" / "npc_turn_executor.py").read_text(
        encoding="utf-8"
    )
    npc_tools = (root / "gm_npc_tools.py").read_text(encoding="utf-8")
    for source in (combat_rules, turn_executor, npc_tools):
        assert "create_chat_completion" not in source
        assert "OpenAICompatibleClient" not in source
    assert "def decide(" not in combat_rules
    assert "action_description" in combat_rules
    assert "public_segments" in npc_tools
