from __future__ import annotations

import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from fu_gm.action_brain import ActionBrain
from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.safety_manager import SafetyManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.sheet_exporter import SheetExporter
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
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
    SessionZeroStage,
    SheetExportBundle,
    LevelUpResult,
    StatusEffect,
    TravelRouteType,
    TravelThreatLevel,
)
from fu_gm.npc_director import NPCDirector
from fu_gm.play_process_guidance import summarize_play_process_for_prompt
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator, SessionZeroFacilitator


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
        ActionType.CONTRIBUTE_RITUAL,
        ActionType.CAST_RITUAL,
        ActionType.NPCACT,
    }

    def __init__(
        self,
        action_brain: ActionBrain,
        character_manager: CharacterManager,
        clock_manager: ClockManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
        interceptor: ActionInterceptor,
        expressor: Narrator,
        npc_director: NPCDirector | None = None,
        scene_manager: SceneManager | None = None,
        session_zero_manager: SessionZeroManager | None = None,
        session_zero_facilitator: SessionZeroFacilitator | None = None,
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
        campaign_id: str = "default",
        trigger_manager: TriggerManager | None = None,
        world_map_manager: WorldMapManager | None = None,
        world_map_image_manager: WorldMapImageManager | None = None,
    ) -> None:
        self.action_brain = action_brain
        self.character_manager = character_manager
        self.clock_manager = clock_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state
        self.interceptor = interceptor
        self.expressor = expressor
        self.npc_director = npc_director
        self.scene_manager = scene_manager or SceneManager()
        self.session_zero_manager = session_zero_manager or SessionZeroManager(world_state)
        self.session_zero_facilitator = session_zero_facilitator or HeuristicSessionZeroFacilitator()
        self.character_creation_manager = character_creation_manager or CharacterCreationManager(
            character_manager,
            world_state,
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
        self.story_arc_manager = story_arc_manager or StoryArcManager(world_state, clock_manager)
        self.campaign_id = campaign_id or "default"
        self._surfaced_topic_memory_paths: set[str] = set()
        self.trigger_manager = trigger_manager or TriggerManager(character_manager)
        self.world_map_manager = world_map_manager
        self.world_map_image_manager = world_map_image_manager
        self._world_map_generation_thread: threading.Thread | None = None
        self._world_map_generation_status: dict[str, object] = {"status": "idle", "attempts": 0}
        self.recent_pipeline_spans: list[dict[str, object]] = []
        self.interceptor.ritual_manager = self.ritual_manager
        self.interceptor.project_manager = self.project_manager
        self.interceptor.dungeon_manager = self.dungeon_manager
        self.interceptor.trigger_manager = self.trigger_manager
        self.story_arc_manager.sync_from_world_profile()

    def build_panel(self, recent_chat: str) -> GamePanel:
        pcs = [c for c in self.character_manager.all() if "pc" in c.traits]
        enemies = [c for c in self.character_manager.all() if "enemy" in c.traits]
        phase = self.conflict_manager.format_phase()
        if not self.conflict_manager.state.active:
            phase = self.scene_manager.format_phase()
        memory_context = self._retrieve_memory_context(recent_chat)
        return GamePanel(
            game_phase=phase,
            active_clocks=self.clock_manager.formatted(),
            pc_status=[self.character_manager.format_status(c) for c in pcs],
            enemy_status=[self.character_manager.format_status(c) for c in enemies],
            recent_chat=recent_chat,
            current_actor=self.conflict_manager.state.current_actor(),
            table_status=self.world_state.format_attendance(),
            safety_guidance=self.safety_manager.render_guidance(),
            retrieved_public_memory=memory_context["public"],
            gm_private_memory=memory_context["private"],
            memory_guidance=memory_context["guidance"],
        )

    def set_campaign_id(self, campaign_id: str) -> None:
        campaign_id = campaign_id or "default"
        if campaign_id != self.campaign_id:
            self._surfaced_topic_memory_paths.clear()
        self.campaign_id = campaign_id

    def run_turn(self, recent_chat: str) -> str:
        total_started = time.monotonic()
        span: dict[str, object] = {
            "kind": "player_turn",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "input_chars": len(str(recent_chat)),
            "action_brain": self.action_brain.__class__.__name__,
            "expressor": self.expressor.__class__.__name__,
        }
        panel, action, resolution, recovery = self._decide_and_resolve_with_recovery(recent_chat, span)
        if recovery:
            span["recovery"] = recovery
        phase_started = time.monotonic()
        span["rules_ms"] = int(span.get("rules_ms", 0))
        phase_started = time.monotonic()
        self._persist_narrative_topic_memory(resolution)
        span["memory_writeback_ms"] = int((time.monotonic() - phase_started) * 1000)
        resolution.payload["safety_guidance"] = self.safety_manager.render_guidance()
        self._attach_public_memory_to_resolution(resolution, panel)
        phase_started = time.monotonic()
        reply = self.expressor.render(resolution)
        span["expressor_ms"] = int((time.monotonic() - phase_started) * 1000)
        span["total_ms"] = int((time.monotonic() - total_started) * 1000)
        span["ok"] = True
        self._record_pipeline_span(span)
        return reply

    def _decide_and_resolve_with_recovery(
        self,
        recent_chat: str,
        span: dict[str, object],
    ) -> tuple[GamePanel, Action, ActionResolution, list[dict[str, object]]]:
        recovery: list[dict[str, object]] = []
        working_chat = recent_chat
        build_panel_ms = 0
        action_brain_ms = 0
        rules_ms = 0
        for attempt in range(2):
            phase_started = time.monotonic()
            panel = self.build_panel(working_chat)
            build_panel_ms += int((time.monotonic() - phase_started) * 1000)
            phase_started = time.monotonic()
            action = self.action_brain.decide(panel)
            action_brain_ms += int((time.monotonic() - phase_started) * 1000)
            span["action_type"] = action.action_type.value

            missing_names = self._missing_action_characters(action)
            recovered_names = self._recover_characters_from_drafts(missing_names)
            unresolved = [name for name in missing_names if not self.character_manager.exists(name)]
            if recovered_names:
                recovery.append({"attempt": attempt + 1, "kind": "hero_draft_restore", "characters": recovered_names})
            if unresolved and attempt == 0:
                recovery.append({"attempt": 1, "kind": "action_replan", "missing_characters": unresolved})
                working_chat = self._recovery_turn_context(recent_chat, unresolved)
                continue

            try:
                phase_started = time.monotonic()
                out_of_turn_resolution = self._out_of_turn_resolution(action, working_chat)
                if out_of_turn_resolution is not None:
                    resolution = out_of_turn_resolution
                else:
                    action = self._with_pending_conflict_assists(action)
                    resolution = self.interceptor.resolve(action)
                    self._auto_advance_conflict_turn(action, resolution)
                rules_ms += int((time.monotonic() - phase_started) * 1000)
            except KeyError as exc:
                missing = str(exc.args[0]) if exc.args else "未知角色或字段"
                if attempt == 0:
                    recovered = self._recover_characters_from_drafts([missing])
                    if recovered:
                        recovery.append({"attempt": 1, "kind": "hero_draft_restore", "characters": recovered})
                        continue
                    recovery.append({"attempt": 1, "kind": "action_replan", "missing_characters": [missing]})
                    working_chat = self._recovery_turn_context(recent_chat, [missing])
                    continue
                raise KeyError(f"内部恢复重试后仍找不到权威角色或规则字段：{missing}") from exc

            span["build_panel_ms"] = build_panel_ms
            span["action_brain_ms"] = action_brain_ms
            span["rules_ms"] = rules_ms
            return panel, action, resolution, recovery
        raise RuntimeError("动作恢复流程意外结束。")

    def _out_of_turn_resolution(self, action: Action, recent_chat: str = "") -> ActionResolution | None:
        if not self.conflict_manager.state.active:
            return None
        if not self._is_turn_consuming_action(action):
            return None
        current_actor = self.conflict_manager.state.current_actor()
        actor = self._action_actor_name(action)
        if not current_actor or not actor or actor == current_actor:
            return None
        if not self.character_manager.exists(actor):
            return None
        if "pc" not in self.character_manager.get(actor).traits:
            return None
        if self._looks_like_conflict_assist(action, recent_chat) and self.conflict_manager.register_team_assist(
            actor,
            current_actor,
            reason=self._summarize_attempted_action(action, recent_chat),
        ):
            helpers = self.conflict_manager.state.pending_assists.get(current_actor, [])
            message = (
                f"【{actor}】消耗本轮行动协助【{current_actor}】。"
                f"当【{current_actor}】完成下一次检定时，团队合作会计入修正。"
            )
            return ActionResolution(
                action=Action(
                    ActionType.NARRATE,
                    {
                        "summary": message,
                        "team_assist_registered": True,
                        "supporter": actor,
                        "leader": current_actor,
                    },
                ),
                rules_text=message,
                payload={
                    "team_assist_registered": True,
                    "supporter": actor,
                    "leader": current_actor,
                    "pending_assists": {current_actor: list(helpers)},
                    "turn_board": self.conflict_manager.format_turn_board(),
                    "combat_log": self.conflict_manager.format_combat_log(),
                },
            )
        held_action = self.conflict_manager.register_held_action(
            actor,
            action.action_type.value,
            self._summarize_attempted_action(action, recent_chat),
        )
        message = (
            f"现在轮到【{current_actor}】行动；【{actor}】的动作先不结算，"
            "已经暂缓到回合队列里。轮到他时再确认并结算。"
        )
        self.conflict_manager.record_log(actor, "out_of_turn", message)
        return ActionResolution(
            action=Action(
                ActionType.NARRATE,
                {
                    "summary": message,
                    "out_of_turn": True,
                    "attempted_action_type": action.action_type.value,
                    "attempted_actor": actor,
                },
            ),
            rules_text=message,
            payload={
                "out_of_turn": True,
                "attempted_action": action,
                "attempted_action_type": action.action_type.value,
                "attempted_actor": actor,
                "current_actor": current_actor,
                "held_action": held_action,
                "turn_board": self.conflict_manager.format_turn_board(),
                "combat_log": self.conflict_manager.format_combat_log(),
            },
        )

    def _with_pending_conflict_assists(self, action: Action) -> Action:
        if not self.conflict_manager.state.active or not self._is_turn_consuming_action(action):
            return action
        actor = self._action_actor_name(action)
        if not actor or actor != self.conflict_manager.state.current_actor():
            return action
        helpers = self.conflict_manager.consume_pending_assists(actor)
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
        parameters["teamwork_turns_already_consumed"] = helpers
        parameters["teamwork_source"] = "pending_conflict_assists"
        return Action(action.action_type, parameters)

    def _looks_like_conflict_assist(self, action: Action, recent_chat: str) -> bool:
        pieces = [
            recent_chat,
            str(action.parameters.get("summary") or ""),
            str(action.parameters.get("reasoning") or ""),
            str(action.parameters.get("in_mind_reply") or ""),
            str(action.parameters.get("target") or ""),
            str(action.parameters.get("clock_name") or ""),
        ]
        text = "\n".join(piece for piece in pieces if piece)
        return any(token in text for token in ("协助", "支援", "帮忙", "帮助", "团队合作", "配合", "辅助"))

    def _summarize_attempted_action(self, action: Action, recent_chat: str) -> str:
        summary = str(action.parameters.get("summary") or action.parameters.get("reasoning") or "").strip()
        if summary:
            return summary[:120]
        text = str(recent_chat or "").strip().replace("\n", " ")
        return text[:120] if text else action.action_type.value

    def _auto_advance_conflict_turn(self, action: Action, resolution: ActionResolution) -> None:
        if not self.conflict_manager.state.active:
            return
        if resolution.payload.get("out_of_turn"):
            return
        if action.action_type == ActionType.NEXT_TURN:
            return
        if not self._is_turn_consuming_action(action):
            return
        previous_actor = self.conflict_manager.state.current_actor()
        next_actor = self.conflict_manager.next_turn()
        resolution.payload["turn_auto_advanced"] = True
        resolution.payload["previous_actor"] = previous_actor
        resolution.payload["next_actor"] = next_actor
        resolution.payload["turn_board"] = self.conflict_manager.format_turn_board()
        resolution.payload["combat_log"] = self.conflict_manager.format_combat_log()
        if next_actor:
            resolution.rules_text = f"{resolution.rules_text} 下一位行动者：{next_actor}。"

    def _is_turn_consuming_action(self, action: Action) -> bool:
        if action.action_type not in self._TURN_CONSUMING_ACTIONS:
            return False
        if action.action_type == ActionType.NPCACT:
            subaction = str(action.parameters.get("npc_action_type") or "").strip()
            return subaction not in {"", "Narrate", "narrate", "叙事"}
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

    def _missing_action_characters(self, action: Action) -> list[str]:
        if action.action_type in {ActionType.NARRATE, ActionType.ADVANCE_CLOCK, ActionType.ACCEPT_STORY_CHANGE}:
            return []
        names: list[str] = []
        for key in ("actor", "caster", "inventor", "payer", "buyer", "opener", "explorer", "user"):
            value = action.parameters.get(key)
            if isinstance(value, str) and value.strip() and not self.character_manager.exists(value.strip()):
                names.append(value.strip())
        return list(dict.fromkeys(names))

    def _recover_characters_from_drafts(self, names: list[str]) -> list[str]:
        recovered: list[str] = []
        drafts = self.world_state.world_profile.hero_drafts
        for name in names:
            if not name or self.character_manager.exists(name):
                continue
            draft_key = next(
                (key for key, draft in drafts.items() if key == name or draft.hero_name == name),
                "",
            )
            if not draft_key:
                continue
            try:
                result = self.create_player_character_from_draft(draft_key, require_confirmed=False)
            except ValueError:
                continue
            recovered.append(result.character.name)
            self.world_state.record_memory_event(
                f"冒险开始前从有效角色草稿恢复正式 PC：【{result.character.name}】。",
                kind="character_recovery",
                entities=[result.character.name],
                tags=["recovery", "character_creation"],
                source="SceneOrchestrator",
            )
        return recovered

    def _recovery_turn_context(self, recent_chat: str, missing_names: list[str]) -> str:
        roster = [character.name for character in self.character_manager.all()]
        draft_issues: list[str] = []
        for key, draft in self.world_state.world_profile.hero_drafts.items():
            if key not in missing_names and draft.hero_name not in missing_names:
                continue
            validation = self.validate_hero_draft(key)
            issues = validation.missing_fields + validation.errors
            if issues:
                draft_issues.append(f"{draft.hero_name or key}：{'；'.join(issues)}")
        return (
            f"{recent_chat}\n\n"
            "<system-reminder title=\"动作内部恢复\">\n"
            f"上一次动作引用了不存在的权威角色：{'、'.join(missing_names)}。"
            f"当前可结算角色：{'、'.join(roster) if roster else '暂无正式角色'}。"
            + (f"草稿尚缺：{' | '.join(draft_issues)}。" if draft_issues else "")
            + "请重新判断同一条玩家输入一次。不得再次引用不存在的角色进行硬规则结算；"
            "如果角色卡尚未完成，改用 Narrate 承接行动并在叙事中自然要求补齐必要信息，不要伪造数值。\n"
            "</system-reminder>"
        )

    def _record_pipeline_span(self, span: dict[str, object]) -> None:
        self.recent_pipeline_spans.append(span)
        self.recent_pipeline_spans = self.recent_pipeline_spans[-50:]

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
            ritual_manager=self.ritual_manager,
            project_manager=self.project_manager,
            story_arc_manager=self.story_arc_manager,
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
            ritual_manager=self.ritual_manager,
            project_manager=self.project_manager,
            story_arc_manager=self.story_arc_manager,
            slot=slot,
        )
        self.session_zero_manager.state.world = self.world_state.world_profile
        self.story_arc_manager.world_state = self.world_state
        self.story_arc_manager.clock_manager = self.clock_manager
        self.story_arc_manager.sync_from_world_profile()
        return snapshot

    def run_npc_turn(self, scene_brief: str = "") -> str:
        actor_name = self.conflict_manager.state.current_actor()
        if actor_name is None:
            raise ValueError("当前没有可行动的角色。")
        actor = self.character_manager.get(actor_name)
        if "enemy" not in actor.traits and "villain" not in actor.traits:
            raise ValueError(f"{actor_name} 不是敌对角色，不能调用 NPCAct。")
        if self.npc_director is None:
            raise ValueError("当前场景未配置 NPCDirector。")

        panel = self.build_panel(scene_brief or f"轮到 {actor_name} 行动。")
        action = self.npc_director.decide(panel, actor_name)
        resolution = self.interceptor.resolve(action)
        self._auto_advance_conflict_turn(action, resolution)
        self._persist_narrative_topic_memory(resolution)
        resolution.payload["safety_guidance"] = self.safety_manager.render_guidance()
        self._attach_public_memory_to_resolution(resolution, panel)
        return self.expressor.render(resolution)

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
        world_completion_guidance = self._format_world_completion_guidance()
        if world_completion_guidance:
            guidance = f"{guidance}\n{world_completion_guidance}"
        story_arc_guidance = self._format_story_arc_guidance()
        if story_arc_guidance:
            guidance = f"{guidance}\n{story_arc_guidance}"
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

    def _persist_narrative_topic_memory(self, resolution: ActionResolution) -> None:
        """把 LLM 的软叙事裁量即时写成可召回 Markdown 记忆。

        规则拦截器只负责把非数值事实落进 WorldState；这里负责让这些创意事实在下一轮
        就能被主动召回。任何 HP/MP/金币/命刻等硬状态仍不在这里处理。
        """

        if resolution.action.action_type != ActionType.NARRATE:
            return
        if not resolution.payload.get("narrative_authority"):
            return

        params = resolution.action.parameters
        summary = str(resolution.payload.get("summary") or "").strip()
        public_facts = self._string_list(params.get("public_facts") or params.get("world_facts") or params.get("facts"))
        private_notes = self._string_list(params.get("gm_private_notes") or params.get("private_notes"))
        subject_facts = self._dict_list(params.get("subject_facts"))
        npc_updates = self._dict_list(params.get("npc_updates"))
        relations = self._dict_list(params.get("relations"))
        persistent_changes = [str(item) for item in resolution.payload.get("persistent_changes", []) if str(item).strip()]
        world_profile_updates = [
            str(item) for item in resolution.payload.get("world_profile_updates", []) if str(item).strip()
        ]

        public_lines: list[str] = []
        if summary and (public_facts or subject_facts or npc_updates or relations or persistent_changes or world_profile_updates):
            public_lines.extend(["## 场景摘要", summary])
        if public_facts:
            public_lines.extend(["", "## 公开事实", *[f"- {fact}" for fact in public_facts]])
        public_subject_lines = []
        for item in subject_facts:
            subject = str(item.get("subject") or item.get("name") or "").strip()
            note = str(item.get("note") or item.get("fact") or item.get("description") or "").strip()
            if subject and note:
                public_subject_lines.append(f"- {subject}：{note}")
        if public_subject_lines:
            public_lines.extend(["", "## 对象事实", *public_subject_lines])
        public_npc_lines = []
        private_npc_lines = []
        for item in npc_updates:
            name = str(item.get("name") or item.get("npc") or "").strip()
            if not name:
                continue
            note = str(item.get("note") or item.get("memory") or item.get("event") or "").strip()
            public_identity = str(item.get("public_identity") or "").strip()
            role = str(item.get("role_in_story") or "").strip()
            if note or public_identity or role:
                public_npc_lines.append(f"- {name}：" + "；".join(part for part in [public_identity, role, note] if part))
            secret_parts = []
            for key in ("core_drive", "secrets", "taboos", "custom_prompt"):
                value = item.get(key)
                if isinstance(value, list):
                    secret_parts.extend(str(part) for part in value if str(part).strip())
                elif str(value or "").strip():
                    secret_parts.append(str(value).strip())
            if secret_parts:
                private_npc_lines.append(f"- {name}：" + "；".join(secret_parts))
        if public_npc_lines:
            public_lines.extend(["", "## NPC 公开更新", *public_npc_lines])

        public_relation_lines = []
        private_relation_lines = []
        for item in relations:
            source = str(item.get("source") or "").strip()
            relation = str(item.get("relation") or item.get("type") or "").strip()
            target = str(item.get("target") or "").strip()
            if not source or not relation or not target:
                continue
            line = f"- {source} --{relation}--> {target}"
            visibility = str(item.get("visibility") or MemoryVisibility.PUBLIC.value)
            if visibility == MemoryVisibility.PRIVATE.value:
                private_relation_lines.append(line)
            else:
                public_relation_lines.append(line)
        if public_relation_lines:
            public_lines.extend(["", "## 公开关系", *public_relation_lines])
        if persistent_changes:
            public_lines.extend(["", "## 非数值持久变化", *[f"- {change}" for change in persistent_changes]])
        if world_profile_updates:
            public_lines.extend(["", "## 世界观补全", *[f"- {change}" for change in world_profile_updates]])

        private_lines: list[str] = []
        if private_notes:
            private_lines.extend(["## GM 私密暗线", *[f"- {note}" for note in private_notes]])
        if private_npc_lines:
            private_lines.extend(["", "## NPC 私密更新", *private_npc_lines])
        if private_relation_lines:
            private_lines.extend(["", "## 私密关系", *private_relation_lines])

        if not public_lines and not private_lines:
            return

        now = datetime.now(timezone.utc)
        scene = self.scene_manager.current_scene
        scene_title = scene.name if scene else "软叙事"
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        title = f"{scene_title}：LLM 软叙事写回"
        tags = ["narrate", "llm_soft_writeback"]
        if scene:
            tags.append(scene.scene_type.value)
        all_text = "\n".join([summary, *public_facts, *world_profile_updates, *private_notes, *public_lines, *private_lines])
        entities = self._entities_from_text(all_text)

        if public_lines:
            self.topic_memory_store.write_topic_memory(
                self.campaign_id,
                visibility=MemoryVisibility.PUBLIC,
                memory_type="narrative_writeback",
                title=title,
                description=summary or self._first_nonempty_line(public_lines),
                body="\n".join(public_lines),
                entities=entities,
                tags=tags,
                filename=f"narrate_{timestamp}",
                last_event_at=now.isoformat(),
                extra_frontmatter={"scene": scene_title},
            )
        if private_lines:
            self.topic_memory_store.write_topic_memory(
                self.campaign_id,
                visibility=MemoryVisibility.PRIVATE,
                memory_type="narrative_private_writeback",
                title=f"{scene_title}：GM 私密软叙事写回",
                description=self._first_nonempty_line(private_lines),
                body="\n".join(private_lines),
                entities=entities,
                tags=[*tags, "private"],
                filename=f"narrate_{timestamp}_private",
                last_event_at=now.isoformat(),
                lock_level="draft",
                extra_frontmatter={"scene": scene_title},
            )

    def _entities_from_text(self, text: str) -> list[str]:
        extra_entities = [character.name for character in self.character_manager.all()]
        return self.world_state.extract_entities(text, extra_entities=extra_entities)

    def _first_nonempty_line(self, lines: list[str]) -> str:
        for line in lines:
            stripped = line.strip().lstrip("#- ").strip()
            if stripped:
                return stripped[:160]
        return ""

    def _string_list(self, value) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _dict_list(self, value) -> list[dict]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

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

    def advance_turn(self) -> str | None:
        return self.conflict_manager.next_turn()

    def start_scene(
        self,
        name: str,
        scene_type: SceneType = SceneType.STANDARD,
        *,
        location: str = "",
        participants: list[str] | None = None,
        objective: str = "",
        summary: str = "",
    ) -> SceneRecord:
        return self.scene_manager.start_scene(
            name,
            scene_type,
            location=location,
            participants=participants,
            objective=objective,
            summary=summary,
        )

    def end_scene(self, summary: str = "") -> SceneRecord | None:
        return self.scene_manager.end_scene(summary)

    def start_session_zero(
        self,
        gm_style: GMStyleProfile | None = None,
        participants: list[str] | None = None,
    ) -> SessionZeroResponse:
        state = self.session_zero_manager.start(gm_style=gm_style, participants=participants)
        self.scene_manager.start_scene(
            "Session 0 世界创建",
            SceneType.SESSION_ZERO,
            objective="共同建立世界、小队原型、反派种子，以及界限与帷幕",
        )
        response = self.session_zero_facilitator.opening(state)
        self.session_zero_manager.apply_response(response)
        self.story_arc_manager.sync_from_world_profile()
        return response

    def configure_session_zero_participants(self, participants: list[str]) -> list[str]:
        configured = self.session_zero_manager.configure_participants(participants)
        return [participant.name for participant in configured]

    def discuss_session_zero(self, speaker: str, message: str) -> SessionZeroResponse:
        participants = list(self.world_state.present_players)
        if speaker and speaker not in participants:
            participants.append(speaker)
        if not self.session_zero_manager.state.active:
            self.start_session_zero(participants=participants or [speaker])
        else:
            self.session_zero_manager.ensure_participants(participants or [speaker])
        self.safety_manager.parse_and_declare(speaker, message)
        self.session_zero_manager.record_player_input(speaker, message)
        response = self.session_zero_facilitator.respond(self.session_zero_manager.state, speaker, message)
        self.session_zero_manager.apply_response(response)
        self._apply_session_zero_creation_intent(speaker, message, response)
        map_started = False
        if self.session_zero_manager.world_creation_ready():
            map_status = self.start_world_map_generation_async(max_attempts=2)
            map_started = map_status.get("status") == "generating"
        if self.session_zero_manager.finish_if_ready():
            response.stage = SessionZeroStage.READY
            response.world_updates["completed"] = True
            response.world_updates["summary"] = self.session_zero_summary(include_private=False)
            response.message = response.message.rstrip() + "\n\n" + self.format_session_zero_summary(include_private=False)
        else:
            response.stage = self.session_zero_manager.state.stage
            response.world_updates["completed"] = False
        if map_started and "地图生成中" not in response.message:
            response.message = response.message.rstrip() + "\n地图生成中；完成前不会进入第一章。"
        self.story_arc_manager.sync_from_world_profile()
        return response

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

    def ensure_world_map_for_adventure(self, *, max_attempts: int = 2) -> dict[str, object]:
        """Generate the player map before adventure play, independent of Session 0 completion."""

        if self._world_map_generation_thread is not None and self._world_map_generation_thread.is_alive():
            self._world_map_generation_thread.join()
            return dict(self._world_map_generation_status)
        if self._world_map_generation_status.get("status") in {"generated", "ready"}:
            return dict(self._world_map_generation_status)
        return self._generate_world_map_for_adventure(max_attempts=max_attempts)

    def start_world_map_generation_async(self, *, max_attempts: int = 2) -> dict[str, object]:
        if self.world_map_image_manager is None:
            self._world_map_generation_status = {"status": "unavailable", "attempts": 0}
            return dict(self._world_map_generation_status)
        if self._world_map_generation_thread is not None and self._world_map_generation_thread.is_alive():
            return dict(self._world_map_generation_status)
        if self._world_map_generation_status.get("status") in {"generated", "ready"}:
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
        return dict(self._world_map_generation_status)

    def _generate_world_map_for_adventure(self, *, max_attempts: int = 2) -> dict[str, object]:
        if self.world_map_image_manager is None:
            return {"status": "unavailable", "attempts": 0}
        if not self._has_world_map_foundation():
            return {
                "status": "deferred",
                "attempts": 0,
                "reason": "尚无足够的地理共创信息可供绘图。",
            }
        if self.world_map_manager is not None:
            self.world_map_manager.sync_from_world_state()
        errors: list[str] = []
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                result = self.world_map_image_manager.generate_for_adventure(
                    self.world_state,
                    campaign_id=self.campaign_id,
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
                return {
                    "status": "ready" if existing else "unavailable",
                    "attempts": attempt,
                    "output_path": str(existing.payload.get("output_path") or "") if existing else "",
                }
            return {"status": "generated", "attempts": attempt, "output_path": result.output_path or ""}
        return {"status": "failed", "attempts": len(errors), "errors": errors}

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
        lines = ["【Session 0 摘要】"]
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
            lines.append("英雄草稿：" + "、".join(heroes))
        if include_private and isinstance(summary["gm_private_notes"], list) and summary["gm_private_notes"]:
            lines.append("GM私密暗线：" + "；".join(summary["gm_private_notes"]))
        elif not include_private:
            lines.append(f"GM私密暗线：{summary['gm_private_notes']}，不进入玩家摘要。")
        return "\n".join(lines)

    def _apply_session_zero_creation_intent(
        self,
        speaker: str,
        message: str,
        response: SessionZeroResponse,
    ) -> None:
        if not self.world_state.world_profile.hero_drafts:
            return
        should_confirm = self._looks_like_confirm_hero_intent(message)
        should_create = self._looks_like_create_hero_intent(message)
        if not should_confirm and not should_create:
            return

        draft_key = self._draft_key_from_message(speaker, message)
        notes: list[str] = []
        try:
            validation = self.confirm_hero_draft(draft_key)
            if validation.ready:
                notes.append(f"角色草稿【{draft_key}】已确认。")
            else:
                details = validation.missing_fields + validation.errors
                notes.append(f"角色草稿【{draft_key}】已标记确认，但还不能建卡：{'；'.join(details)}")
        except ValueError as exc:
            notes.append(str(exc))
            response.accepted_facts.extend(notes)
            response.world_updates.setdefault("creation_intents", []).extend(notes)
            response.message = response.message.rstrip() + "\n角色还不能正式建卡；我已把原因记在后台状态里，下一步会继续提示缺项。"
            return

        if should_create:
            try:
                result = self.create_player_character_from_draft(draft_key)
                notes.append(
                    f"正式 PC【{result.character.name}】已创建，初始泽尼特 {result.starting_zenit}。"
                )
            except ValueError as exc:
                notes.append(f"暂时不能创建正式 PC：{exc}")

        response.accepted_facts.extend(notes)
        response.world_updates.setdefault("creation_intents", []).extend(notes)

    def _looks_like_confirm_hero_intent(self, message: str) -> bool:
        tokens = ["确认角色", "角色确认", "确认草稿", "角色定稿", "定稿角色", "就这个角色", "这个角色可以", "就这样"]
        return any(token in message for token in tokens)

    def _looks_like_create_hero_intent(self, message: str) -> bool:
        tokens = ["创建角色", "正式建卡", "生成角色", "建立角色", "创建pc", "创建PC", "建卡", "做成正式角色"]
        return any(token in message for token in tokens)

    def _draft_key_from_message(self, speaker: str, message: str) -> str:
        drafts = self.world_state.world_profile.hero_drafts
        if speaker in drafts:
            return speaker
        for key, draft in drafts.items():
            if key and key in message:
                return key
            if draft.hero_name and draft.hero_name in message:
                return key
        if len(drafts) == 1:
            return next(iter(drafts))
        return speaker

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
        self.conflict_manager.end_scene()
        self.scene_manager.end_scene("冲突场景结束。")

    def take_rest(
        self,
        rest_type: RestType,
        *,
        safe_source: str,
        payer: str | None = None,
        threat_clocks: list[str] | None = None,
    ) -> RestResult:
        self.scene_manager.start_scene(
            f"{safe_source}休息",
            SceneType.REST,
            objective="恢复体力并调整羁绊",
        )
        result = self.rest_manager.rest(
            rest_type,
            safe_source=safe_source,
            payer=payer,
            threat_clocks=threat_clocks,
        )
        self.world_state.add_memory(result.summary)
        self.scene_manager.end_scene(result.summary)
        return result

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
        )
        for day in result.day_results:
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

    def start_dungeon(
        self,
        name: str,
        mode: DungeonExploreMode,
        *,
        location: str = "",
        danger_clocks: dict[str, int] | None = None,
    ):
        self.scene_manager.start_scene(
            name,
            SceneType.DUNGEON,
            location=location,
            objective="探索复杂地点并处理危险命刻",
        )
        return self.dungeon_manager.start_dungeon(
            name,
            mode,
            location=location,
            danger_clocks=danger_clocks,
        )

    def end_dungeon(self, summary: str = ""):
        ended = self.dungeon_manager.end_dungeon(summary)
        self.scene_manager.end_scene(summary or "地下城探索结束。")
        if ended is not None:
            self.world_state.add_memory(f"地下城【{ended.name}】探索结束。")
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
            f"仪式计划：{caster} 准备【{name}】，消耗 {plan.mp_cost} MP，DL {plan.target_number}。"
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
            f"{actor} 推进仪式【{clock_name}】：{outcome.total} vs {outcome.target_number}，命刻 {change.after}/{change.max_segments}。"
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
        if result.completed:
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
