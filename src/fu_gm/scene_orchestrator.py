from __future__ import annotations

from copy import deepcopy
import re
import threading
from pathlib import Path

from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.clock_lifecycle_coordinator import ClockLifecycleCoordinator
from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.components.campaign_pacing_manager import CampaignPacingManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.conflict_action_round_coordinator import ConflictActionRoundCoordinator
from fu_gm.components.ally_npc_manager import AllyNPCManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.hero_log_manager import HeroLogManager
from fu_gm.components.loyal_companion_manager import LoyalCompanionManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.narrative_memory_writer import NarrativeMemoryWriter
from fu_gm.components.npc_continuity_policy import NPCCommitmentBoundary
from fu_gm.components.npc_response_window_manager import NPCResponseWindowManager
from fu_gm.components.npc_turn_executor import NPCTurnExecutor
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.resolution_commit_coordinator import ResolutionCommitCoordinator
from fu_gm.components.resolved_turn_publisher import ResolvedTurnPublisher
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.safety_manager import SafetyManager
from fu_gm.components.scene_frame_manager import SceneFrameManager
from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.components.scene_action_outcome_policy import SceneActionOutcomePolicy
from fu_gm.components.scene_access_boundary import SceneAccessBoundary
from fu_gm.components.scene_action_round_coordinator import SceneActionRoundCoordinator
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_lifecycle_coordinator import SceneLifecycleCoordinator
from fu_gm.components.scene_transition_coordinator import SceneTransitionCoordinator
from fu_gm.components.session_ledger import SessionLedger
from fu_gm.components.session_episode_tracker import SessionEpisodeTracker
from fu_gm.components.solo_play_manager import SoloPlayManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.sheet_exporter import SheetExporter
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.structured_turn_executor import StructuredTurnExecutor
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.components.turn_response_renderer import TurnResponseRenderer
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.trigger_manager import TriggerManager
from fu_gm.components.world_map_image_manager import WorldMapImageManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Narrator
from fu_gm.gm_guidance import summarize_guidance_for_prompt
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    DungeonExploreMode,
    EnemyRank,
    GMStyleProfile,
    GamePanel,
    CampaignCreationBundle,
    CharacterCreationResult,
    HeroDraftValidationResult,
    HeroCreationProfile,
    MemoryVisibility,
    PersistentChangeType,
    ProjectProgressResult,
    ProjectState,
    ProjectUse,
    RestResult,
    RestType,
    RitualCastResult,
    RitualDiscipline,
    RitualPlan,
    RitualPotency,
    RitualScope,
    SafetyDeclarationResult,
    SceneRecord,
    SceneType,
    SessionExperienceReport,
    SessionZeroResponse,
    SessionZeroTurn,
    SheetExportBundle,
    SpellEffectType,
    LevelUpResult,
    StatusEffect,
    TravelRouteType,
    TravelThreatLevel,
)
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.npc_blueprint_designer import NPCBlueprintDesigner
from fu_gm.components.npc_blueprint_compiler import NPCBlueprintCompiler
from fu_gm.components.npc_voice_renderer import NPCVoiceRenderer
from fu_gm.components.scene_creative_writer import SceneCreativeWriter
from fu_gm.optional_rules import format_optional_rules_for_prompt
from fu_gm.play_process_guidance import summarize_play_process_for_prompt
from fu_gm.skill_library import (
    has_skill_name,
    normalize_skill_reference_name,
    skill_rank,
)
from fu_gm.spellbook import get_spell_definition
from fu_gm.turn_pipeline import (
    TurnReplyPipeline,
    TurnReplyStage,
)


class SceneOrchestrator:
    _TURN_CONSUMING_ACTIONS = {
        ActionType.ATTACK,
        ActionType.SPELL,
        ActionType.GUARD,
        ActionType.EQUIP,
        ActionType.HINDER,
        ActionType.INVESTIGATE,
        ActionType.OBJECTIVE,
        ActionType.SKILL,
        ActionType.USE_INVENTORY,
        ActionType.TINKERER_GADGET,
        ActionType.OPEN_CHEST,
        ActionType.EXPLORE_DUNGEON,
        ActionType.REQUEST_ROLL,
        ActionType.PLAN_RITUAL,
        ActionType.CONTRIBUTE_RITUAL,
        ActionType.CAST_RITUAL,
        ActionType.NPCACT,
        ActionType.ABSENT_PLAYER,
    }
    _DEFINITE_CHECK_ACTIONS = {
        ActionType.HINDER,
        ActionType.INVESTIGATE,
        ActionType.OBJECTIVE,
        ActionType.REQUEST_ROLL,
        ActionType.CONTRIBUTE_RITUAL,
    }
    # These active skill handlers resolve an attack, ordinary check, or
    # opposed check made by the skill user. Other implemented skill actions
    # are automatic/fixed effects, react to an earlier check, or delegate the
    # roll to a companion, so they must not consume this leader's assistance.
    _CHECK_SKILL_ACTIONS = {
        "暗影击",
        "摧心重击",
        "挑衅",
        "谴责",
        "窃取灵魂",
        "碎骨",
        "威慑射击",
        "破防打击",
        "弹幕射击",
        "利刃风暴",
    }

    def __init__(
        self,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
        interceptor: ActionInterceptor,
        expressor: Narrator,
        llm_client: object | None = None,
        llm_model: str = "",
        creative_client: object | None = None,
        creative_model: str = "",
        deepseek_roleplay_mode: str = "default",
        semantic_review_client: object | None = None,
        semantic_review_model: str = "",
        npc_combat_rules: NPCCombatRules | None = None,
        npc_blueprint_designer: NPCBlueprintDesigner | None = None,
        npc_voice_renderer: NPCVoiceRenderer | None = None,
        scene_manager: SceneManager | None = None,
        session_zero_manager: SessionZeroManager | None = None,
        character_creation_manager: CharacterCreationManager | None = None,
        rest_manager: RestManager | None = None,
        travel_manager: TravelManager | None = None,
        dungeon_manager: DungeonManager | None = None,
        sheet_exporter: SheetExporter | None = None,
        safety_manager: SafetyManager | None = None,
        ritual_manager: RitualManager | None = None,
        project_manager: ProjectManager | None = None,
        progression_manager: ProgressionManager | None = None,
        memory_store: CampaignMemoryStore | None = None,
        topic_memory_store: TopicMemoryStore | None = None,
        story_arc_manager: StoryArcManager | None = None,
        scene_frame_manager: SceneFrameManager | None = None,
        hero_log_manager: HeroLogManager | None = None,
        ally_npc_manager: AllyNPCManager | None = None,
        campaign_id: str = "default",
        trigger_manager: TriggerManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        world_map_image_manager: WorldMapImageManager | None = None,
        session_ledger: SessionLedger | None = None,
        gm_beat_timeout_seconds: float = 45.0,
        session_prep_timeout_seconds: float = 60.0,
    ) -> None:
        self.character_manager = character_manager
        self.clock_manager = clock_manager
        self.clock_lifecycle = ClockLifecycleCoordinator(clock_manager)
        self.conflict_manager = conflict_manager
        self.world_state = world_state
        self.interceptor = interceptor
        self.expressor = expressor
        self.llm_client = llm_client
        self.llm_model = str(llm_model or "").strip()
        self.creative_client = creative_client if creative_client is not None else llm_client
        self.creative_model = str(creative_model or "").strip() or self.llm_model
        self.semantic_review_client = (
            semantic_review_client
            if semantic_review_client is not None
            else llm_client
        )
        self.semantic_review_model = (
            str(semantic_review_model or "").strip() or self.llm_model
        )
        self.scene_creative_writer = SceneCreativeWriter(
            client=self.creative_client,
            model=self.creative_model,
            audit_client=self.semantic_review_client,
            audit_model=self.semantic_review_model,
            deepseek_roleplay_mode=deepseek_roleplay_mode,
        )
        self.authoritative_tool_writes_enabled = True
        self.gm_beat_timeout_seconds = max(1.0, float(gm_beat_timeout_seconds))
        self.last_gm_beat_diagnostics: list[dict[str, object]] = []
        self.last_gm_beat_fidelity_diagnostics: list[dict[str, object]] = []
        self.npc_combat_rules = npc_combat_rules
        self.npc_voice_renderer = npc_voice_renderer
        self.npc_blueprint_designer = npc_blueprint_designer or NPCBlueprintDesigner(
            world_state,
            client=None,
            model="",
            current_scene_id=lambda: str(
                getattr(self.scene_manager.current_scene, "scene_id", "")
                or getattr(self.scene_manager.current_scene, "name", "")
                or ""
            ),
        )
        self.scene_manager = scene_manager or SceneManager()
        # Scene membership is one shared source of truth.  The rules layer and
        # its persisted spell-choice windows must not keep a detached manager,
        # otherwise present narrative NPCs disappear between parsing and
        # transaction validation.
        self.interceptor.scene_manager = self.scene_manager
        self.interceptor.spell_parameter_manager.scene_manager = self.scene_manager
        self.interceptor.post_check_decisions.scenes = self.scene_manager
        self.loyal_companion_manager = LoyalCompanionManager(
            self.character_manager,
            self.conflict_manager,
            self.scene_manager,
            self.world_state,
        )
        self.interceptor.loyal_companion_manager = self.loyal_companion_manager
        self.conflict_manager.bind_loyal_companion_manager(
            self.loyal_companion_manager
        )
        self.session_zero_manager = session_zero_manager or SessionZeroManager(world_state)
        self.character_creation_manager = character_creation_manager or CharacterCreationManager(
            character_manager,
            world_state,
        )
        self.session_zero_manager.bind_hero_validator(
            self.character_creation_manager.validate_hero_draft_for_session_zero
        )
        self.rest_manager = rest_manager or RestManager(character_manager, clock_manager)
        self.travel_manager = travel_manager
        self.dungeon_manager = dungeon_manager or DungeonManager(clock_manager)
        self.sheet_exporter = sheet_exporter or SheetExporter()
        self.safety_manager = safety_manager or SafetyManager(world_state)
        self.ritual_manager = ritual_manager or RitualManager(interceptor.rules_engine, character_manager, clock_manager)
        self.project_manager = project_manager or ProjectManager(character_manager)
        self.progression_manager = progression_manager or ProgressionManager(character_manager, world_state)
        self.memory_store = memory_store or CampaignMemoryStore()
        self.topic_memory_store = topic_memory_store or TopicMemoryStore(self.memory_store.root)
        self.narrative_memory_writer = NarrativeMemoryWriter(
            topics=self.topic_memory_store,
            world=self.world_state,
            characters=self.character_manager,
            scenes=self.scene_manager,
        )
        self.story_arc_manager = story_arc_manager or StoryArcManager(world_state, clock_manager)
        self.campaign_pacing_manager = CampaignPacingManager(
            self.story_arc_manager,
            clock_manager,
            world_state,
            character_manager=self.character_manager,
            client=self.creative_client,
            model=self.creative_model,
            review_client=self.semantic_review_client,
            review_model=self.semantic_review_model,
            session_prep_timeout_seconds=session_prep_timeout_seconds,
        )
        self.session_ledger = session_ledger or SessionLedger()
        self.scene_action_rounds = SceneActionRoundCoordinator(
            scenes=self.scene_manager,
            characters=self.character_manager,
            world=self.world_state,
            conflicts=self.conflict_manager,
            clocks=self.clock_manager,
            pacing=self.campaign_pacing_manager,
            clock_lifecycle=self.clock_lifecycle,
            session_ledger=self.session_ledger,
        )
        self.session_episode_tracker = SessionEpisodeTracker(
            self.campaign_pacing_manager,
            self.character_manager,
        )
        self.scene_frame_manager = scene_frame_manager or SceneFrameManager(
            session_ledger=self.session_ledger
        )
        self.scene_frame_manager.session_ledger = self.session_ledger
        self.scene_action_outcome_policy = SceneActionOutcomePolicy()
        self.npc_response_windows = NPCResponseWindowManager()
        self.scene_transition_coordinator = SceneTransitionCoordinator()
        self.turn_response_renderer = TurnResponseRenderer()
        self.hero_log_manager = hero_log_manager or HeroLogManager()
        self.ally_npc_manager = ally_npc_manager or AllyNPCManager()
        self.campaign_id = campaign_id or "default"
        self._surfaced_topic_memory_paths: set[str] = set()
        self.trigger_manager = trigger_manager or TriggerManager(character_manager)
        self.world_map_manager = world_map_manager
        self.world_map_image_manager = world_map_image_manager
        self.solo_play_manager = SoloPlayManager(character_manager, world_state)
        self.resolution_committer = ResolutionCommitCoordinator(
            clocks=self.clock_lifecycle,
            memories=self.narrative_memory_writer,
            topics_provider=lambda: self.topic_memory_store,
            scenes=self.scene_manager,
            frame_provider=lambda: self.scene_frame_manager,
            campaign_id_provider=lambda: self.campaign_id,
        )
        self.turn_reply_pipeline = self._build_turn_reply_pipeline()
        self.structured_turn_executor = StructuredTurnExecutor(self)
        self.resolved_turn_publisher = ResolvedTurnPublisher(self)
        self.npc_turn_executor = NPCTurnExecutor(self)
        self._world_map_generation_thread: threading.Thread | None = None
        self._world_map_generation_status: dict[str, object] = {"status": "idle", "attempts": 0}
        self.recent_pipeline_spans: list[dict[str, object]] = []
        self.interceptor.ritual_manager = self.ritual_manager
        self.interceptor.project_manager = self.project_manager
        self.interceptor.dungeon_manager = self.dungeon_manager
        self.interceptor.trigger_manager = self.trigger_manager
        self.interceptor.rest_manager = self.rest_manager
        self.interceptor.reveal_motivation_provider = self._scene_motivation_for_target
        self.decision_window_manager = self.interceptor.decision_window_manager
        self.conflict_action_rounds = ConflictActionRoundCoordinator(
            conflicts=self.conflict_manager,
            decisions=self.decision_window_manager,
            clocks=self.clock_manager,
            pacing=self.campaign_pacing_manager,
            clock_changes=self.scene_action_rounds,
            is_turn_consuming=self._is_turn_consuming_action,
            is_boss_scene=self._is_boss_pressure_scene,
            held_action_notice=self._held_action_notice,
        )
        self._turn_start_clock_changes: dict[int, list[object]] = {}
        self.conflict_manager.register_turn_start_listener(
            self._on_clock_turn_start,
        )
        self.scene_lifecycle = SceneLifecycleCoordinator(
            clocks=self.clock_manager,
            decisions=self.decision_window_manager,
            conflict=self.conflict_manager,
            characters=self.character_manager,
            world_state=self.world_state,
            skills=self.interceptor.skill_trigger_manager,
            episodes=self.session_episode_tracker,
            rituals=self.ritual_manager,
        )
        self.scene_access_boundary = SceneAccessBoundary()
        self.character_manager.register_resource_listener(self.session_ledger.record_resource_change)
        self.conflict_manager.register_ultima_spend_listener(self.session_ledger.record_ultima_spent)
        self.scene_manager.register_lifecycle_listener(
            on_start=self._on_scene_started,
            on_end=self._on_scene_ended,
            on_focus=self._on_scene_focused,
            on_enter=self._on_scene_participants_entered,
        )
        if self.scene_manager.current_scene is not None:
            self._on_scene_started(self.scene_manager.current_scene)
        self.story_arc_manager.sync_from_world_profile()

    def _on_scene_started(self, scene: SceneRecord) -> None:
        # A newly opened split-party branch becomes the authoritative camera.
        # Park the previous branch frame before any tool starts preparing the
        # destination, otherwise synchronize_current_location would mutate the
        # old frame while leaving its source_scene_id behind.
        current = self.scene_frame_manager.current_frame
        scene_id = str(scene.scene_id or "").strip()
        if current is not None and str(current.source_scene_id or "").strip() != scene_id:
            self.scene_frame_manager.suspend_current_frame()
        if self.scene_frame_manager.current_frame is None:
            self.scene_frame_manager.restore_suspended_frame(scene)
        self.scene_lifecycle.start(scene)
        self.loyal_companion_manager.sync_scene(scene, scene_started=True)
        # GM工具代理不经过旧Expressor的私有场景包路径。开场演员若只在
        # render_scene_moment时准备，第一条代理回复就可能另造一个功能相同
        # 的NPC。场景生命周期在任何公开叙事前先落实契约要求的演员与档案。
        dramatic_contract = self._current_dramatic_contract()
        if dramatic_contract is not None:
            self.scene_frame_manager.ensure_frame(
                scene=scene,
                recent_chat="",
                world_state=self.world_state,
                character_manager=self.character_manager,
                contract=dramatic_contract,
            )
            self._ensure_required_opening_npc_personas()
        self.world_state.sync_carried_story_item_locations(
            {
                participant: str(
                    scene.participant_locations.get(participant)
                    or scene.location
                    or ""
                )
                for participant in scene.participants
            },
            source="SceneOrchestrator.scene_started",
        )

    def _on_scene_ended(self, scene: SceneRecord) -> None:
        self.scene_lifecycle.end(scene)
        self.scene_frame_manager.archive_scene(scene.scene_id or scene.name)

    def _on_scene_focused(self, scene: SceneRecord) -> None:
        self.scene_lifecycle.focus(scene)
        self.loyal_companion_manager.sync_scene(scene, scene_started=False)
        current = self.scene_frame_manager.current_frame
        scene_id = str(scene.scene_id or "").strip()
        if current is not None and str(current.source_scene_id or "").strip() != scene_id:
            self.scene_frame_manager.suspend_current_frame()
        if self.scene_frame_manager.current_frame is None:
            self.scene_frame_manager.restore_suspended_frame(scene)

    def _on_scene_participants_entered(
        self,
        scene: SceneRecord,
        participants: list[str],
    ) -> None:
        self.scene_lifecycle.enter(scene, participants)
        self.world_state.sync_carried_story_item_locations(
            {
                participant: str(
                    scene.participant_locations.get(participant)
                    or scene.location
                    or ""
                )
                for participant in participants
            },
            source="SceneOrchestrator.scene_participants_entered",
        )

    def build_panel(self, recent_chat: str) -> GamePanel:
        pcs = [c for c in self.character_manager.all() if "pc" in c.traits]
        enemies = [c for c in self.character_manager.all() if "enemy" in c.traits]
        phase = self.conflict_manager.format_phase()
        if not self.conflict_manager.state.active:
            phase = self.scene_manager.format_phase()
        frame = self.scene_frame_manager.ensure_frame(
            scene=self.scene_manager.current_scene,
            recent_chat=recent_chat,
            world_state=self.world_state,
            character_manager=self.character_manager,
            contract=self._current_dramatic_contract(),
        )
        scene = self.scene_manager.current_scene
        if scene is not None and any(
            str(name or "").strip()
            and str(name or "").strip() not in scene.participants
            for name in frame.required_opening_npc_names
        ):
            # Saved scenes created before opening-cast reconciliation may have
            # a prepared NPC in the selected situation but not in the durable
            # participant roster. Repair that invariant before a player action
            # asks the rule layer to target or protect the NPC.
            self._ensure_required_opening_npc_personas()
        memory_context = self._retrieve_memory_context(recent_chat)
        return GamePanel(
            game_phase=phase,
            active_clocks=self.campaign_pacing_manager.prompt_clock_context(),
            pc_status=[self.character_manager.format_status(c) for c in pcs],
            enemy_status=[self.character_manager.format_status(c) for c in enemies],
            recent_chat=recent_chat,
            current_actor=self.conflict_manager.state.current_actor(),
            table_status=self.world_state.format_attendance(),
            safety_guidance=self.safety_manager.render_guidance(),
            optional_rules_guidance=format_optional_rules_for_prompt(self.world_state.world_profile),
            retrieved_public_memory=memory_context["public"],
            gm_private_memory=memory_context["private"],
            memory_guidance=memory_context["guidance"],
        )

    def set_campaign_id(self, campaign_id: str) -> None:
        campaign_id = campaign_id or "default"
        if campaign_id != self.campaign_id:
            self._surfaced_topic_memory_paths.clear()
        self.campaign_id = campaign_id

    def run_structured_turn(
        self,
        action: Action,
        player_message: str,
        *,
        recent_public_context: str = "",
        speaker: str = "",
        route_decision: dict[str, object] | None = None,
    ) -> str:
        """Execute one typed player action through the rules transaction."""

        return self.structured_turn_executor.execute(
            action,
            player_message=player_message,
            recent_public_context=recent_public_context,
            speaker=speaker,
            route_decision=route_decision,
        )

    def _complete_resolved_player_turn(
        self,
        *,
        player_message: str,
        recent_chat: str,
        route_decision: dict[str, object] | None,
        panel: GamePanel,
        action: Action,
        resolution: ActionResolution,
        recovery: list[dict[str, object]],
        span: dict[str, object],
        total_started: float,
    ) -> str:
        """Commit and publish one resolved player turn."""

        self.last_resolved_check_event_id = ""
        reply = self.resolved_turn_publisher.publish(
            player_message=player_message,
            recent_chat=recent_chat,
            route_decision=route_decision,
            panel=panel,
            action=action,
            resolution=resolution,
            recovery=recovery,
            span=span,
            total_started=total_started,
        )
        self.last_resolved_check_event_id = self._record_resolved_check_receipt(
            resolution,
            public_reply=reply,
        )
        return reply

    def _record_resolved_check_receipt(
        self,
        resolution: ActionResolution,
        *,
        public_reply: str,
    ) -> str:
        if resolution.payload.get("check_result_provisional"):
            return ""
        roll = resolution.payload.get("roll")
        if roll is None or not hasattr(roll, "success"):
            return ""
        existing = str(
            resolution.payload.get("_resolved_check_receipt_id") or ""
        ).strip()
        if existing:
            return existing
        committed = resolution.payload.get("committed_source_action")
        source_action = committed if isinstance(committed, Action) else resolution.action
        actor = str(source_action.parameters.get("actor") or "").strip()
        scene = self.scene_manager.current_scene
        payload = {
            "actor": actor,
            "action_type": source_action.action_type.value,
            "scene_id": str(getattr(scene, "scene_id", "") or ""),
            "scene_name": str(getattr(scene, "name", "") or ""),
            "target": str(source_action.parameters.get("target") or "").strip(),
            "purpose": str(
                source_action.parameters.get("declared_action_goal")
                or source_action.parameters.get("reasoning")
                or ""
            ).strip(),
            "check_label": str(
                source_action.parameters.get("scene_investigation_label") or ""
            ).strip(),
            "success": bool(getattr(roll, "success", False)),
            "critical_success": bool(getattr(roll, "critical_success", False)),
            "fumble": bool(getattr(roll, "fumble", False)),
            "total": int(getattr(roll, "total", 0) or 0),
            "target_number": int(getattr(roll, "target_number", 0) or 0),
            "dungeon_area": str(
                source_action.parameters.get("dungeon_area") or ""
            ).strip(),
            "success_state_changes": deepcopy(
                source_action.parameters.get("success_state_changes") or []
            ),
            "base_observation": str(
                source_action.parameters.get("base_observation") or ""
            ).strip(),
            "success_observation": str(
                source_action.parameters.get("success_observation") or ""
            ).strip(),
            "failure_consequence": str(
                source_action.parameters.get("failure_consequence") or ""
            ).strip(),
            "success_state_changes_applied": False,
            "consumed_by": [],
            "public_reply": str(public_reply or "").strip(),
        }
        outcome = "成功" if payload["success"] else "失败"
        event = self.world_state.record_memory_event(
            f"检定回执：{actor or '未指定角色'}的"
            f"【{payload['check_label'] or payload['action_type']}】{outcome}。",
            kind="resolved_check",
            visibility=MemoryVisibility.PRIVATE,
            entities=[
                item
                for item in (
                    actor,
                    str(payload["target"]),
                    str(payload["dungeon_area"]),
                )
                if item
            ],
            tags=["rules", "check_receipt", str(payload["action_type"])],
            source="SceneOrchestrator",
            payload=payload,
        )
        resolution.payload["_resolved_check_receipt_id"] = event.event_id
        return event.event_id

    def _current_scene_non_player_entities(self) -> list[str]:
        """Expose public scene people to semantic ownership checks.

        Scene preparation can mention a traveller, witness, crowd, or other
        in-world person before that person receives a durable NPC profile.
        Preserve the original public/planning wording and let the semantic
        reviewer decide identity; do not guess names with local regexes.
        """

        frame = self.scene_frame_manager.current_frame
        if frame is None:
            return []
        values = [
            *(str(item or "").strip() for item in frame.visible_elements),
            *(str(item or "").strip() for item in frame.npc_functions),
            *(
                str(item.get("name") or "").strip()
                for item in frame.session_npc_records
                if isinstance(item, dict)
            ),
        ]
        return list(dict.fromkeys(item for item in values if item))[:30]

    def _current_known_npc_names(self) -> list[str]:
        frame = self.scene_frame_manager.current_frame
        scene = self.scene_manager.current_scene
        values: list[str] = []
        for name in list(getattr(scene, "participants", []) or []):
            clean = str(name or "").strip()
            if clean and not self._is_player_character(clean):
                values.append(clean)
        if frame is not None:
            if frame.last_npc_speaker:
                values.append(frame.last_npc_speaker)
            for item in frame.session_npc_records:
                name = str(item.get("name") or "").strip()
                if name:
                    values.append(name)
            for item in frame.npc_functions:
                raw = str(item or "").strip()
                # npc_functions contains prose such as “值守者负责判断……”.
                # It is scene context, not a stable identity. Only the
                # explicitly structured ``Name: function`` form contributes a
                # name; unstructured prose remains available through
                # _current_scene_non_player_entities for semantic resolution.
                if "：" in raw or ":" in raw:
                    name = raw.split("：", 1)[0].split(":", 1)[0].strip()
                else:
                    name = ""
                if name:
                    values.append(name)
        return list(dict.fromkeys(values))[:20]

    def _transition_actor_for_turn(
        self,
        *,
        resolution: ActionResolution | None = None,
        route_decision: dict[str, object] | None,
    ) -> str:
        """Use the acting character, not the group-chat account, for movement."""

        route = dict(route_decision or {})
        candidates = (
            str(route.get("actor") or "").strip(),
            str(resolution.action.parameters.get("actor") or "").strip(),
        )
        for candidate in candidates:
            if candidate and self._is_player_character(candidate):
                return candidate
        return next((candidate for candidate in candidates if candidate), "")


    def run_scene_recap(self) -> str:
        """Render a public, no-change recap of the live scene.

        Reconnecting a group or resuming a long-running session needs a shared
        view of what is already on the table.  That is deliberately different
        from opening a new scene: requiring an LLM to introduce a fresh image,
        NPC move, or pressure change here both violates continuity and makes a
        harmless reconnect vulnerable to expression-model quality retries.

        The recap therefore uses only the public scene packet and has no state
        transition side effects.  It is still logged by the HTTP layer so the
        players and FU-PL receive the same public boundary afterwards.
        """

        packet = self._scene_expression_packet("", include_private=False)
        pending_question = self.scene_frame_manager.latest_pending_npc_question()
        if pending_question is not None:
            npc = str(pending_question.get("npc") or "对方").strip()
            actor = str(pending_question.get("addressed_actor") or "答话者").strip()
            summary = str(pending_question.get("summary") or "刚才的问题").strip()
            return f"{npc}还在等{actor}答清{summary}。"
        reply = SceneMomentPolicy.recap(packet)
        reply = self._sanitize_scene_opening_reply(reply)
        return self._ensure_complete_present_character_list(reply, packet)


    @staticmethod
    def _ensure_complete_present_character_list(reply: str, packet: dict[str, object]) -> str:
        return SceneMomentPolicy.ensure_complete_present_character_list(reply, packet)

    def _scene_expression_packet(self, recent_context: str, *, include_private: bool) -> dict[str, object]:
        self.scene_frame_manager.ensure_frame(
            scene=self.scene_manager.current_scene,
            recent_chat=recent_context,
            world_state=self.world_state,
            character_manager=self.character_manager,
            contract=self._current_dramatic_contract(),
        )
        if include_private:
            self._ensure_required_opening_npc_personas()
        packet = self.scene_frame_manager.expression_packet(
            active_clocks=self.clock_manager.formatted_public(),
            include_private=include_private,
        )
        packet["npc_statement_ledger"] = self._public_npc_statement_ledger()
        packet["npc_due_commitments"] = NPCCommitmentBoundary.due_commitments(
            packet["npc_statement_ledger"]
        )
        packet["clock_boundaries"] = ClockNarrativeBoundary.packet(self.clock_manager.all())
        return packet

    def _public_npc_statement_ledger(self) -> list[dict[str, object]]:
        """Build a small public-only ledger for scene-expression continuity."""

        frame = self.scene_frame_manager.current_frame
        scene = self.scene_manager.current_scene
        if frame is None:
            return []
        participant_names = {
            str(name or "").strip()
            for name in list(getattr(scene, "participants", []) or [])
            if str(name or "").strip()
        }
        participant_names.update(
            str(item.get("npc") or "").strip()
            for item in frame.open_conditions
            if str(item.get("npc") or "").strip()
        )
        location = str(frame.location or "").strip()
        scene_id = str(getattr(scene, "scene_id", "") or "").strip()
        ledger: list[dict[str, object]] = []
        for canonical, persona in self.world_state.npc_personas.items():
            aliases = [
                str(persona.public_identity or "").strip(),
                *(str(alias or "").strip() for alias in persona.aliases),
            ]
            known_here = bool(
                canonical in participant_names
                or any(alias in participant_names for alias in aliases if alias)
                or (
                    location
                    and self.scene_manager.locations_overlap(
                        persona.current_location,
                        location,
                    )
                )
                or (scene_id and persona.last_seen_scene == scene_id)
            )
            if not known_here:
                continue
            statements: list[str] = []
            for note in persona.memories[-12:]:
                clean = " ".join(str(note or "").split()).strip()
                if "我公开说过：" in clean:
                    clean = clean.split("我公开说过：", 1)[1].strip()
                elif "；我的答复：" in clean:
                    clean = clean.split("；我的答复：", 1)[1].strip()
                else:
                    continue
                if clean and clean not in statements:
                    statements.append(clean[:500])
            for condition in frame.open_conditions:
                condition_npc = str(condition.get("npc") or "").strip()
                resolved_name = self.world_state.resolve_npc_name(condition_npc) or condition_npc
                if resolved_name != canonical:
                    continue
                for key in ("condition", "promised_result"):
                    clean = " ".join(str(condition.get(key) or "").split()).strip()
                    if clean and clean not in statements:
                        statements.append(clean[:500])
            if statements:
                ledger.append(
                    {
                        "npc": canonical,
                        "public_identity": persona.public_identity,
                        "aliases": [alias for alias in aliases if alias],
                        "statements": statements[-8:],
                    }
                )
        return ledger[-12:]

    def _ensure_required_opening_npc_personas(self) -> None:
        frame = self.scene_frame_manager.current_frame
        if frame is None:
            return
        scene = self.scene_manager.current_scene
        scene_name = str(getattr(scene, "name", "") or frame.scene_name or "").strip()
        scene_id = str(getattr(scene, "scene_id", "") or frame.scene_key or "").strip()
        location = str(getattr(scene, "location", "") or frame.location or scene_name).strip()
        required_names = {
            str(name or "").strip()
            for name in frame.required_opening_npc_names
            if str(name or "").strip()
        }
        records = [
            dict(item)
            for item in frame.session_npc_records
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        recorded_names = {
            str(record.get("name") or "").strip()
            for record in records
        }
        for missing_name in required_names - recorded_names:
            records.append({"name": missing_name, "public_role": missing_name})

        for record in records:
            name = str(record.get("name") or "").strip()
            clean_name = str(name or "").strip()
            if not clean_name or self._is_player_character(clean_name):
                continue
            required_at_opening = any(
                self._scene_entity_alias_match(clean_name, required)
                for required in required_names
            )
            existing_persona = self.world_state.npc_personas.get(
                self.world_state.resolve_npc_name(clean_name) or clean_name
            )
            authoritative_location = str(
                self.scene_manager.location_of(clean_name)
                or getattr(existing_persona, "current_location", "")
                or ""
            ).strip()
            # A dramatic contract describes reusable session cast, not a
            # teleport order.  When a player opens a split branch, an NPC that
            # is already established in another active location stays there
            # unless a movement tool explicitly includes that NPC.
            present_at_opening = bool(
                required_at_opening
                and (
                    not authoritative_location
                    or self.scene_manager.locations_overlap(
                        authoritative_location,
                        location,
                    )
                )
            )
            public_role = str(record.get("public_role") or clean_name).strip()
            goal = str(record.get("goal_now") or "").strip()
            voice = str(record.get("voice_cue") or "").strip()
            secret = str(record.get("private_secret") or "").strip()
            authority = str(record.get("authority_scope") or "").strip()
            aliases = [public_role] if public_role and public_role != clean_name else []
            has_authored_profile = any(
                (goal, voice, secret, authority, str(record.get("public_role") or "").strip())
            )
            persona = self.world_state.ensure_npc_persona(
                clean_name,
                profile_status=(
                    "established" if has_authored_profile else "placeholder"
                ),
                aliases=aliases,
                public_identity=public_role,
                role_in_story="当前场景的在场人物",
                core_drive=goal,
                manner=voice,
                speech_style=voice,
                first_scene=scene_name,
                goals=[goal] if goal else [],
                secrets=[secret] if secret else [],
                custom_prompt=(f"自身权限范围：{authority}" if authority else ""),
                current_location=(location if present_at_opening else ""),
                active_goal=goal,
                last_seen_scene=(scene_id if present_at_opening else ""),
            )
            if present_at_opening:
                self.scene_manager.add_participant(clean_name)
            record["persona_id"] = persona.npc_id
            for stored in frame.session_npc_records:
                if self._scene_entity_alias_match(clean_name, str(stored.get("name") or "")):
                    stored["persona_id"] = persona.npc_id
            self._prewarm_npc_combat_blueprint(persona)

    def _prewarm_npc_combat_blueprint(self, persona) -> None:
        """Queue private inheritance design without blocking scene speech."""

        defaults = self._npc_blueprint_defaults(persona)
        self.npc_blueprint_designer.submit(
            persona,
            level=defaults["level"],
            species="",
            rank=defaults["rank"],
            champion_value=defaults["champion_value"],
            combat_side="enemy",
            is_villain=defaults["is_villain"],
            ultima_points=defaults["ultima_points"],
            # Planned cast blueprints are reusable across scene changes. A
            # conflict may request a scene-specific refresh later.
            scene_id="",
            scene_context=self._npc_design_scene_context(persona),
            background=True,
        )

    def _npc_design_scene_context(self, persona) -> dict[str, object]:
        scene = self.scene_manager.current_scene
        frame = self.scene_frame_manager.current_frame
        return {
            "scene_name": str(getattr(scene, "name", "") or ""),
            "location": str(
                getattr(scene, "location", "")
                or getattr(frame, "location", "")
                or ""
            ),
            "premise": str(getattr(frame, "premise", "") or ""),
            "current_pressure": str(
                getattr(frame, "current_pressure", "") or ""
            ),
            "opposition_goal": str(
                getattr(frame, "opposition_goal", "") or ""
            ),
            "npc_role_now": str(
                getattr(persona, "active_goal", "")
                or getattr(persona, "role_in_story", "")
                or ""
            ),
            "visible_elements": list(
                getattr(frame, "visible_elements", []) or []
            )[:4],
        }

    def _npc_blueprint_defaults(self, persona) -> dict[str, object]:
        pc_levels = [
            character.level
            for character in self.character_manager.all()
            if "pc" in character.traits
        ]
        level = max(pc_levels, default=5)
        narrative_rank = str(getattr(persona, "npc_rank", "minor") or "minor")
        rank = (
            "champion"
            if narrative_rank == "boss"
            else "elite"
            if narrative_rank in {"elite", "villain"}
            else "soldier"
        )
        is_villain = narrative_rank in {"villain", "boss"}
        return {
            "level": level,
            "rank": rank,
            "champion_value": 2 if rank == "champion" else 1,
            "is_villain": is_villain,
            "ultima_points": 5 if is_villain else 0,
        }

    def ensure_npc_combat_profiles(
        self,
        names: list[str],
        *,
        combat_side: str,
        deadline: float | None = None,
        publication_lease_owner: str = "",
    ) -> list[str]:
        """Synchronously fill missing NPC sheets immediately before conflict.

        The explicit structured side list supplied by the GM is authoritative.
        No player character is synthesized and no generic level-5 fallback is
        committed: every missing NPC is inherited from one legal core-bestiary
        card chosen by the isolated designer and compiled locally.
        """

        committed: list[str] = []
        scene = self.scene_manager.current_scene
        scene_id = str(
            getattr(scene, "scene_id", "")
            or getattr(scene, "name", "")
            or ""
        )
        location = str(getattr(scene, "location", "") or "").strip()
        for requested_name in names:
            requested = str(requested_name or "").strip()
            if not requested:
                continue
            canonical = self.world_state.resolve_npc_name(requested) or requested
            existing_name = (
                canonical
                if self.character_manager.exists(canonical)
                else requested
                if self.character_manager.exists(requested)
                else ""
            )
            if existing_name and self._is_player_character(existing_name):
                continue
            if existing_name and self._has_executable_npc_combat_profile(
                self.character_manager.get(existing_name)
            ):
                continue
            persona = self.world_state.ensure_npc_persona(
                canonical,
                profile_status="placeholder",
                public_identity=canonical,
                role_in_story="当前冲突的参与者",
                first_scene=str(getattr(scene, "name", "") or ""),
                current_location=location,
                last_seen_scene=scene_id,
            )
            defaults = self._npc_blueprint_defaults(persona)
            blueprint = self.npc_blueprint_designer.prepare_sync(
                persona,
                level=defaults["level"],
                species="",
                rank=defaults["rank"],
                champion_value=defaults["champion_value"],
                combat_side=combat_side,
                is_villain=defaults["is_villain"],
                ultima_points=defaults["ultima_points"],
                scene_id=scene_id,
                scene_context=self._npc_design_scene_context(persona),
                allow_scene_agnostic_reuse=True,
                deadline=deadline,
                publication_lease_owner=publication_lease_owner,
            )
            character = NPCBlueprintCompiler.materialize(blueprint)
            self.character_manager.add(character)
            rank, action_count = NPCBlueprintCompiler.rank_registration(
                blueprint
            )
            self.conflict_manager.register_enemy(
                canonical,
                rank,
                ultima_points=blueprint.ultima_points,
                action_count=action_count,
                is_villain=blueprint.is_villain,
            )
            persona.known_skills = list(
                dict.fromkeys(
                    [*persona.known_skills, *blueprint.selected_skills]
                )
            )
            persona.combat_actions = list(
                dict.fromkeys(
                    [
                        *persona.combat_actions,
                        *(attack.name for attack in blueprint.attacks),
                    ]
                )
            )
            committed.append(canonical)
        return committed

    @staticmethod
    def _has_executable_npc_combat_profile(character) -> bool:
        """Distinguish a real NPC sheet from a social-scene placeholder.

        A combat-ready sheet must carry its attacks explicitly.  Generic social
        placeholders and old partial records are completed through the NPC
        blueprint pipeline before initiative rather than silently receiving a
        fallback attack.
        """

        required_attributes = {"DEX", "INS", "MIG", "WLP"}
        return bool(
            character.npc_attacks
            and required_attributes.issubset(character.attributes)
            and character.max_hp > 0
        )

    def _current_dramatic_contract(self):
        plan = self.story_arc_manager.state.current_pacing_plan
        contract = getattr(plan, "dramatic_contract", None)
        return contract if contract and str(getattr(contract, "title", "") or "").strip() else None

    def _scene_motivation_for_target(self, target: str) -> str:
        """Commit a prepared opposition motive only when a rule reveals it."""

        clean_target = str(target or "").strip()
        if not clean_target or self._is_player_character(clean_target):
            return ""
        frame = self.scene_frame_manager.current_frame
        if frame is not None:
            for line in frame.npc_functions:
                text = str(line or "").strip()
                label = text.split("动机：", 1)[0].strip(" ：:，,。；;")
                if self._scene_entity_alias_match(clean_target, label) and "动机：" in text:
                    return text.split("动机：", 1)[1].strip()
        contract = self._current_dramatic_contract()
        opposition = str(getattr(contract, "opposition_goal", "") or "").strip()
        for clause in re.split(r"[，,；;。]", opposition):
            clause = clause.strip()
            subject = re.split(r"(?:要|想|试图|准备|正在|必须|会)", clause, maxsplit=1)[0].strip()
            if clean_target and self._scene_entity_alias_match(clean_target, subject or clause):
                persona = self.world_state.ensure_npc_persona(
                    clean_target,
                    public_identity=clean_target,
                    role_in_story="当前局面的对立或把关者",
                    core_drive=clause,
                    first_scene=str(getattr(self.scene_manager.current_scene, "name", "") or ""),
                )
                persona.active_goal = persona.active_goal or clause
                return clause
        return ""

    @staticmethod
    def _scene_entity_alias_match(left: str, right: str) -> bool:
        def normalize(value: str) -> str:
            clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or ""))
            return re.sub(r"^(?:门外|眼前|那支|那名|那位|这个|那个|一支|一名|一位)+", "", clean)

        left_clean = normalize(left)
        right_clean = normalize(right)
        return bool(left_clean and right_clean and (left_clean in right_clean or right_clean in left_clean))

    def _sanitize_scene_opening_reply(self, reply: str, *, allow_empty: bool = False) -> str:
        return SceneMomentPolicy.sanitize(
            reply,
            self._scene_expression_packet("", include_private=False),
            allow_empty=allow_empty,
        )




    def _build_turn_reply_pipeline(self) -> TurnReplyPipeline:
        return TurnReplyPipeline(
            [
                TurnReplyStage(
                    "rules_fallback",
                    lambda reply, resolution, _context: (
                        str(reply or "").strip()
                        or str(resolution.rules_text or "").strip()
                    ),
                ),
                TurnReplyStage(
                    "post_check_decision_prompt",
                    lambda reply, resolution, _context: self._append_post_check_choice_prompt(
                        reply,
                        resolution,
                    ),
                ),
                TurnReplyStage(
                    "resolution_fact_delivery",
                    lambda reply, resolution, context: self._ensure_resolution_information_in_reply(
                        reply,
                        resolution,
                        prior_public_facts=context.prior_public_facts,
                    ),
                ),
            ]
        )

    @staticmethod
    def _ensure_resolution_information_in_reply(
        reply: str,
        resolution: ActionResolution,
        *,
        prior_public_facts: tuple[str, ...] = (),
    ) -> str:
        """Guarantee that every authoritative public result is said at the table."""

        text = str(reply or "").strip()
        if resolution.payload.get("check_result_provisional"):
            # Traits, bonds and other post-check choices may still replace the
            # roll.  Showing success/failure fiction now would make a later
            # reroll contradict something the table has already heard.
            return text
        roll = resolution.payload.get("roll")
        committed = resolution.payload.get("committed_source_action")
        source_action = committed if isinstance(committed, Action) else resolution.action
        outcome_text = ""
        if roll is not None and source_action.parameters.get("scene_check_planned"):
            if bool(getattr(roll, "success", False)):
                outcome_text = str(
                    source_action.parameters.get("success_observation")
                    or source_action.parameters.get("success_answer")
                    or ""
                ).strip()
            else:
                outcome_text = str(
                    source_action.parameters.get("failure_consequence")
                    or source_action.parameters.get("failure_stakes")
                    or ""
                ).strip()
        if outcome_text and not TurnResponseRenderer.contains_public_text(
            text,
            outcome_text,
        ):
            text = TurnResponseRenderer.insert_before_public_state(
                text,
                outcome_text,
            )
        prepared = str(
            source_action.parameters.get("player_facing_reply") or ""
        ).strip()
        if (
            prepared
            and source_action.parameters.get("routed_world_response")
            and " ".join(prepared.split()) not in " ".join(text.split())
        ):
            # This is the final reply stage. Keep the adjudicated world response
            # before supplemental state such as clock progress so a later local
            # sanitizer cannot turn a resolved action into a bare status line.
            text = "\n".join(part for part in (prepared, text) if part).strip()

        raw_information = resolution.payload.get("information") or []
        information_items = (
            list(raw_information)
            if isinstance(raw_information, (list, tuple))
            else [raw_information]
        )
        prior_keys = {
            SceneOrchestrator._resolution_fact_key(item)
            for item in prior_public_facts
            if SceneOrchestrator._resolution_fact_key(item)
        }
        unique_information: list[str] = []
        information_keys: set[str] = set()
        for item in information_items:
            fact = " ".join(str(item or "").split()).strip()
            fact_key = SceneOrchestrator._resolution_fact_key(fact)
            if not fact_key or fact_key in information_keys:
                continue
            information_keys.add(fact_key)
            if fact_key not in prior_keys:
                unique_information.append(fact)

        # 确定性表达器通常会先把每条情报各放一行。此处只清理已经公开或
        # 规范化后重复的行，不改写权威 resolution payload。
        seen_reply_fact_keys: set[str] = set()
        filtered_lines: list[str] = []
        for line in text.splitlines():
            line_key = SceneOrchestrator._resolution_fact_key(line)
            if line_key in information_keys:
                if line_key in prior_keys or line_key in seen_reply_fact_keys:
                    continue
                seen_reply_fact_keys.add(line_key)
            filtered_lines.append(line)
        text = "\n".join(filtered_lines).strip()

        missing: list[str] = []
        for fact in unique_information:
            if not TurnResponseRenderer.contains_public_text(text, fact):
                missing.append(fact)
        if not missing:
            return text
        return "\n".join([part for part in [text, *missing] if part]).strip()

    @staticmethod
    def _resolution_fact_key(value: object) -> str:
        """忽略空白和标点，为公开事实生成稳定的精确匹配键。"""

        return re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+",
            "",
            str(value or ""),
        ).lower()

    def _is_player_character(self, name: str | None) -> bool:
        clean = str(name or "").strip()
        return bool(clean and clean in self._known_player_character_names())

    def _known_player_character_names(self) -> list[str]:
        """Return every authoritative or in-progress player character name."""

        names = {
            character.name
            for character in self.character_manager.all()
            if "pc" in character.traits and str(character.name or "").strip()
        }
        party_sheet = getattr(self.world_state, "party_sheet", None)
        for member in list(getattr(party_sheet, "members", []) or []):
            hero_name = str(getattr(member, "hero_name", "") or "").strip()
            if hero_name:
                names.add(hero_name)
        profiles = (
            getattr(self.world_state, "world_profile", None),
            getattr(getattr(self.session_zero_manager, "state", None), "world", None),
        )
        for profile in profiles:
            for key, draft in dict(getattr(profile, "hero_drafts", {}) or {}).items():
                hero_name = str(getattr(draft, "hero_name", "") or key or "").strip()
                if hero_name:
                    names.add(hero_name)
        return sorted(names)

    def _action_enters_conflict_check(self, action: Action) -> bool:
        """Return true only when this submitted action will make the leader roll.

        Pending teamwork cannot be attached to every turn-consuming action:
        Guard, Equip, inventory actions and several active skills are resolved
        without a check.  This deliberately uses a conservative allow-list so
        an ambiguous or parameter-incomplete action leaves assistance pending.
        """

        actor = self._action_actor_name(action)
        if not actor or not self.character_manager.exists(actor):
            return False
        if action.action_type in self._DEFINITE_CHECK_ACTIONS:
            return True
        if action.action_type == ActionType.ATTACK:
            targets = self._submitted_action_targets(action)
            return bool(targets) and all(
                self.character_manager.exists(target) for target in targets
            )
        if action.action_type == ActionType.SPELL:
            return self._spell_action_enters_check(action, actor)
        if action.action_type == ActionType.SKILL:
            return self._skill_action_enters_check(action, actor)
        if action.action_type == ActionType.PLAN_RITUAL:
            # This helper only runs during an active conflict; plan_ritual then
            # starts/tracks a ritual clock and makes its opening check.
            return True
        return False

    @staticmethod
    def _submitted_action_targets(action: Action) -> list[str]:
        raw_targets = (
            action.parameters.get("dual_wield_targets")
            or action.parameters.get("targets")
            or action.parameters.get("target_names")
            or action.parameters.get("target")
        )
        if isinstance(raw_targets, str):
            return [
                name.strip()
                for name in re.split(r"[、,，/]+", raw_targets)
                if name.strip()
            ]
        if isinstance(raw_targets, (list, tuple)):
            return [str(name).strip() for name in raw_targets if str(name).strip()]
        return []

    def _spell_action_enters_check(self, action: Action, actor: str) -> bool:
        spell_name = str(
            action.parameters.get("spell_name")
            or action.parameters.get("spell")
            or ""
        ).strip()
        if not spell_name:
            # The generic/custom spell path resolves through RequestRoll.
            return True
        try:
            definition = get_spell_definition(spell_name)
        except ValueError:
            # Unknown canonical names use the ad-hoc scene spell check path.
            return True

        parameter_manager = getattr(self.interceptor, "spell_parameter_manager", None)
        if (
            parameter_manager is not None
            and parameter_manager.inspect(action, definition, actor) is not None
        ):
            return False
        if definition.requires_check:
            return True
        if definition.effect_type == SpellEffectType.DAMAGE:
            return not bool(definition.fixed_damage_only)
        if definition.effect_type == SpellEffectType.MP_DAMAGE:
            return True
        if definition.effect_type == SpellEffectType.STATUS_APPLY:
            return not bool(definition.automatic_effect)
        return False

    def _skill_action_enters_check(self, action: Action, actor: str) -> bool:
        raw_name = str(action.parameters.get("skill_name") or "")
        skill_name = normalize_skill_reference_name(
            raw_name.split("（+")[0].split("(+")[0].strip()
        )
        if skill_name not in self._CHECK_SKILL_ACTIONS:
            return False
        character = self.character_manager.get(actor)
        if not (
            skill_rank(character.skills, skill_name) > 0
            or has_skill_name(character.hero_skills, skill_name)
        ):
            return False
        targets = self._submitted_action_targets(action)
        return bool(targets) and all(
            self.character_manager.exists(target) for target in targets
        )

    def _with_pending_conflict_assists(self, action: Action) -> Action:
        if not self.conflict_manager.state.active or not self._is_turn_consuming_action(action):
            return action
        actor = self._action_actor_name(action)
        if not actor or actor != self.conflict_manager.state.current_actor():
            return action
        check_entered = self._action_enters_conflict_check(action)
        helpers = (
            self.conflict_manager.pending_assists_for(actor)
            if check_entered
            else []
        )
        if not helpers:
            return action
        parameters = dict(action.parameters)
        existing = parameters.get("supporters", parameters.get("teamwork_supporters", []))
        if isinstance(existing, str):
            supporters = [name.strip() for name in re.split(r"[、,，/]+", existing) if name.strip()]
        else:
            supporters = [str(name).strip() for name in existing if str(name).strip()]
        for helper in helpers:
            if helper not in supporters:
                supporters.append(helper)
        parameters["supporters"] = supporters
        parameters["teamwork_source"] = "pending_conflict_assists"
        return Action(action.action_type, parameters)





































    def _settle_bound_scene_condition(self, resolution: ActionResolution) -> None:
        """Commit an explicitly bound condition after its final rules result.

        The GM agent chooses the exact condition before the roll. This method
        validates only typed state and the final dice result; it never rereads
        player prose or invents the NPC's payoff.
        """

        committed = resolution.payload.get("committed_source_action")
        source_action = committed if isinstance(committed, Action) else resolution.action
        condition_id = str(
            source_action.parameters.get("scene_condition_id") or ""
        ).strip()
        if not condition_id:
            return
        resolution.payload["scene_condition_id"] = condition_id
        if resolution.payload.get("check_result_provisional"):
            resolution.payload["scene_condition_outcome"] = "pending"
            return
        roll = resolution.payload.get("roll")
        if roll is None or not hasattr(roll, "success"):
            resolution.payload["scene_condition_outcome"] = "pending"
            return
        if not bool(getattr(roll, "success", False)):
            resolution.payload["scene_condition_outcome"] = "failed"
            return

        actor = str(source_action.parameters.get("actor") or "").strip()
        fulfilled = self.scene_frame_manager.mark_condition_fulfilled(
            condition_id,
            scene=self.scene_manager.current_scene,
            actor=actor,
        )
        if fulfilled is None:
            raise ValueError(
                f"开放条件【{condition_id}】无法由【{actor or '未指定角色'}】履行。"
            )
        resolution.payload["scene_condition_outcome"] = "fulfilled"
        resolution.payload["fulfilled_condition"] = dict(fulfilled)
        resolution.payload["condition_payoff_due_from"] = str(
            fulfilled.get("npc") or ""
        ).strip()
























    def _append_post_check_choice_prompt(self, reply: str, resolution: ActionResolution) -> str:
        windows = [window for window in (resolution.payload.get("post_check_windows") or []) if isinstance(window, dict)]
        if not windows:
            return reply

        prompts: list[str] = []
        critical = next(
            (
                window
                for window in windows
                if window.get("kind") == "critical_opportunity"
                and str(window.get("owner") or window.get("actor") or "") != "__gm__"
            ),
            None,
        )
        if critical is not None:
            prompts.append("这次大成功带来一个机会，你想要怎么使用它？")

        opposing_fumble = next(
            (
                window
                for window in windows
                if window.get("kind") == "fumble_opportunity"
                and str(window.get("owner") or window.get("actor") or "") != "__gm__"
            ),
            None,
        )
        if opposing_fumble is not None:
            prompts.append("对手的大失败带来一个机会，你想要怎么使用它？")

        insight = next(
            (
                window
                for window in windows
                if window.get("kind") == "skill_judgement" and window.get("label") == "灵光洞见"
            ),
            None,
        )
        if insight is not None:
            option = next((item for item in insight.get("options", []) if isinstance(item, dict)), {})
            max_questions = int(option.get("max_questions") or 1)
            target = str(option.get("target") or "调查对象")
            prompts.append(f"【灵光洞见】生效：你可以就【{target}】向我提出至多 {max_questions} 个问题。")

        text = str(reply or "").strip()
        if critical is not None or opposing_fumble is not None:
            text = re.sub(r"(?:你获得|获得)\s*1\s*次机会[。！]?", "", text).strip()
        for prompt in prompts:
            if prompt not in text:
                text = f"{text}\n{prompt}".strip()
        return text

    def _audit_transparency(self, recent_chat: str, reply: str, resolution: ActionResolution) -> None:
        """Record GM-facing quality checks without rewriting the final narration."""

        text = str(reply or "")
        leakage_markers = (
            "后台使用",
            "不要原样念",
            "当前场景框架",
            "GM私密",
            "线索池",
            "基调引导",
            "地点引导",
            "角色引导",
            "开场手法",
            "NPC回应原则",
            "调查结果原则",
            "失败处理原则",
            "秘密/真相",
            "可揭示内容",
            "特殊机制候选",
            "非固定流程",
            "action parameters",
            "payload",
            "schema",
        )
        leaked = [marker for marker in leakage_markers if marker in text]
        self.world_state.record_transparency_audit(
            "no_backend_leakage",
            not leaked,
            "未发现后台提示词泄露。" if not leaked else f"玩家输出疑似泄露后台提示词：{', '.join(leaked)}",
            severity="error" if leaked else "info",
            source="SceneOrchestrator._audit_transparency",
        )

        if self._resolution_has_failed_roll(resolution):
            failure_markers = ("失败", "没能", "没有看出", "看不出", "受阻", "代价", "误判", "暂时找不到", "被打断")
            passed = any(marker in text for marker in failure_markers)
            self.world_state.record_transparency_audit(
                "failed_roll_has_feedback",
                passed,
                "失败检定已有剧情反馈。" if passed else "失败检定缺少明确的受阻、代价或替代线索描述。",
                severity="warning" if not passed else "info",
                source="SceneOrchestrator._audit_transparency",
            )

        if resolution.payload.get("clock_change") or resolution.payload.get("clock_progress"):
            clock_visible = "命刻" in text or any(clock.name in text for clock in self.clock_manager.all())
            self.world_state.record_transparency_audit(
                "clock_progress_visible",
                clock_visible,
                "命刻变化已对玩家可见。" if clock_visible else "规则层发生命刻变化，但玩家输出没有通报命刻进度。",
                severity="warning" if not clock_visible else "info",
                source="SceneOrchestrator._audit_transparency",
            )

    def _resolution_has_failed_roll(self, resolution: ActionResolution) -> bool:
        candidates = []
        if "roll" in resolution.payload:
            candidates.append(resolution.payload.get("roll"))
        candidates.extend(resolution.payload.get("rolls") or [])
        for roll in candidates:
            if roll is None:
                continue
            if isinstance(roll, dict):
                success = roll.get("success")
            else:
                success = getattr(roll, "success", None)
            if success is False:
                return True
        return False

    def _auto_advance_conflict_turn(self, action: Action, resolution: ActionResolution) -> None:
        turn_serial = int(self.conflict_manager.state.turn_serial or 0)
        turn_start_changes = self._turn_start_clock_changes.pop(turn_serial, [])
        if resolution.payload.get("check_result_provisional"):
            turn_start_changes = []
        if turn_start_changes:
            existing = list(resolution.payload.get("auto_clock_changes") or [])
            resolution.payload["auto_clock_changes"] = [
                *existing,
                *turn_start_changes,
            ]
            resolution.payload.setdefault("timeline_phases", []).append(
                {
                    "kind": "automatic_clock",
                    "timing": "owner_turn_start",
                    "actor": str(
                        self.conflict_manager.state.turn_started_actor or ""
                    ),
                    "status": "completed",
                    "clock_names": [
                        str(getattr(change, "clock_name", "") or "")
                        for change in turn_start_changes
                    ],
                }
            )
        self.conflict_action_rounds.advance(action, resolution)

    def _on_clock_turn_start(self, actor_name: str, turn_serial: int) -> None:
        changes = self.clock_manager.emit_auto_advance_event(
            "owner_turn_start",
            actor=actor_name,
        )
        if changes:
            self._turn_start_clock_changes[int(turn_serial)] = list(changes)

    def _auto_advance_free_scene_action(
        self,
        action: Action,
        resolution: ActionResolution,
        *,
        actor_hint: str = "",
    ) -> None:
        resolution.payload.update(
            self.scene_action_rounds.record_action(
                action,
                resolution,
                actor_hint=actor_hint,
                boss_scene=self._is_boss_pressure_scene(),
                is_turn_consuming=self._is_turn_consuming_action,
            )
        )

    def record_free_scene_player_action(
        self,
        actor: str,
        *,
        changed_clock_names: set[str] | None = None,
        auto_advance_skip_names: set[str] | None = None,
    ) -> dict[str, object]:
        """Commit one typed, meaningful free-scene action to fictional time."""

        return self.scene_action_rounds.record(
            actor,
            changed_clock_names=changed_clock_names or set(),
            auto_advance_skip_names=auto_advance_skip_names or set(),
            boss_scene=self._is_boss_pressure_scene(),
        )



    def _is_boss_pressure_scene(self) -> bool:
        if any(
            clock.clock_type == "boss"
            and clock.status == "active"
            and clock.current < clock.max_segments
            for clock in self.clock_manager.all()
        ):
            return True
        if not self.conflict_manager.state.active:
            return False
        ranks = set(self.conflict_manager.state.enemy_ranks.values())
        if ranks & {EnemyRank.CHAMPION, EnemyRank.VILLAIN}:
            return True
        scene = self.scene_manager.current_scene
        return bool(
            scene is not None
            and scene.session_opportunity_role in {"climax", "climax_candidate", "boss"}
            and self.conflict_manager.state.villains
        )

    def _is_turn_consuming_action(self, action: Action) -> bool:
        if action.parameters.get("opportunity_action") or action.parameters.get(
            "_reaction_followup"
        ):
            return False
        if action.action_type == ActionType.NARRATE:
            return bool(action.parameters.get("consume_turn"))
        if action.action_type not in self._TURN_CONSUMING_ACTIONS:
            return False
        if action.action_type == ActionType.NPCACT:
            subaction = str(action.parameters.get("npc_action_type") or "").strip()
            return subaction not in {"", "Narrate", "narrate", "叙事"}
        if action.action_type == ActionType.SKILL:
            skill_name = normalize_skill_reference_name(str(action.parameters.get("skill_name") or ""))
            mode = str(action.parameters.get("mode") or "").strip().lower()
            if skill_name == "挺身守护":
                return False
            if skill_name == "契约与召唤" and mode in {
                "dismiss",
                "release",
                "解除",
                "解除阿卡纳",
                "遣散",
                "遣散奥灵",
                "释放",
                "解放",
            }:
                return False
        return True

    def _action_actor_name(self, action: Action) -> str:
        if action.action_type == ActionType.NPCACT:
            value = action.parameters.get("actor") or self.conflict_manager.state.current_actor()
            return str(value or "").strip()
        for key in (
            "actor",
            "caster",
            "inventor",
            "opener",
            "explorer",
            "user",
            "payer",
            "buyer",
        ):
            value = action.parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""










    def _held_action_notice(self, actor_name: str | None) -> str:
        if not actor_name:
            return ""
        held_actions = self.conflict_manager.held_actions_for_actor(actor_name)
        if not held_actions:
            return ""
        latest = held_actions[-1]
        summary = str(latest.get("summary") or "").strip()
        speaker = str(latest.get("speaker") or "").strip()
        mention = f"@{speaker}" if speaker else f"【{actor_name}】"
        if len(summary) > 80:
            summary = summary[:77] + "..."
        summary = summary.rstrip("。！？.!?")
        if "机会偏好" in summary or ("机会" in summary and "优先" in summary):
            return (
                f"{mention}，轮到【{actor_name}】了；刚才那条大成功用途方向我已经记下，"
                "它不会消耗你的行动。要改动作就直接说新的动作。"
            )
        return (
            f"{mention}，轮到【{actor_name}】了；刚才缓存的是："
            f"{summary or '未写明'}。要改动作就直接说新的动作。"
        )





    def _record_pipeline_span(self, span: dict[str, object]) -> None:
        self.recent_pipeline_spans.append(span)
        self.recent_pipeline_spans = self.recent_pipeline_spans[-50:]

    def _post_check_window_summary(self, resolution: ActionResolution) -> list[dict[str, object]]:
        windows = resolution.payload.get("post_check_windows") or []
        summary: list[dict[str, object]] = []
        for window in windows[:8]:
            if not isinstance(window, dict):
                continue
            summary.append(
                {
                    "kind": str(window.get("kind") or ""),
                    "label": str(window.get("label") or ""),
                    "actor": str(window.get("actor") or ""),
                    "priority": str(window.get("priority") or ""),
                }
            )
        return summary

    def _combat_trait_event_summary(self, resolution: ActionResolution) -> list[dict[str, object]]:
        events = resolution.payload.get("combat_trait_events") or []
        summary: list[dict[str, object]] = []
        for event in events[:8]:
            summary.append(
                {
                    "actor": str(getattr(event, "actor", "")),
                    "event_type": str(getattr(event, "event_type", "")),
                    "summary": str(getattr(event, "summary", "")),
                }
            )
        return summary

    def pipeline_telemetry(self) -> dict[str, object]:
        recent = self.recent_pipeline_spans[-10:]
        slowest = sorted(self.recent_pipeline_spans, key=lambda item: int(item.get("total_ms", 0)), reverse=True)[:5]
        return {
            "recent_turns": recent,
            "slowest_turns": slowest,
            "last_turn": self.recent_pipeline_spans[-1] if self.recent_pipeline_spans else {},
        }

    def save_campaign_memory(self, campaign_id: str, slot: str | None = None):
        self.set_campaign_id(campaign_id)
        return self.memory_store.save_campaign(
            campaign_id,
            world_state=self.world_state,
            character_manager=self.character_manager,
            clock_manager=self.clock_manager,
            conflict_manager=self.conflict_manager,
            scene_manager=self.scene_manager,
            scene_frame_manager=self.scene_frame_manager,
            ritual_manager=self.ritual_manager,
            project_manager=self.project_manager,
            story_arc_manager=self.story_arc_manager,
            hero_log_manager=self.hero_log_manager,
            ally_npc_manager=self.ally_npc_manager,
            session_ledger=self.session_ledger,
            session_zero_manager=self.session_zero_manager,
            travel_manager=self.travel_manager,
            dungeon_manager=self.dungeon_manager,
            world_map_manager=self.world_map_manager,
            rules_engine=self.interceptor.rules_engine,
            progression_manager=self.progression_manager,
            slot=slot,
        )

    def load_campaign_memory(self, campaign_id: str, slot: str | None = None) -> dict:
        self.set_campaign_id(campaign_id)
        snapshot = self.memory_store.load_campaign(
            campaign_id,
            world_state=self.world_state,
            character_manager=self.character_manager,
            clock_manager=self.clock_manager,
            conflict_manager=self.conflict_manager,
            scene_manager=self.scene_manager,
            scene_frame_manager=self.scene_frame_manager,
            ritual_manager=self.ritual_manager,
            project_manager=self.project_manager,
            story_arc_manager=self.story_arc_manager,
            hero_log_manager=self.hero_log_manager,
            ally_npc_manager=self.ally_npc_manager,
            session_ledger=self.session_ledger,
            session_zero_manager=self.session_zero_manager,
            travel_manager=self.travel_manager,
            dungeon_manager=self.dungeon_manager,
            world_map_manager=self.world_map_manager,
            rules_engine=self.interceptor.rules_engine,
            progression_manager=self.progression_manager,
            slot=slot,
        )
        # Successful-check invocation rights keep an in-memory rollback journal
        # and intentionally do not survive a process or save-slot boundary.
        self.interceptor.decision_window_manager.expire_ephemeral(
            reason="campaign_loaded",
        )
        self.interceptor.check_transaction_manager.clear()
        for repair_note in self.character_manager.reconcile_permanent_skill_bonuses():
            self.world_state.add_memory(f"规则迁移：{repair_note}")
        for repair_note in self.character_creation_manager.reconcile_legacy_bonds():
            self.world_state.add_memory(f"规则迁移：{repair_note}")
        self.session_zero_manager.state.world = self.world_state.world_profile
        self.story_arc_manager.world_state = self.world_state
        self.story_arc_manager.clock_manager = self.clock_manager
        self.story_arc_manager.sync_from_world_profile()
        pacing_plan = self.story_arc_manager.state.current_pacing_plan
        repaired_contract = self.campaign_pacing_manager.contract_planner.repair_legacy_contract_identity(
            pacing_plan.dramatic_contract
        )
        pacing_plan.dramatic_contract = repaired_contract
        self.scene_frame_manager.apply_contract_to_current(repaired_contract)
        self.scene_frame_manager.session_ledger = self.session_ledger
        self.session_episode_tracker.reconcile_scene_frames(
            [*self.scene_frame_manager.history, self.scene_frame_manager.current_frame]
        )
        self.reconcile_session_participants_from_current_scene()
        return snapshot

    def start_session_tracking(self, session_id: str, *, participating_pcs: list[str] | None = None) -> list[str]:
        pc_names = (
            list(participating_pcs)
            if participating_pcs is not None
            else [
                character.name
                for character in self.character_manager.all()
                if "pc" in character.traits
            ]
        )
        pc_names = list(
            dict.fromkeys(
                name
                for name in pc_names
                if self.character_manager.exists(name)
                and "pc" in self.character_manager.get(name).traits
            )
        )
        continuing_same_session = (
            self.session_ledger.active
            and not self.session_ledger.settled
            and self.session_ledger.session_id == str(session_id or "default")
        )
        existing_participants = set(self.session_ledger.participating_pcs)
        self.session_ledger.start(session_id, participating_pcs=pc_names)
        awarded: list[str] = []
        for name in pc_names:
            if continuing_same_session and name in existing_participants:
                continue
            if not self.character_manager.exists(name):
                continue
            character = self.character_manager.get(name)
            self.interceptor.skill_trigger_manager.emit(
                "session_start",
                character,
                session_id=session_id,
            )
            if "pc" in character.traits and character.fabula_points == 0:
                self.character_manager.modify_resource(name, "fabula_points", 1)
                awarded.append(name)
        return awarded

    def register_session_participant(self, character_name: str) -> bool:
        """Add a late-arriving PC and apply start-of-session effects once."""

        name = str(character_name or "").strip()
        if (
            not self.session_ledger.active
            or not name
            or not self.character_manager.exists(name)
            or "pc" not in self.character_manager.get(name).traits
        ):
            return False
        if name in self.session_ledger.participating_pcs:
            return False
        self.session_ledger.mark_participant(name)
        character = self.character_manager.get(name)
        self.interceptor.skill_trigger_manager.emit(
            "session_start",
            character,
            session_id=self.session_ledger.session_id,
        )
        if character.fabula_points == 0:
            self.character_manager.modify_resource(name, "fabula_points", 1)
            return True
        return False

    def reconcile_session_participants_from_current_scene(self) -> list[str]:
        """Repair legacy ledgers that omitted PCs already present in the scene."""

        scene = self.scene_manager.current_scene
        if not self.session_ledger.active or scene is None:
            return []
        added: list[str] = []
        for participant in scene.participants:
            name = str(participant or "").strip()
            if (
                not name
                or name in self.session_ledger.participating_pcs
                or not self.character_manager.exists(name)
                or "pc" not in self.character_manager.get(name).traits
            ):
                continue
            self.register_session_participant(name)
            if name in self.session_ledger.participating_pcs:
                added.append(name)
        return added

    def settle_session_experience(self, session_id: str) -> SessionExperienceReport | None:
        pc_names = [character.name for character in self.character_manager.all() if "pc" in character.traits]
        if not pc_names:
            self.session_ledger.finish()
            return None
        if self.session_ledger.settled and self.session_ledger.session_id == str(session_id or "default"):
            return None
        if not self.session_ledger.active or self.session_ledger.session_id != str(session_id or "default"):
            return None
        participants = [name for name in self.session_ledger.participating_pcs if self.character_manager.exists(name)]
        if not participants:
            self.session_ledger.finish()
            return None
        report = self.progression_manager.award_session_experience(
            participating_pcs=participants,
            ultima_spent=self.session_ledger.ultima_spent,
            fabula_spent=self.session_ledger.fabula_spent,
        )
        for name in participants:
            self.interceptor.skill_trigger_manager.emit(
                "session_end",
                self.character_manager.get(name),
                session_id=session_id,
            )
        self.session_ledger.finish()
        return report

    def run_npc_turn(
        self,
        action_parameters: dict[str, object],
        scene_brief: str = "",
    ) -> str:
        return self.npc_turn_executor.execute(
            action_parameters,
            scene_brief,
        )

    def _retrieve_memory_context(self, recent_chat: str) -> dict[str, list[str] | str]:
        query = self._build_memory_query(recent_chat)
        extra_entities = [character.name for character in self.character_manager.all()]
        recall = self.world_state.recall_context(
            query,
            include_private=True,
            limit=8,
            extra_entities=extra_entities,
        )
        topic_records = self.topic_memory_store.recall(
            self.campaign_id,
            query,
            include_private=True,
            include_table=True,
            already_surfaced=self._surfaced_topic_memory_paths,
            max_selected=6,
        )
        self._surfaced_topic_memory_paths.update(record.relative_path for record in topic_records)
        topic_public = [
            record.format_for_prompt()
            for record in topic_records
            if record.visibility.value == "public"
        ]
        topic_private = [
            record.format_for_prompt()
            for record in topic_records
            if record.visibility.value == "private"
        ]
        entity_hint = ""
        if recall.entities:
            entity_hint = f"本轮识别到的实体：{'、'.join(recall.entities)}。"
        guidance = (
            f"{entity_hint}公开记忆可用于对外叙事；GM私密记忆只用于内部决策、伏笔、NPC动机和暗线一致性，"
            "不得在 action parameters、in_mind_reply 或最终播报中直接泄露。"
            "Markdown 主题记忆是动态召回附件，旧记忆带 freshness note 时只能作为方向提示，"
            "硬状态以当前角色表、命刻、地图和快照为准。"
        )
        creative_guidance = self._format_creative_guidance(recent_chat)
        if creative_guidance:
            guidance = f"{guidance}\n{creative_guidance}"
        process_guidance = self._format_play_process_guidance()
        if process_guidance:
            guidance = f"{guidance}\n{process_guidance}"
        chapter_guidance = self.world_state.chapter_package_prompt()
        if chapter_guidance:
            guidance = f"{guidance}\n{chapter_guidance}"
        iconic_guidance = self.world_state.iconic_elements_prompt()
        if iconic_guidance:
            guidance = f"{guidance}\n{iconic_guidance}"
        scene_frame_guidance = self.scene_frame_manager.format_for_prompt(include_private=True)
        if scene_frame_guidance:
            guidance = f"{guidance}\n{scene_frame_guidance}"
        world_completion_guidance = self._format_world_completion_guidance()
        if world_completion_guidance:
            guidance = f"{guidance}\n{world_completion_guidance}"
        story_arc_guidance = self._format_story_arc_guidance()
        if story_arc_guidance:
            guidance = f"{guidance}\n{story_arc_guidance}"
        campaign_pacing_guidance = self.campaign_pacing_manager.prompt_guidance(
            conflict_active=self.conflict_manager.state.active,
            boss_scene=self._is_boss_pressure_scene(),
        )
        if campaign_pacing_guidance:
            guidance = f"{guidance}\n{campaign_pacing_guidance}"
        solo_guidance = self.solo_play_manager.prompt_guidance()
        if solo_guidance:
            guidance = f"{guidance}\n{solo_guidance}"
        return {
            "public": self._dedupe_memory_lines([*topic_public, *recall.public_memory], limit=12),
            "private": self._dedupe_memory_lines([*topic_private, *recall.private_memory], limit=10),
            "guidance": guidance,
        }

    def _format_creative_guidance(self, recent_chat: str) -> str:
        guidance = summarize_guidance_for_prompt(
            self.world_state.world_profile,
            extra_text=recent_chat,
            location_limit=3,
            detailed_locations=True,
        )
        parts: list[str] = []
        tags = guidance.get("inspiration_tags") or []
        if tags:
            parts.append(f"灵感标签：{'、'.join(str(tag) for tag in tags)}")
        principles = guidance.get("principles") or []
        if principles:
            parts.append("原则：" + "；".join(str(item) for item in principles[:3]))
        tone = guidance.get("tone_guidance") or []
        if tone:
            parts.append("基调引导：" + "；".join(str(item) for item in tone[:3]))
        location_guidance = guidance.get("location_guidance") or []
        if location_guidance:
            parts.append("地点引导：" + "；".join(str(item) for item in location_guidance[:3]))
        character_guidance = guidance.get("character_guidance") or []
        if character_guidance:
            parts.append("角色引导：" + "；".join(str(item) for item in character_guidance[:3]))
        scene_framework = guidance.get("scene_framework") or []
        if scene_framework:
            parts.append("场景框架：" + "；".join(str(item) for item in scene_framework[:3]))
        npc_guidance = guidance.get("npc_guidance") or []
        if npc_guidance:
            parts.append("NPC功能：" + "；".join(str(item) for item in npc_guidance[:3]))
        opening_moves = guidance.get("opening_moves") or []
        if opening_moves:
            parts.append("开场手法：" + "；".join(str(item) for item in opening_moves[:3]))
        questions = guidance.get("question_angles") or []
        if questions:
            parts.append("追问角度：" + "；".join(str(item) for item in questions[:3]))
        beats = guidance.get("story_beats") or []
        if beats:
            parts.append("故事节奏：" + "；".join(str(item) for item in beats[:3]))
        locations = guidance.get("prepared_locations") or []
        location_lines: list[str] = []
        for raw in locations[:3]:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name", "")
            archetype = raw.get("archetype", "")
            use_when = raw.get("use_when", "")
            if name:
                details = [f"{name}({archetype})：{use_when}"]
                location_questions = raw.get("questions") or []
                if location_questions:
                    details.append(f"可问：{location_questions[0]}")
                dangers = raw.get("dangers") or []
                if dangers:
                    details.append("危险：" + "、".join(str(item) for item in dangers[:2]))
                story_hooks = raw.get("story_hooks") or []
                hook_summaries: list[str] = []
                for hook in story_hooks[:3]:
                    if isinstance(hook, dict) and hook.get("title"):
                        hook_summaries.append(f"{hook['title']}：{hook.get('summary', '')}")
                if hook_summaries:
                    details.append("引子：" + "；".join(hook_summaries))
                location_lines.append("；".join(details))
        if location_lines:
            parts.append("预备地点：" + "；".join(location_lines))
        if not parts:
            return ""
        return (
            "GM创作指导（后台使用，不要原样念给玩家；预备地点不是公开事实，"
            "只有玩家追踪、物语点引入或剧情自然需要时才写入世界）："
            + "；".join(parts)
            + "。"
        )

    def _format_play_process_guidance(self) -> str:
        guidance = summarize_play_process_for_prompt(
            self.scene_manager.current_scene,
            conflict_active=self.conflict_manager.state.active,
        )
        parts: list[str] = []
        focus = str(guidance.get("current_focus") or "").strip()
        if focus:
            parts.append(f"当前镜头：{focus}")
        scene_type = guidance.get("scene_type_guidance") or []
        if scene_type:
            parts.append("场景类型：" + "；".join(str(item) for item in scene_type[:2]))
        end_triggers = guidance.get("scene_end_triggers") or []
        if end_triggers:
            parts.append("收束条件：" + "；".join(str(item) for item in end_triggers[:2]))
        session = guidance.get("session_guidance") or []
        if session:
            parts.append("场次：" + "；".join(str(item) for item in session[:2]))
        campaign = guidance.get("campaign_guidance") or []
        if campaign:
            parts.append("战役：" + "；".join(str(item) for item in campaign[:2]))
        if not parts:
            return ""
        return (
            "主持流程指导（后台使用；遵守《最终物语》的场景、场次与战役结构，"
            "不要把它原样念给玩家）："
            + "；".join(parts)
            + "。"
        )

    def _format_world_completion_guidance(self) -> str:
        profile = self.world_state.world_profile
        missing: list[str] = []
        checks = [
            ("地图卡/世界地图", profile.map_card),
            ("魔法与科技地位", profile.magic_tech_role),
            ("小队原型", profile.group_concept),
            ("世界奥秘", profile.mysteries),
            ("世界性威胁", profile.world_threats),
        ]
        for label, value in checks:
            if not value:
                missing.append(label)
        if not missing:
            return ""
        return (
            "世界创建仍有缺项（后台提示，不要机械展示清单）："
            + "、".join(missing[:5])
            + "。如果玩家已经进入冒险，不要强制倒回第零章；"
            "在合适场景用一两个自然问题、NPC线索、地点描写或玩家回答补全。"
            "当某个世界设定成为公开事实时，使用 Narrate.world_profile_updates 写入世界表；"
            "只写入玩家确认或剧情自然公开的内容。"
        )

    def _format_story_arc_guidance(self) -> str:
        summary = self.story_arc_manager.prompt_summary()
        parts: list[str] = []
        phase = str(summary.get("phase") or "").strip()
        if phase:
            parts.append(f"战役阶段：{phase}（第 {summary.get('session_count', 0)} 场后）")
        agenda = summary.get("agenda") or {}
        focus = agenda.get("recommended_focus") or []
        if focus:
            parts.append("下一场焦点：" + "；".join(str(item) for item in focus[:3]))
        questions = agenda.get("questions") or []
        if questions:
            parts.append("可问玩家：" + "；".join(str(item) for item in questions[:3]))
        closure = agenda.get("scene_closure") or []
        if closure:
            parts.append("收束建议：" + "；".join(str(item) for item in closure[:2]))
        pacing = agenda.get("campaign_pacing") or []
        if pacing:
            parts.append("战役节奏：" + "；".join(str(item) for item in pacing[:2]))
        director_moves = agenda.get("director_moves") or []
        if director_moves:
            parts.append("导演动作：" + "；".join(str(item) for item in director_moves[:2]))
        pressure = summary.get("villain_pressure") or []
        pressure_lines = []
        for raw in pressure[:2]:
            if not isinstance(raw, dict):
                continue
            pressure_lines.append(
                f"{raw.get('villain', '威胁')} {raw.get('current', 0)}/{raw.get('segments', 0)}："
                f"{raw.get('goal', '')}"
            )
        if pressure_lines:
            parts.append("反派压力：" + "；".join(pressure_lines))
        reveals = summary.get("reveal_candidates") or []
        reveal_lines = []
        for raw in reveals[:2]:
            if not isinstance(raw, dict):
                continue
            reveal_lines.append(
                f"{raw.get('title', '未命名真相')}（{raw.get('status', 'seeded')}，"
                f"线索 {raw.get('clue_count', 0)}/{raw.get('required_clues', 0)}）"
            )
        if reveal_lines:
            parts.append("待揭示真相：" + "；".join(reveal_lines))
        if not parts:
            return ""
        return (
            "长期故事节奏（后台使用；用来追踪战役阶段、反派压力、伏笔和下一场议程；"
            "不要把未公开真相直接告诉玩家）："
            + "；".join(parts)
            + "。"
        )

    def _dedupe_memory_lines(self, memories: list[str], *, limit: int) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for memory in memories:
            normalized = " ".join(memory.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(memory)
            if len(deduped) >= limit:
                break
        return deduped

    def _build_memory_query(self, recent_chat: str) -> str:
        parts = [recent_chat]
        current_actor = self.conflict_manager.state.current_actor()
        if current_actor:
            parts.append(current_actor)
        current_scene = self.scene_manager.current_scene
        if current_scene:
            parts.extend([current_scene.name, current_scene.location, current_scene.summary])
            parts.extend(current_scene.participants)
        parts.extend(self.clock_manager.formatted())
        for character in self.character_manager.all():
            if character.name and character.name in recent_chat:
                parts.append(character.name)
            if character.identity and character.identity in recent_chat:
                parts.append(character.identity)
        return " ".join(part for part in parts if part)

    def _attach_public_memory_to_resolution(self, resolution: ActionResolution, panel: GamePanel) -> None:
        if panel.retrieved_public_memory:
            resolution.payload["retrieved_public_memory"] = list(panel.retrieved_public_memory)
        if panel.memory_guidance:
            resolution.payload["memory_guidance"] = (
                "仅可使用 retrieved_public_memory 进行对外叙事；GM 私密记忆未传入表达层，不得臆造暗线。"
            )


    def start_scene(
        self,
        name: str,
        scene_type: SceneType = SceneType.STANDARD,
        *,
        location: str = "",
        participants: list[str] | None = None,
        objective: str = "",
        summary: str = "",
        session_opportunity_key: str = "",
        session_opportunity_role: str = "",
        session_opportunity_title: str = "",
        session_opportunity_purpose: str = "",
        session_opportunity_situation: str = "",
    ) -> SceneRecord:
        scene = self.scene_manager.start_scene(
            name,
            scene_type,
            location=location,
            participants=participants,
            objective=objective,
            summary=summary,
            session_opportunity_key=session_opportunity_key,
            session_opportunity_role=session_opportunity_role,
            session_opportunity_title=session_opportunity_title,
            session_opportunity_purpose=session_opportunity_purpose,
            session_opportunity_situation=session_opportunity_situation,
        )
        if self.session_ledger.active:
            for participant in scene.participants:
                self.register_session_participant(participant)
        return scene

    def end_scene(
        self,
        summary: str = "",
        *,
        restore_suspended: bool = True,
    ) -> SceneRecord | None:
        ended = self.scene_manager.end_scene(summary)
        if restore_suspended:
            self.scene_manager.restore_latest_suspended()
        return ended

    def end_all_scenes(self, summary: str = "") -> list[SceneRecord]:
        return self.scene_manager.end_all_scenes(summary)


    def initialize_session_zero(
        self,
        gm_style: GMStyleProfile | None = None,
        participants: list[str] | None = None,
    ) -> SessionZeroState:
        """Enter Session 0 without generating a second public response.

        Typed GM transactions own their public response. Keeping state setup
        separate prevents a nested model call from deciding what the GM says
        before the outer agent can review it.
        """

        state = self.session_zero_manager.start(gm_style=gm_style, participants=participants)
        self.scene_manager.start_scene(
            "Session 0 世界创建",
            SceneType.SESSION_ZERO,
            objective="共同建立世界、小队原型、反派种子，以及界限与帷幕",
        )
        self.story_arc_manager.sync_from_world_profile()
        return state



    def _record_expression_only_session_zero_response(
        self,
        speaker: str,
        message: str,
        response: SessionZeroResponse,
    ) -> SessionZeroResponse:
        """Keep conversation history without accepting facilitator mutations.

        In agent mode the facilitator is an expression component, not a state
        writer.  Any proposed facts, character changes, safety declarations or
        stage transitions must arrive through a validated GM tool receipt.
        """

        self.session_zero_manager.observe_table_talk(speaker, message)
        clean_response = SessionZeroResponse(
            message=response.message,
            stage=self.session_zero_manager.state.stage,
            action=response.action,
            suggestions=list(response.suggestions),
            questions=list(response.questions),
        )
        if getattr(clean_response, "action", "reply") != "silent":
            self.session_zero_manager.state.transcript.append(
                SessionZeroTurn(
                    speaker=self.session_zero_manager.state.gm_style.name,
                    message=clean_response.message,
                    stage=self.session_zero_manager.state.stage,
                    suggestions=list(clean_response.suggestions),
                    questions=list(clean_response.questions),
                )
            )
        return clean_response

    def _clean_stale_session_zero_hero_prompts(self, response: SessionZeroResponse) -> None:
        """Remove prompts that were composed before late hero-draft updates landed."""

        complete_class_names: set[str] = set()
        for key, draft in self.world_state.world_profile.hero_drafts.items():
            if not draft.classes or sum(draft.classes.values()) != 5:
                continue
            for name in (str(key), draft.player_name, draft.hero_name):
                clean = str(name or "").strip()
                if clean:
                    complete_class_names.add(clean)
        if not complete_class_names:
            return

        def is_stale(text: str) -> bool:
            clean = str(text or "")
            if "职业还没定全" not in clean and "分配起始 5 级" not in clean:
                return False
            return any(name and name in clean for name in complete_class_names)

        lines = [line for line in str(response.message or "").splitlines() if not is_stale(line)]
        response.message = "\n".join(line for line in lines if line.strip())
        response.questions = [question for question in response.questions if not is_stale(question)]
        if response.world_updates.get("open_questions"):
            response.world_updates["open_questions"] = [
                question for question in response.world_updates.get("open_questions", []) if not is_stale(str(question))
            ]

    def _attach_world_map_visual_if_ready(self, response: SessionZeroResponse) -> None:
        if self.world_map_image_manager is None:
            return
        try:
            result = self.world_map_image_manager.generate_if_ready(self.world_state, campaign_id=self.campaign_id)
        except Exception as exc:
            response.world_updates["map_visual_error"] = self._safe_external_error(exc)
            self.world_state.record_memory_event(
                f"世界地图原画生成失败：{self._safe_external_error(exc)}",
                kind="world_map_visual_error",
                visibility=MemoryVisibility.PRIVATE,
                tags=["map", "visual", "image_generation"],
                source="WorldMapImageManager",
            )
            return
        if result is None:
            return
        response.world_updates["map_visual"] = {
            "model": result.model,
            "output_path": result.output_path,
            "remote_url": result.remote_url,
            "revised_prompt": result.revised_prompt,
        }

    def ensure_world_map_for_adventure(self, *, max_attempts: int = 2, force: bool = False) -> dict[str, object]:
        """Generate the player map before adventure play, independent of Session 0 completion."""

        if self._world_map_generation_thread is not None and self._world_map_generation_thread.is_alive():
            self._world_map_generation_thread.join()
        if (
            self._world_map_generation_status.get("status") in {"generated", "ready"}
            and self._world_map_artifact_is_current()
            and not force
        ):
            self.session_zero_manager.ensure_custom_map_card()
            return dict(self._world_map_generation_status)
        if self._world_map_generation_status.get("status") == "failed" and not force:
            return dict(self._world_map_generation_status)
        self._world_map_generation_status = self._generate_world_map_for_adventure(
            max_attempts=max_attempts,
            force=force,
        )
        return dict(self._world_map_generation_status)

    def start_world_map_generation_async(self, *, max_attempts: int = 2) -> dict[str, object]:
        if self.world_map_image_manager is None:
            self._world_map_generation_status = {"status": "unavailable", "attempts": 0}
            return dict(self._world_map_generation_status)
        if self._world_map_generation_thread is not None and self._world_map_generation_thread.is_alive():
            return dict(self._world_map_generation_status)
        if (
            self._world_map_generation_status.get("status") in {"generated", "ready"}
            and self._world_map_artifact_is_current()
        ):
            return dict(self._world_map_generation_status)
        if self._world_map_generation_status.get("status") == "failed":
            return dict(self._world_map_generation_status)
        if not self._has_world_map_foundation():
            self._world_map_generation_status = {
                "status": "deferred",
                "attempts": 0,
                "reason": "尚无足够的地理共创信息可供绘图。",
            }
            return dict(self._world_map_generation_status)

        self._world_map_generation_status = {"status": "generating", "attempts": 0}

        def worker() -> None:
            self._world_map_generation_status = self._generate_world_map_for_adventure(max_attempts=max_attempts)

        self._world_map_generation_thread = threading.Thread(
            target=worker,
            name=f"fu-gm-map-{self.campaign_id}",
            daemon=True,
        )
        self._world_map_generation_thread.start()
        return dict(self._world_map_generation_status)

    def world_map_generation_status(self) -> dict[str, object]:
        if self._world_map_generation_thread is not None and self._world_map_generation_thread.is_alive():
            return dict(self._world_map_generation_status)
        status = dict(self._world_map_generation_status)
        manager = self.world_map_image_manager
        if manager is None:
            return status
        event = next(
            (
                item
                for item in reversed(self.world_state.memory_events)
                if str(getattr(item, "kind", "") or "") == "world_map_visual"
            ),
            None,
        )
        if event is None:
            return status
        try:
            current = bool(manager.has_current_map(self.world_state))
        except Exception:
            current = False
        payload = dict(getattr(event, "payload", {}) or {})
        local_available = any(
            path and Path(path).expanduser().is_file()
            for path in (
                str(payload.get("thumbnail_path") or "").strip(),
                str(payload.get("output_path") or "").strip(),
            )
        )
        available = bool(
            local_available or str(payload.get("remote_url") or "").strip()
        )
        if current:
            status.update(
                {
                    "status": "ready",
                    "output_path": str(payload.get("output_path") or ""),
                    "thumbnail_path": str(payload.get("thumbnail_path") or ""),
                    "remote_url": str(payload.get("remote_url") or ""),
                    "recovered_from_artifact": str(
                        status.get("status") or ""
                    ).lower()
                    not in {"generated", "ready"},
                }
            )
        elif available and str(status.get("status") or "").lower() not in {
            "generating",
            "failed",
        }:
            status["status"] = "stale"
        return status

    def _generate_world_map_for_adventure(
        self,
        *,
        max_attempts: int = 2,
        force: bool = False,
    ) -> dict[str, object]:
        if self.world_map_image_manager is None:
            return {"status": "unavailable", "attempts": 0}
        if not self._has_world_map_foundation():
            return {
                "status": "deferred",
                "attempts": 0,
                "reason": "尚无足够的地理共创信息可供绘图。",
            }
        self.session_zero_manager.ensure_custom_map_card(
            map_generation_requested=True,
        )
        errors: list[str] = []
        for attempt in range(1, max(1, max_attempts) + 1):
            if self.world_map_manager is not None:
                self.world_map_manager.sync_from_world_state()
            try:
                result = self.world_map_image_manager.generate_for_adventure(
                    self.world_state,
                    campaign_id=self.campaign_id,
                    force=force,
                )
            except Exception as exc:
                error = self._safe_external_error(exc)
                errors.append(error)
                self.world_state.record_memory_event(
                    f"世界地图生成第 {attempt} 次尝试失败：{error}",
                    kind="world_map_visual_error",
                    visibility=MemoryVisibility.PRIVATE,
                    tags=["map", "visual", "recovery"],
                    source="WorldMapImageManager",
                    payload={"attempt": attempt, "recoverable": attempt < max_attempts},
                )
                continue
            if result is None:
                existing = next(
                    (event for event in reversed(self.world_state.memory_events) if event.kind == "world_map_visual"),
                    None,
                )
                if existing and self._world_map_artifact_is_current():
                    self.session_zero_manager.ensure_custom_map_card()
                    return {
                        "status": "ready",
                        "attempts": attempt,
                        "output_path": str(existing.payload.get("output_path") or ""),
                    }
                if existing:
                    errors.append("世界设定在地图生成期间发生变化，旧地图已失效。")
                    continue
                return {"status": "unavailable", "attempts": attempt, "output_path": ""}
            if self._world_map_artifact_is_current(generated_now=True):
                self.session_zero_manager.ensure_custom_map_card()
                return {"status": "generated", "attempts": attempt, "output_path": result.output_path or ""}
            errors.append("世界设定在地图生成期间发生变化，正在按最新设定重绘。")
        return {"status": "failed", "attempts": len(errors), "errors": errors}

    def _world_map_artifact_is_current(self, *, generated_now: bool = False) -> bool:
        if self.world_map_image_manager is None:
            return False
        checker = getattr(self.world_map_image_manager, "has_current_map", None)
        if callable(checker):
            try:
                return bool(checker(self.world_state))
            except Exception:
                return False
        if generated_now:
            return True
        return self._world_map_generation_status.get("status") in {"generated", "ready"}

    def _has_world_map_foundation(self) -> bool:
        world = self.world_state.world_profile
        return any(
            (
                world.map_card,
                world.continent_name,
                world.starting_region,
                world.major_locations,
                world.kingdoms,
            )
        )

    def _world_map_hero_origins_ready(self) -> bool:
        participants = [participant.name for participant in self.session_zero_manager.state.participants]
        if not participants:
            return bool(self.world_state.world_profile.hero_drafts)
        for participant in participants:
            _key, draft = self.session_zero_manager._draft_for_player(participant)
            if draft is None or not draft.hero_name or not draft.origin:
                return False
        return True

    def _safe_external_error(self, exc: Exception) -> str:
        text = " ".join(str(exc).split())
        text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", text)
        return text[:500] if text else exc.__class__.__name__

    def session_zero_snapshot(self) -> dict:
        return self.session_zero_manager.snapshot()

    def session_zero_summary(self, *, include_private: bool = False) -> dict:
        world = self.world_state.world_profile
        hero_drafts = {}
        for key, draft in world.hero_drafts.items():
            hero_drafts[key] = {
                "player_name": draft.player_name,
                "hero_name": draft.hero_name,
                "identity": draft.identity,
                "theme": draft.theme,
                "origin": draft.origin,
                "classes": dict(draft.classes),
                "confirmed": draft.confirmed,
            }
        summary = {
            "campaign_title": world.campaign_title,
            "group_concept": world.group_concept,
            "starting_region": world.starting_region,
            "core_themes": list(world.core_themes),
            "major_locations": dict(world.major_locations),
            "factions": dict(world.factions),
            "villain_seeds": list(world.villain_seeds),
            "villain_mirrors": list(world.villain_mirrors),
            "mysteries": list(world.mysteries),
            "first_act_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "title": candidate.title,
                    "premise": candidate.premise,
                    "votes": list(candidate.votes),
                }
                for candidate in world.first_act_candidates
            ],
            "selected_first_act": world.selected_first_act_summary,
            "starting_bond_suggestions": list(world.starting_bond_suggestions),
            "safety_lines": list(world.safety_lines),
            "safety_veils": list(world.safety_veils),
            "hero_drafts": hero_drafts,
            "gm_private_notes": list(world.gm_secret_notes) if include_private else f"{len(world.gm_secret_notes)} 条已保存",
        }
        return summary

    def format_session_zero_summary(self, *, include_private: bool = False) -> str:
        summary = self.session_zero_summary(include_private=include_private)
        lines = ["第零章核心素材："]
        if summary["group_concept"]:
            lines.append(f"小队原型：{summary['group_concept']}")
        if summary["starting_region"]:
            lines.append(f"起始地区：{summary['starting_region']}")
        if summary["major_locations"]:
            lines.append("关键地点：" + "、".join(summary["major_locations"].keys()))
        if summary["factions"]:
            lines.append("阵营冲突：" + "、".join(summary["factions"].keys()))
        if summary["villain_seeds"]:
            lines.append("反派种子：" + "；".join(summary["villain_seeds"][:2]))
        if summary["mysteries"]:
            lines.append("谜团：" + "；".join(summary["mysteries"][:2]))
        if summary["selected_first_act"]:
            lines.append("第一幕：" + summary["selected_first_act"])
        if summary["starting_bond_suggestions"]:
            lines.append("可选初始羁绊：" + "；".join(summary["starting_bond_suggestions"][:2]))
        if summary["hero_drafts"]:
            heroes = []
            for draft in summary["hero_drafts"].values():
                name = draft["hero_name"] or draft["player_name"] or "未命名英雄"
                identity = f"（{draft['identity']}）" if draft["identity"] else ""
                heroes.append(f"{name}{identity}")
            lines.append("英雄：" + "、".join(heroes))
        if include_private and isinstance(summary["gm_private_notes"], list) and summary["gm_private_notes"]:
            lines.append("GM私密暗线：" + "；".join(summary["gm_private_notes"]))
        return "\n".join(lines)

    def declare_safety_line(self, item: str, *, speaker: str = "", anonymous: bool = False) -> SafetyDeclarationResult:
        return self.safety_manager.declare_line(item, speaker=speaker, anonymous=anonymous)

    def declare_safety_veil(self, item: str, *, speaker: str = "", anonymous: bool = False) -> SafetyDeclarationResult:
        return self.safety_manager.declare_veil(item, speaker=speaker, anonymous=anonymous)

    def declare_safety_boundary(
        self,
        declaration_type: str,
        item: str,
        *,
        speaker: str = "",
        anonymous: bool = False,
    ) -> SafetyDeclarationResult:
        return self.safety_manager.declare(declaration_type, item, speaker=speaker, anonymous=anonymous)

    def safety_guidance(self) -> str:
        return self.safety_manager.render_guidance()

    def suggest_hero_angles(self) -> list[str]:
        return self.character_creation_manager.suggest_hero_angles()

    def create_player_character(self, profile: HeroCreationProfile) -> CharacterCreationResult:
        return self.character_creation_manager.create_player_character(profile)

    def validate_hero_draft(self, draft_key: str) -> HeroDraftValidationResult:
        return self.character_creation_manager.validate_hero_draft(draft_key)

    def confirm_hero_draft(self, draft_key: str) -> HeroDraftValidationResult:
        return self.character_creation_manager.confirm_hero_draft(draft_key)

    def create_player_character_from_draft(
        self,
        draft_key: str,
        *,
        require_confirmed: bool = True,
    ) -> CharacterCreationResult:
        return self.character_creation_manager.create_player_character_from_draft(
            draft_key,
            require_confirmed=require_confirmed,
        )

    def create_confirmed_player_characters_from_drafts(self) -> dict[str, CharacterCreationResult]:
        results: dict[str, CharacterCreationResult] = {}
        for draft_key, draft in list(self.world_state.world_profile.hero_drafts.items()):
            if draft.confirmed:
                results[draft_key] = self.create_player_character_from_draft(draft_key)
        return results

    def finalize_campaign_creation(
        self,
        *,
        shared_goal: str = "",
        party_notes: list[str] | None = None,
    ) -> CampaignCreationBundle:
        bundle = self.character_creation_manager.finalize_campaign_creation(
            shared_goal=shared_goal,
            party_notes=party_notes,
        )
        self.story_arc_manager.sync_from_world_profile()
        return bundle

    def current_campaign_bundle(self) -> CampaignCreationBundle:
        if self.world_state.world_sheet is None or self.world_state.party_sheet is None:
            return self.finalize_campaign_creation()
        characters = [character for character in self.character_manager.all() if "pc" in character.traits]
        return CampaignCreationBundle(
            world_sheet=self.world_state.world_sheet,
            party_sheet=self.world_state.party_sheet,
            characters=characters,
        )

    def export_campaign_sheets(self, bundle: CampaignCreationBundle | None = None) -> SheetExportBundle:
        return self.sheet_exporter.export_campaign(bundle or self.current_campaign_bundle())

    def write_campaign_sheets(
        self,
        directory: str | Path,
        bundle: CampaignCreationBundle | None = None,
    ) -> SheetExportBundle:
        return self.sheet_exporter.write_campaign_exports(bundle or self.current_campaign_bundle(), directory)

    def start_conflict_scene(self, name: str, turn_order: list[str], *, location: str = "", objective: str = "") -> None:
        self.scene_manager.start_scene(
            name,
            SceneType.CONFLICT,
            location=location,
            participants=turn_order,
            objective=objective,
        )
        self.conflict_manager.start_scene(name, turn_order)

    def end_conflict_scene(self) -> None:
        blocking = [
            window
            for window in self.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if blocking:
            owners = "、".join(
                dict.fromkeys(window.owner for window in blocking if window.owner)
            )
            raise ValueError(
                f"仍有必须由【{owners or '相关玩家'}】处理的规则选择，不能结束冲突。"
            )
        self.scene_manager.end_scene("冲突场景结束。")

    def take_rest(
        self,
        rest_type: RestType,
        *,
        safe_source: str,
        payer: str | None = None,
        threat_clocks: list[str] | None = None,
        participants: list[str] | None = None,
    ) -> RestResult:
        if self.conflict_manager.state.active:
            raise ValueError("冲突仍在进行，不能开始休息。")
        resting_participants = list(participants or [])
        if not resting_participants:
            current = self.scene_manager.current_scene
            resting_participants = [
                name
                for name in list(getattr(current, "participants", []) or [])
                if self.character_manager.exists(name)
                and "pc" in self.character_manager.get(name).traits
            ]
        if not resting_participants:
            resting_participants = [
                character.name
                for character in self.character_manager.all()
                if "pc" in character.traits
            ]
        current_location = str(
            getattr(self.scene_manager.current_scene, "location", "") or safe_source
        ).strip()
        # 先验证所有角色、费用和跨场景威胁命刻。若参数无效，不能先归档
        # 当前场景，否则一次失败的休息请求会破坏仍在进行的场景状态。
        self.rest_manager.validate(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
            participants=resting_participants,
        )
        self.scene_manager.start_scene(
            f"{safe_source}休息",
            SceneType.REST,
            location=current_location,
            participants=resting_participants,
            objective="恢复体力并调整羁绊",
        )
        result = self.rest_manager.rest(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
            participants=resting_participants,
        )
        self.world_state.add_memory(result.summary)
        self.scene_manager.end_scene(result.summary)
        return result

    def begin_staged_travel(
        self,
        *,
        journey_id: str,
        origin: str,
        destination: str,
        participants: list[str],
        threat_levels: list[TravelThreatLevel] | None = None,
        regions: list[str] | None = None,
        distance: int | None = None,
        default_threat_level: TravelThreatLevel | str = TravelThreatLevel.MEDIUM,
        route_type: TravelRouteType | str | None = None,
        transport: str = "徒步",
        enforce_owned_transport: bool = False,
    ) -> dict[str, object]:
        if self.travel_manager is None:
            raise ValueError("当前编排器未配置 TravelManager。")
        route_plan = None
        if self.world_map_manager is not None and (
            distance is None
            or threat_levels is None
            or regions is None
        ):
            route_plan = self.world_map_manager.plan_route(
                origin,
                destination,
                transport=transport,
                party_size=max(1, len(participants)),
                route_type=route_type,
                explicit_distance=distance,
                default_threat_level=default_threat_level,
            )
            if distance is None:
                distance = route_plan.distance
            if threat_levels is None:
                threat_levels = route_plan.threat_levels
            if regions is None:
                regions = route_plan.regions
            route_type = route_plan.route_type
        if route_type is None:
            route_type = TravelRouteType.LAND
        if not threat_levels:
            if distance is None:
                raise ValueError("旅行缺少路线距离或逐日威胁等级。")
            days = self.travel_manager.calculate_travel_days(
                distance,
                transport=transport,
            )
            threat_levels = [
                TravelThreatLevel(default_threat_level)
            ] * days

        self.scene_manager.start_scene(
            f"{origin} -> {destination}",
            SceneType.TRAVEL,
            location=origin,
            participants=participants,
            objective=f"抵达 {destination}",
        )
        progress = self.travel_manager.begin_journey(
            journey_id=journey_id,
            origin=origin,
            destination=destination,
            threat_levels=threat_levels,
            regions=regions,
            distance=distance,
            default_threat_level=default_threat_level,
            route_type=route_type,
            transport=transport,
            party_size=max(1, len(participants)),
            party_names=participants,
            enforce_owned_transport=enforce_owned_transport,
            threat_die_step_reduction=self._travel_die_step_reduction(
                participants
            ),
            discovery_threshold=self._travel_discovery_threshold(participants),
        )
        advance = self.travel_manager.advance_active_journey()
        return self._commit_staged_travel_advance(
            advance,
            progress=progress,
            participants=participants,
            route_plan=route_plan,
        )

    def continue_staged_travel(
        self,
        *,
        event_resolution: str,
    ) -> dict[str, object]:
        if self.travel_manager is None:
            raise ValueError("当前编排器未配置 TravelManager。")
        progress = self.travel_manager.active_journey
        if progress is None:
            raise ValueError("当前没有进行中的旅程。")
        resolved_event = self.travel_manager.resolve_pending_travel_event(
            event_resolution
        )
        participants = list(progress.party_names)
        route_plan = self._matching_route_plan(progress)
        current = self.scene_manager.current_scene
        if current is None or current.scene_type != SceneType.TRAVEL:
            self.scene_manager.start_scene(
                f"{progress.origin} -> {progress.destination}",
                SceneType.TRAVEL,
                location=self._travel_progress_location(progress),
                participants=participants,
                objective=f"抵达 {progress.destination}",
            )
        advance = self.travel_manager.advance_active_journey()
        result = self._commit_staged_travel_advance(
            advance,
            progress=progress,
            participants=participants,
            route_plan=route_plan,
        )
        result["resolved_event"] = resolved_event
        return result

    def abort_staged_travel(
        self,
        *,
        reason: str,
        end_location: str,
    ) -> dict[str, object]:
        if self.travel_manager is None:
            raise ValueError("当前编排器未配置 TravelManager。")
        progress = self.travel_manager.active_journey
        if progress is None:
            raise ValueError("当前没有进行中的旅程。")
        participants = list(progress.party_names)
        interrupted = self.travel_manager.cancel_active_journey(
            reason=reason,
            end_location=end_location,
        )
        current = self.scene_manager.current_scene
        if current is not None and current.scene_type == SceneType.TRAVEL:
            self.scene_manager.end_scene(interrupted.summary)
        self.scene_manager.start_scene(
            f"旅程中止：{end_location}",
            SceneType.STANDARD,
            location=end_location,
            participants=participants,
            objective="回应旅程中止后的局面",
            summary=interrupted.summary,
        )
        self.world_state.add_memory(interrupted.summary)
        return {
            "status": "interrupted",
            "interrupted_journey": interrupted,
            "end_location": end_location,
            "participants": participants,
        }

    def _commit_staged_travel_advance(
        self,
        advance,
        *,
        progress,
        participants: list[str],
        route_plan,
    ) -> dict[str, object]:
        for day in advance.day_results:
            self._apply_travel_day_side_effects(day, participants)

        if advance.pending_event is not None:
            location = self._travel_progress_location(progress)
            scene = self.scene_manager.current_scene
            if scene is not None:
                scene.location = location
                scene.summary = advance.pending_event.summary
                for name in participants:
                    self.scene_manager.set_participant_location(name, location)
            return {
                "status": "event_pending",
                "progress": progress,
                "day_results": list(advance.day_results),
                "pending_event": advance.pending_event,
                "completed_journey": None,
            }

        completed = advance.completed_journey
        if completed is None:
            raise RuntimeError("旅行推进既没有事件，也没有完成结果。")
        self.world_state.add_memory(completed.summary)
        if self.world_map_manager is not None:
            self.world_map_manager.record_journey(completed, route_plan)
        current = self.scene_manager.current_scene
        if current is not None and current.scene_type == SceneType.TRAVEL:
            self.scene_manager.end_scene(completed.summary)
        self.scene_manager.start_scene(
            f"抵达{completed.destination}",
            SceneType.STANDARD,
            location=completed.destination,
            participants=participants,
            objective="回应抵达后的新局面",
            summary=completed.summary,
        )
        return {
            "status": "arrived",
            "progress": None,
            "day_results": list(advance.day_results),
            "pending_event": None,
            "completed_journey": completed,
        }

    def _apply_travel_day_side_effects(
        self,
        day,
        participants: list[str],
    ) -> None:
        pc_names = [
            name
            for name in participants
            if self.character_manager.exists(name)
            and "pc" in self.character_manager.get(name).traits
        ]
        for name in pc_names:
            character = self.character_manager.get(name)
            skill_result = self.interceptor.skill_trigger_manager.emit(
                "travel_roll",
                character,
                roll=day.roll,
            )
            for effect in skill_result.effects:
                if effect.resource != "inventory_points" or effect.amount <= 0:
                    continue
                before, after = self.character_manager.modify_resource(
                    character.name,
                    "inventory_points",
                    effect.amount,
                )
                if after > before:
                    self.world_state.add_memory(
                        f"{character.name} 的【{effect.source}】在第 {day.day} 个旅行日"
                        f"恢复了 {after - before} 点物资。"
                    )
        if day.event_type.value == "discovery":
            if self.world_map_manager is not None:
                discovered = self.world_map_manager.discover_from_travel_day(day)
                if discovered is not None:
                    self.world_state.add_memory(f"地图新增地点：{discovered.name}")
            trigger_results = self.trigger_manager.on_travel_discovery(pc_names)
            day.trigger_results.extend(trigger_results)
            for trigger_result in trigger_results:
                self.world_state.add_memory(trigger_result.summary)
        self.world_state.add_memory(day.summary)

    @staticmethod
    def _travel_progress_location(progress) -> str:
        day = max(1, int(progress.current_day or 1))
        region = (
            progress.regions[day - 1]
            if progress.regions and day - 1 < len(progress.regions)
            else progress.destination
        )
        return (
            f"{progress.origin}至{progress.destination}途中"
            f"（第{day}日：{region}）"
        )

    def _matching_route_plan(self, progress):
        if self.world_map_manager is None:
            return None
        for plan in reversed(self.world_map_manager.route_plans):
            if (
                plan.origin == progress.origin
                and plan.destination == progress.destination
                and plan.transport == progress.transport
                and int(plan.distance) == int(progress.distance)
                and int(plan.travel_days) == int(progress.total_days)
            ):
                return plan
        return None

    def travel(
        self,
        *,
        origin: str,
        destination: str,
        threat_levels: list[TravelThreatLevel] | None = None,
        regions: list[str] | None = None,
        distance: int | None = None,
        default_threat_level: TravelThreatLevel | str = TravelThreatLevel.MEDIUM,
        route_type: TravelRouteType | str | None = None,
        transport: str = "徒步",
        party_size: int = 1,
        enforce_owned_transport: bool = False,
    ):
        if self.travel_manager is None:
            raise ValueError("当前编排器未配置 TravelManager。")
        route_plan = None
        if self.world_map_manager is not None and (distance is None or threat_levels is None or regions is None):
            route_plan = self.world_map_manager.plan_route(
                origin,
                destination,
                transport=transport,
                party_size=party_size,
                route_type=route_type,
                explicit_distance=distance,
                default_threat_level=default_threat_level,
            )
            if distance is None:
                distance = route_plan.distance
            if threat_levels is None:
                threat_levels = route_plan.threat_levels
            if regions is None:
                regions = route_plan.regions
            route_type = route_plan.route_type
        if route_type is None:
            route_type = TravelRouteType.LAND
        self.scene_manager.start_scene(
            f"{origin} -> {destination}",
            SceneType.TRAVEL,
            location=origin,
            objective=f"抵达 {destination}",
        )
        result = self.travel_manager.travel(
            origin=origin,
            destination=destination,
            threat_levels=threat_levels,
            regions=regions,
            distance=distance,
            default_threat_level=default_threat_level,
            route_type=route_type,
            transport=transport,
            party_size=party_size,
            enforce_owned_transport=enforce_owned_transport,
            event_tables_by_region=route_plan.event_tables_by_region if route_plan is not None else None,
            discovery_threshold=self._travel_discovery_threshold(),
            threat_die_step_reduction=self._travel_die_step_reduction(),
        )
        for day in result.day_results:
            for character in self.character_manager.all():
                if "pc" not in character.traits:
                    continue
                skill_result = self.interceptor.skill_trigger_manager.emit(
                    "travel_roll",
                    character,
                    roll=day.roll,
                )
                for effect in skill_result.effects:
                    if effect.resource != "inventory_points" or effect.amount <= 0:
                        continue
                    before, after = self.character_manager.modify_resource(
                        character.name,
                        "inventory_points",
                        effect.amount,
                    )
                    if after > before:
                        self.world_state.add_memory(
                            f"{character.name} 的【{effect.source}】在第 {day.day} 个旅行日恢复了 {after - before} 点物资。"
                        )
            if day.event_type.value == "discovery":
                if self.world_map_manager is not None:
                    discovered = self.world_map_manager.discover_from_travel_day(day)
                    if discovered is not None:
                        self.world_state.add_memory(f"地图新增地点：{discovered.name}")
                party_names = [character.name for character in self.character_manager.all() if "pc" in character.traits]
                trigger_results = self.trigger_manager.on_travel_discovery(party_names)
                day.trigger_results.extend(trigger_results)
                for trigger_result in trigger_results:
                    self.world_state.add_memory(trigger_result.summary)
            self.world_state.add_memory(day.summary)
        self.world_state.add_memory(result.summary)
        if self.world_map_manager is not None:
            self.world_map_manager.record_journey(result, route_plan)
        self.scene_manager.end_scene(f"队伍从 {origin} 抵达 {destination}。")
        return result

    def _travel_discovery_threshold(
        self,
        participants: list[str] | None = None,
    ) -> int:
        participant_names = set(participants or [])
        return max(
            [
                skill_rank(character.skills, "宝物猎人") + 1
                for character in self.character_manager.all()
                if "pc" in character.traits
                and (not participant_names or character.name in participant_names)
                and skill_rank(character.skills, "宝物猎人") > 0
            ]
            or [1]
        )

    def _travel_die_step_reduction(
        self,
        participants: list[str] | None = None,
    ) -> int:
        participant_names = set(participants or [])
        return int(
            any(
                "pc" in character.traits
                and (not participant_names or character.name in participant_names)
                and skill_rank(character.skills, "见多识广") > 0
                for character in self.character_manager.all()
            )
        )

    def start_dungeon(
        self,
        name: str,
        mode: DungeonExploreMode,
        *,
        location: str = "",
        danger_clocks: dict[str, int] | None = None,
        session_opportunity_key: str = "",
        session_opportunity_role: str = "",
        session_opportunity_title: str = "",
        session_opportunity_purpose: str = "",
        session_opportunity_situation: str = "",
    ):
        self.scene_manager.start_scene(
            name,
            SceneType.DUNGEON,
            location=location,
            objective="探索复杂地点并处理危险命刻",
            session_opportunity_key=session_opportunity_key,
            session_opportunity_role=session_opportunity_role,
            session_opportunity_title=session_opportunity_title,
            session_opportunity_purpose=session_opportunity_purpose,
            session_opportunity_situation=session_opportunity_situation,
        )
        return self.dungeon_manager.start_dungeon(
            name,
            mode,
            location=location,
            danger_clocks=danger_clocks,
        )

    def end_dungeon(self, summary: str = "", *, outcome: str = "completed"):
        ended = self.dungeon_manager.end_dungeon(summary, outcome=outcome)
        self.scene_manager.end_scene(summary or "地下城探索结束。")
        if ended is not None:
            outcome_label = {
                "completed": "完成",
                "retreated": "撤离",
                "abandoned": "放弃",
            }.get(ended.completion_status, "结束")
            self.world_state.add_memory(
                f"地下城【{ended.name}】探索{outcome_label}。"
            )
        return ended

    def explore_dungeon_area(
        self,
        area_name: str | None = None,
        *,
        actor: str = "",
        action: str = "enter",
        success: bool | None = None,
        collect_treasure: bool = False,
        trigger_trap: bool = False,
        danger_segments: int = 1,
        clear_area: bool | None = None,
        note: str = "",
    ):
        result = self.dungeon_manager.explore_area(
            area_name,
            actor=actor,
            action=action,
            success=success,
            collect_treasure=collect_treasure,
            trigger_trap=trigger_trap,
            danger_segments=danger_segments,
            clear_area=clear_area,
            note=note,
        )
        self.world_state.record_memory_event(
            result.summary,
            kind="dungeon_exploration",
            entities=[entity for entity in [actor, result.dungeon_name, result.area_name] if entity],
            tags=["dungeon", result.area_type.value, result.action],
        )
        return result

    def plan_ritual(
        self,
        *,
        caster: str,
        name: str,
        discipline: RitualDiscipline,
        potency: RitualPotency,
        scope: RitualScope,
        effect: str,
        attributes: list[str] | None = None,
        rare_material: str = "",
        forbidden_tags: list[str] | None = None,
    ) -> RitualPlan:
        plan = self.ritual_manager.plan_ritual(
            caster=caster,
            name=name,
            discipline=discipline,
            potency=potency,
            scope=scope,
            effect=effect,
            attributes=attributes,
            rare_material=rare_material,
            forbidden_tags=forbidden_tags,
        )
        self.world_state.add_memory(
            f"仪式计划：{caster} 准备【{name}】，消耗 {plan.mp_cost} MP，难度等级 {plan.target_number}。"
        )
        return plan

    def start_conflict_ritual(self, plan: RitualPlan) -> RitualPlan:
        objective = f"填满命刻【{plan.clock_name}】并完成仪式"
        if self.scene_manager.current_scene is None:
            self.scene_manager.start_scene(plan.name, SceneType.CONFLICT, objective=objective)
        elif self.scene_manager.current_scene.scene_type == SceneType.CONFLICT:
            current_objective = self.scene_manager.current_scene.objective
            self.scene_manager.current_scene.objective = f"{current_objective}；{objective}" if current_objective else objective
        else:
            self.scene_manager.start_scene(plan.name, SceneType.CONFLICT, objective=objective)
        started = self.ritual_manager.start_conflict_ritual(plan)
        self.world_state.add_memory(f"冲突仪式开始：{started.clock_name} {started.clock_segments} 格。")
        return started

    def contribute_to_ritual(self, clock_name: str, *, actor: str, attributes: list[str] | None = None):
        outcome, change = self.ritual_manager.contribute_to_ritual(clock_name, actor=actor, attributes=attributes)
        self.world_state.add_memory(
            f"{actor} 推进仪式【{clock_name}】：{outcome.total} 对抗难度等级 {outcome.target_number}，命刻 {change.after}/{change.max_segments}。"
        )
        return outcome, change

    def cast_ritual(
        self,
        plan_or_clock_name: RitualPlan | str,
        *,
        catastrophe: str = "仪式失控，GM 应让效果以危险、代价或威胁命刻的方式扭曲。",
        require_completed_clock: bool = False,
        persistence_type: PersistentChangeType | str | None = None,
        location: str = "",
        subject: str = "",
    ) -> RitualCastResult:
        result = self.ritual_manager.cast_ritual(
            plan_or_clock_name,
            catastrophe=catastrophe,
            require_completed_clock=require_completed_clock,
        )
        if result.success:
            self.interceptor._persist_ritual_result(
                Action(
                    ActionType.CAST_RITUAL,
                    {
                        "persistence_type": persistence_type,
                        "location": location,
                        "subject": subject,
                    },
                ),
                result,
            )
        self.world_state.add_memory(result.summary)
        return result

    def start_project(
        self,
        *,
        inventor: str,
        name: str,
        potency: RitualPotency,
        scope: RitualScope,
        use: ProjectUse,
        effect: str,
        output_type: PersistentChangeType | str | None = None,
        owner: str = "",
        location: str = "",
        flaw: str = "",
        special_materials: list[str] | None = None,
        cost_materials: list[str] | None = None,
        material_credit: int = 0,
    ) -> ProjectState:
        project = self.project_manager.start_project(
            inventor=inventor,
            name=name,
            potency=potency,
            scope=scope,
            use=use,
            effect=effect,
            output_type=output_type,
            owner=owner,
            location=location,
            flaw=flaw,
            special_materials=special_materials,
            cost_materials=cost_materials,
            material_credit=material_credit,
        )
        self.world_state.add_memory(
            f"项目启动：{inventor} 开始制作【{name}】，成本 {project.material_cost}Z，进度 {project.required_progress}。"
        )
        return project

    def hire_project_helpers(self, project_name: str, *, payer: str, count: int = 1):
        change = self.project_manager.hire_helpers(project_name, payer=payer, count=count)
        self.world_state.add_memory(f"项目【{project_name}】雇佣帮手 {count} 名。")
        return change

    def work_on_project(self, project_name: str, workers: list[str], *, days: int = 1) -> ProjectProgressResult:
        result = self.project_manager.work_on_project(project_name, workers, days=days)
        completed_now = bool(
            result.completed
            and result.before < result.project.required_progress
            and result.after >= result.project.required_progress
        )
        if completed_now:
            self.interceptor._persist_project_result(result.project)
        self.world_state.add_memory(result.summary)
        return result

    def award_session_experience(
        self,
        *,
        participating_pcs: list[str] | None = None,
        ultima_spent: int = 0,
        fabula_spent: int = 0,
        base_xp: int = 5,
    ) -> SessionExperienceReport:
        return self.progression_manager.award_session_experience(
            participating_pcs=participating_pcs,
            ultima_spent=ultima_spent,
            fabula_spent=fabula_spent,
            base_xp=base_xp,
        )

    def level_up_character(
        self,
        character_name: str,
        *,
        class_name: str,
        skill_name: str,
        attribute_increase: str = "",
        hero_skill: str = "",
        status_immunity: StatusEffect | str | None = None,
        extra_spells: list[str] | None = None,
        new_identity: str = "",
        new_theme: str = "",
    ) -> LevelUpResult:
        return self.progression_manager.level_up(
            character_name,
            class_name=class_name,
            skill_name=skill_name,
            attribute_increase=attribute_increase,
            hero_skill=hero_skill,
            status_immunity=status_immunity,
            extra_spells=extra_spells,
            new_identity=new_identity,
            new_theme=new_theme,
        )
