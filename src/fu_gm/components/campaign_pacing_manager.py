from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from fu_gm.components.campaign_feedback_controller import CampaignFeedbackController
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.session_contract_planner import SessionContractPlanner
from fu_gm.components.session_closure_policy import SessionClosurePolicy
from fu_gm.components.session_beat_director import SessionBeatDirective, SessionBeatDirector
from fu_gm.components.episode_momentum_tracker import EpisodeMomentumTracker
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    CampaignLength,
    CampaignPacingProfile,
    Clock,
    PressureBudget,
    SessionEpisodeProgress,
    SessionSceneProgress,
    SessionPacingPlan,
    SessionDramaticContract,
    SessionFeedbackSignals,
    StoryArcPhase,
    DecisionWindowStatus,
)


class CampaignPacingManager:
    """Backstage pacing budget for a Fabula Ultima campaign.

    It does not script the story. It controls density: how many clocks should
    sit in the foreground, how many threats can auto-advance, and when villains
    or boss beats should become louder.
    """

    _PRESSURE_TYPES = {"threat", "villain", "dungeon", "boss"}
    _GOAL_TYPES = {"objective", "ritual"}

    def __init__(
        self,
        story_arc_manager: StoryArcManager,
        clock_manager: ClockManager,
        world_state: WorldState,
        *,
        character_manager: CharacterManager | None = None,
        client=None,
        model: str = "",
        review_client=None,
        review_model: str = "",
        session_prep_timeout_seconds: float = 60.0,
    ) -> None:
        self.story_arc_manager = story_arc_manager
        self.clock_manager = clock_manager
        self.world_state = world_state
        self.feedback_controller = CampaignFeedbackController()
        self.contract_planner = SessionContractPlanner(
            story_arc_manager,
            world_state,
            character_manager=character_manager,
            client=client,
            model=model,
            review_client=review_client,
            review_model=review_model,
            session_prep_timeout_seconds=session_prep_timeout_seconds,
        )
        self.closure_policy = SessionClosurePolicy()
        self.beat_director = SessionBeatDirector()

    def gm_beat_directive(
        self,
        requested_instruction: str = "",
        *,
        force_consequence: bool = False,
    ) -> SessionBeatDirective:
        """Describe the next GM beat from committed episode evidence."""

        state = self.story_arc_manager.state
        current_plan = state.current_pacing_plan
        session_number = max(
            1,
            int(current_plan.session_number or (state.session_count + 1)),
        )
        if self.contract_planner.should_rebuild_first_session_contract(
            current_plan.dramatic_contract,
            session_number=session_number,
        ):
            # 旧存档可能在第一幕地点确认前就生成过契约。主动节拍必须先
            # 修复这项定向错配，不能继续拿错误地点的压力阶梯指导现场。
            self.refresh_plan(force_session_number=session_number)
            state = self.story_arc_manager.state
        return self.beat_director.build(
            contract=state.current_pacing_plan.dramatic_contract,
            progress=state.current_session_progress,
            requested_instruction=requested_instruction,
            force_consequence=force_consequence,
        )

    def configure(
        self,
        *,
        length: CampaignLength | str | None = None,
        target_sessions: int | None = None,
        target_arcs: int | None = None,
    ) -> CampaignPacingProfile:
        profile = self.story_arc_manager.state.pacing_profile
        if length:
            profile.length = length if isinstance(length, CampaignLength) else CampaignLength(str(length))
        if target_sessions:
            profile.target_sessions = max(1, int(target_sessions))
        elif length:
            defaults = {
                CampaignLength.SHORT: 20,
                CampaignLength.STANDARD: 35,
                CampaignLength.LONG: 50,
            }
            profile.target_sessions = defaults.get(profile.length, profile.target_sessions)
        if target_arcs:
            profile.target_arcs = max(1, int(target_arcs))
        else:
            profile.target_arcs = self._recommended_arc_count(profile.target_sessions)
        profile.boss_every_sessions = max(3, round(profile.target_sessions / max(1, profile.target_arcs)))
        profile.minor_climax_every_sessions = max(2, profile.boss_every_sessions // 2)
        return profile

    def refresh_plan(
        self,
        *,
        conflict_active: bool = False,
        boss_scene: bool = False,
        force_session_number: int | None = None,
        allow_model_prep: bool = True,
        deadline: float | None = None,
        register_session_npcs: bool = True,
        preparation_source: str = "foreground",
    ) -> SessionPacingPlan:
        self.story_arc_manager.sync_from_world_profile()
        state = self.story_arc_manager.state
        profile = state.pacing_profile
        if profile.target_sessions <= 0:
            self.configure(length=profile.length)
        session_number = force_session_number or max(1, state.session_count + 1)
        phase = self._phase_for_session(session_number, profile)
        arc_index = self._arc_index(session_number, profile)
        profile.current_arc = arc_index
        profile.current_arc_title = self._arc_title(arc_index, profile.target_arcs)
        base_budget = self.pressure_budget(
            phase=phase,
            conflict_active=conflict_active,
            boss_scene=boss_scene,
        )
        feedback_control = self.feedback_controller.evaluate(
            state.session_feedback_history,
            base_budget,
        )
        budget = self.feedback_controller.apply_budget(base_budget, feedback_control)
        feedback_adjustments = list(feedback_control.notes)
        current_plan = state.current_pacing_plan
        recoverable_contract = next(
            (
                contract
                for contract in reversed(state.session_contract_history)
                if contract.session_number == session_number
                and contract.title
                and contract.status != "completed"
            ),
            None,
        )
        current_contract_needs_rebuild = (
            self.contract_planner.should_rebuild_first_session_contract(
                current_plan.dramatic_contract,
                session_number=session_number,
            )
        )
        recoverable_contract_needs_rebuild = bool(
            recoverable_contract is not None
            and self.contract_planner.should_rebuild_first_session_contract(
                recoverable_contract,
                session_number=session_number,
            )
        )
        if (
            current_plan.session_number == session_number
            and current_plan.dramatic_contract.title
            and current_plan.dramatic_contract.status != "completed"
            and not current_contract_needs_rebuild
        ):
            dramatic_contract = current_plan.dramatic_contract
        elif recoverable_contract is not None and not recoverable_contract_needs_rebuild:
            # A checkpoint may preserve the contract ledger even when the
            # transient current-plan envelope was not the last object saved.
            # Reuse the committed same-session contract instead of silently
            # inventing a replacement story from whatever thread is loudest.
            dramatic_contract = recoverable_contract
        elif self._continuation_contract_due(session_number):
            dramatic_contract = self.contract_planner.continue_from(
                previous=current_plan.dramatic_contract,
                previous_progress=state.current_session_progress,
                session_number=session_number,
            )
        else:
            dramatic_contract = self.contract_planner.create(
                session_number=session_number,
                phase=phase,
                profile=profile,
                feedback=feedback_control,
                allow_model_prep=allow_model_prep,
                deadline=deadline,
                register_npcs=register_session_npcs,
                preparation_source=preparation_source,
            )
        self._ensure_session_progress(session_number)
        expected_turns = self._expected_table_turns(profile)
        if feedback_control.expected_turn_delta:
            expected_turns = (
                expected_turns[0] + feedback_control.expected_turn_delta,
                expected_turns[1] + feedback_control.expected_turn_delta,
            )
        base_reveal_quota = 1 if phase in {StoryArcPhase.OPENING, StoryArcPhase.RISING} else 2
        plan = SessionPacingPlan(
            session_number=session_number,
            arc_index=arc_index,
            arc_title=profile.current_arc_title,
            phase=phase,
            strong_start=self._strong_start_hint(phase),
            expected_scene_count=(3, 5),
            expected_table_turns=expected_turns,
            reveal_quota=max(0, base_reveal_quota + feedback_control.reveal_quota_delta),
            pressure_budget=budget,
            villain_cadence=self._villain_cadence(session_number, profile, phase, boss_scene=boss_scene),
            boss_cadence=self._boss_cadence(session_number, profile, boss_scene=boss_scene),
            gm_autonomy_cadence=self._gm_autonomy_cadence(phase, boss_scene=boss_scene),
            session_structure=self._session_structure(phase),
            gm_notes=[*self._gm_notes(phase, budget), *feedback_adjustments],
            dramatic_contract=dramatic_contract,
            feedback_adjustments=feedback_adjustments,
        )
        state.current_pacing_plan = plan
        state.phase = phase
        self._remember_contract(dramatic_contract)
        return plan

    def observe_scene_started(
        self,
        scene_id: str,
        *,
        opening_image: str = "",
        scene_role: str = "",
        location: str = "",
    ) -> SessionEpisodeProgress:
        progress = self._ensure_session_progress(self.story_arc_manager.state.current_pacing_plan.session_number)
        clean_scene_id = str(scene_id or "").strip()
        if clean_scene_id and clean_scene_id not in progress.scene_ids:
            progress.scene_ids.append(clean_scene_id)
        if clean_scene_id:
            progress.active_scene_id = clean_scene_id
            scene_progress = progress.scene_progress.setdefault(
                clean_scene_id,
                SessionSceneProgress(scene_id=clean_scene_id),
            )
            if scene_role:
                scene_progress.scene_role = str(scene_role).strip()[:80]
            if location:
                scene_progress.location = str(location).strip()[:200]
            if opening_image:
                scene_progress.opening_image = str(opening_image).strip()[:500]
        if opening_image and not progress.memory_image:
            progress.memory_image = str(opening_image).strip()[:300]
        if len(progress.substantial_scene_ids) >= 1 and progress.stage == "opening":
            progress.stage = "development"
        progress.last_event = f"场景开始：{clean_scene_id}" if clean_scene_id else "场景开始"
        return progress

    def observe_scene_focused(self, scene_id: str) -> SessionEpisodeProgress:
        """Move the pacing camera without treating a restored branch as new.

        Split-party scenes remain active while another branch has the table's
        focus.  Returning to one must redirect subsequent pacing evidence to
        that branch, but it must not emit a second scene-start lifecycle event.
        """

        progress = self._ensure_session_progress(
            self.story_arc_manager.state.current_pacing_plan.session_number
        )
        clean_scene_id = str(scene_id or "").strip()
        if not clean_scene_id:
            return progress
        if clean_scene_id not in progress.scene_ids:
            # Legacy saves may contain active scene branches that predate
            # per-scene episode tracking. Recover their identity here without
            # claiming that a new fictional scene just began.
            progress.scene_ids.append(clean_scene_id)
        progress.scene_progress.setdefault(
            clean_scene_id,
            SessionSceneProgress(scene_id=clean_scene_id),
        )
        progress.active_scene_id = clean_scene_id
        progress.last_event = f"镜头聚焦：{clean_scene_id}"
        return progress

    def observe_turn(
        self,
        *,
        player_action: bool,
        action_summary: str = "",
        consequence: str = "",
        local_payoff: str = "",
        reveal: str = "",
        reversal: bool = False,
        climax: str = "",
        opposition_move: str = "",
        public_image: str = "",
        local_question_changed: bool = False,
        local_question_resolved: bool = False,
        scene_resolved: bool = False,
        session_question_resolved: bool = False,
        session_close_requested: bool = False,
        deliberate_cliffhanger: bool = False,
        signature_image_evolved: bool = False,
        opening_signature_realized: str = "",
        awaits_player_response: bool = False,
        closure_payoff: bool = False,
        next_session_hook: str = "",
        callback_to_previous: str = "",
        gm_beat_purpose: str = "",
    ) -> SessionEpisodeProgress:
        progress = self._ensure_session_progress(self.story_arc_manager.state.current_pacing_plan.session_number)
        material_change = bool(
            consequence
            or local_payoff
            or reveal
            or reversal
            or climax
            or opposition_move
            or local_question_changed
            or local_question_resolved
            or scene_resolved
            or session_question_resolved
            or session_close_requested
            or deliberate_cliffhanger
            or signature_image_evolved
        )
        scene_progress = self._active_scene_progress(progress)
        was_awaiting_player = bool(progress.awaiting_player_response)
        if player_action:
            progress.meaningful_turns += 1
            if scene_progress is not None:
                scene_progress.player_actions += 1
            self._append_unique(progress.player_choices, action_summary, limit=12)
            memory_choice = self._memory_choice_candidate(action_summary)
            if memory_choice and not progress.memory_choice:
                progress.memory_choice = memory_choice
            EpisodeMomentumTracker.observe_player_action(
                progress,
                action_summary=action_summary,
                material_change=material_change,
            )
            if material_change:
                progress.last_player_material_change_turn = progress.meaningful_turns
            if was_awaiting_player:
                progress.awaiting_player_response = False
                progress.pending_player_prompt = ""
                progress.pending_player_scene_id = ""
                if progress.closure_stage == "aftermath_open":
                    progress.closure_stage = "aftermath_acknowledged"
                    progress.aftermath_response_count += 1
                if scene_progress is not None:
                    scene_progress.player_responded = True
        elif material_change:
            progress.stagnant_player_turns = 0
            clean_purpose = str(gm_beat_purpose or "").strip()
            if clean_purpose:
                progress.gm_beat_purposes.append(clean_purpose[:80])
                progress.gm_beat_player_turns.append(progress.meaningful_turns)
                if len(progress.gm_beat_purposes) > 16:
                    del progress.gm_beat_purposes[:-16]
                if len(progress.gm_beat_player_turns) > 16:
                    del progress.gm_beat_player_turns[:-16]
        if scene_progress is not None and material_change:
            scene_progress.material_changes += 1
            scene_progress.consequences += int(bool(consequence))
            scene_progress.local_payoffs += int(bool(local_payoff))
            scene_progress.reveals += int(bool(reveal))
            scene_progress.opposition_moves += int(bool(opposition_move))
            scene_progress.climax_events += int(bool(climax))
            scene_progress.local_question_changed = bool(
                scene_progress.local_question_changed or local_question_changed
            )
            scene_progress.local_question_resolved = bool(
                scene_progress.local_question_resolved
                or local_question_resolved
                or scene_resolved
            )
            scene_progress.reversal_reached = bool(
                scene_progress.reversal_reached or (reversal and reveal)
            )
        self._append_unique(progress.concrete_consequences, consequence, limit=12)
        self._append_unique(progress.local_payoffs, local_payoff, limit=8)
        self._append_unique(progress.revealed_changes, reveal, limit=8)
        self._append_unique(progress.climax_events, climax, limit=8)
        self._append_unique(progress.opposition_moves, opposition_move, limit=8)
        progress.local_question_changed = (
            progress.local_question_changed or bool(local_question_changed)
        )
        progress.local_question_resolved = (
            progress.local_question_resolved or bool(local_question_resolved)
        )
        progress.session_question_resolved = bool(
            progress.session_question_resolved or session_question_resolved
        )
        progress.session_close_requested = bool(
            progress.session_close_requested or session_close_requested
        )
        progress.deliberate_cliffhanger = (
            progress.deliberate_cliffhanger or bool(deliberate_cliffhanger)
        )
        if public_image:
            self._append_unique(progress.public_images, public_image, limit=12)
            if not progress.memory_image:
                progress.memory_image = str(public_image).strip()[:300]
        if signature_image_evolved:
            progress.signature_image_evolved = True
        clean_opening_signature = str(opening_signature_realized or "").strip()
        if clean_opening_signature:
            progress.opening_signature = clean_opening_signature[:500]
            progress.opening_signature_realized = True
            if scene_progress is not None:
                scene_progress.opening_signature = clean_opening_signature[:500]
                scene_progress.opening_signature_realized = True
        if session_question_resolved or session_close_requested:
            progress.closure_stage = "payoff_due"
        if awaits_player_response:
            progress.awaiting_player_response = True
            progress.pending_player_prompt = str(public_image or action_summary or "").strip()[:500]
            progress.pending_player_scene_id = str(progress.active_scene_id or "")
            if progress.closure_stage in {
                "payoff_due",
                "aftermath_acknowledged",
            }:
                progress.closure_stage = "aftermath_open"
        elif (
            not player_action
            and progress.closure_stage == "payoff_due"
            and (signature_image_evolved or deliberate_cliffhanger)
        ):
            progress.closure_stage = "aftermath_acknowledged"
        clean_hook = str(next_session_hook or "").strip()
        if clean_hook:
            self._append_unique(progress.next_session_hooks, clean_hook, limit=8)
        if callback_to_previous:
            self._append_unique(progress.callback_events, callback_to_previous, limit=6)
            progress.previous_consequence_recalled = True
        if consequence and not progress.memory_consequence:
            progress.memory_consequence = str(consequence).strip()[:300]

        decisive_beat = bool(
            climax
            or local_question_changed
            or local_question_resolved
            or deliberate_cliffhanger
            or signature_image_evolved
        )
        if decisive_beat and action_summary:
            memory_choice = self._memory_choice_candidate(action_summary)
            if memory_choice:
                progress.memory_choice = memory_choice
        if decisive_beat and consequence:
            progress.memory_consequence = str(consequence).strip()[:300]
        if signature_image_evolved and public_image:
            progress.memory_image = str(public_image).strip()[:300]

        self._refresh_substantial_scene(progress, scene_progress)

        # A clue is not automatically a reversal. The latter means public
        # evidence changes how the table understands the central question.
        if (
            reversal
            and (reveal or consequence or opposition_move)
            and progress.stage in {"opening", "development"}
        ):
            progress.stage = "reversal"
            progress.reversal_reached = True
        if climax:
            progress.stage = "climax"
        elif progress.stage == "opening" and progress.meaningful_turns >= 4:
            progress.stage = "development"
        progress.closure_ready = self._episode_evidence_complete(progress)
        progress.last_event = str(climax or reveal or consequence or action_summary or "本场继续推进").strip()[:300]
        return progress

    def observe_scene_ended(
        self,
        scene_id: str,
        *,
        summary: str = "",
        close_reason: str = "",
    ) -> SessionEpisodeProgress:
        progress = self._ensure_session_progress(self.story_arc_manager.state.current_pacing_plan.session_number)
        clean_scene_id = str(scene_id or "").strip()
        scene_progress = progress.scene_progress.get(clean_scene_id)
        if scene_progress is not None:
            scene_progress.ended = True
            scene_progress.close_reason = str(close_reason or summary or "").strip()[:300]
            self._refresh_substantial_scene(progress, scene_progress)
        if progress.active_scene_id == clean_scene_id:
            progress.active_scene_id = ""
        if len(progress.substantial_scene_ids) >= 1 and progress.stage == "opening":
            progress.stage = "development"
        if summary and progress.climax_events and progress.concrete_consequences:
            progress.stage = "closure"
        progress.closure_ready = self._episode_evidence_complete(progress)
        progress.last_event = f"场景结束：{summary or scene_id}"[:300]
        return progress

    @staticmethod
    def _active_scene_progress(
        progress: SessionEpisodeProgress,
    ) -> SessionSceneProgress | None:
        scene_id = str(progress.active_scene_id or "").strip()
        if not scene_id:
            return None
        return progress.scene_progress.setdefault(
            scene_id,
            SessionSceneProgress(scene_id=scene_id),
        )

    @staticmethod
    def _refresh_substantial_scene(
        progress: SessionEpisodeProgress,
        scene_progress: SessionSceneProgress | None,
    ) -> None:
        if scene_progress is None or not scene_progress.substantial:
            return
        scene_id = str(scene_progress.scene_id or "").strip()
        if scene_id and scene_id not in progress.substantial_scene_ids:
            progress.substantial_scene_ids.append(scene_id)

    def finish_session_progress(self) -> SessionEpisodeProgress:
        state = self.story_arc_manager.state
        progress = state.current_session_progress
        progress.closure_ready = self._episode_evidence_complete(progress)
        if progress.closure_ready:
            progress.stage = "closure"
            progress.closure_stage = "ended"
            progress.closing_mode = "narrative_closure"
            state.current_pacing_plan.dramatic_contract.status = "completed"
        elif state.current_pacing_plan.dramatic_contract.title:
            progress.closing_mode = "administrative_pause"
            state.current_pacing_plan.dramatic_contract.status = "continuing"
        history = state.session_progress_history
        history[:] = [item for item in history if item.session_number != progress.session_number]
        history.append(progress)
        history.sort(key=lambda item: item.session_number)
        if len(history) > 100:
            del history[:-100]
        return progress

    def feedback_from_episode(
        self,
        progress: SessionEpisodeProgress,
        *,
        unresolved_thread_count: int = 0,
        prior_villain_drought: int = 0,
        foreground_pressure_count: int = 0,
        pending_scene_commitment_count: int = 0,
    ) -> SessionFeedbackSignals:
        """Build production feedback from committed events only.

        Semantic long-run audits may later merge stricter conversation-quality
        judgments into this record.  This baseline deliberately does not claim
        an NPC or agency failure without transcript evidence.
        """

        memory_complete = bool(
            progress.memory_image
            and progress.memory_choice
            and progress.memory_consequence
        )
        cause_effect = bool(progress.player_choices and progress.concrete_consequences)
        local_payoff = bool(
            progress.local_payoffs
            or progress.local_question_changed
            or progress.local_question_resolved
            or progress.climax_events
        )
        pending_blocking_decisions = sum(
            1
            for window in self.world_state.decision_windows.values()
            if window.blocking and window.status == DecisionWindowStatus.PENDING
        )
        memory_similarity = self._recent_memory_similarity(progress)
        return SessionFeedbackSignals(
            session_number=max(1, int(progress.session_number or 1)),
            meaningful_turns=progress.meaningful_turns,
            scene_count=len(progress.substantial_scene_ids),
            unresolved_thread_count=max(0, int(unresolved_thread_count)),
            villain_drought_sessions=(
                0 if progress.opposition_moves else max(0, int(prior_villain_drought)) + 1
            ),
            resource_spend_events=progress.resource_spend_events,
            reveal_uptake=(
                1.0
                if progress.reversal_reached
                else 0.5
                if progress.revealed_changes
                else 0.0
            ),
            stalled_beats=progress.max_stagnant_player_turns // 3,
            foreground_pressure_count=max(0, int(foreground_pressure_count)),
            choice_count=len(progress.player_choices),
            consequence_count=len(progress.concrete_consequences),
            villain_move_observed=bool(progress.opposition_moves),
            reveal_understood=bool(
                progress.local_question_changed or progress.local_question_resolved
            ),
            resource_pressure_ratio=progress.resource_pressure_ratio,
            local_question_changed=progress.local_question_changed,
            local_question_resolved=progress.local_question_resolved,
            session_question_resolved=progress.session_question_resolved,
            session_close_requested=progress.session_close_requested,
            deliberate_cliffhanger=progress.deliberate_cliffhanger,
            reversal_reached=progress.reversal_reached,
            memory_anchor_complete=memory_complete,
            session_identity_distinct=bool(memory_complete and memory_similarity < 0.72),
            cause_effect_linked=cause_effect,
            gm_control_present=bool(progress.opposition_moves),
            npc_answer_complete=True,
            player_agency_preserved=True,
            signature_image_evolved=bool(
                progress.signature_image_evolved
            ),
            local_payoff_present=local_payoff,
            previous_consequence_recalled=(
                progress.session_number <= 1 or progress.previous_consequence_recalled
            ),
            memory_similarity_to_recent=memory_similarity,
            pending_blocking_decision_count=pending_blocking_decisions,
            pending_scene_commitment_count=max(
                0,
                int(pending_scene_commitment_count),
            ),
            notes=[progress.last_event] if progress.last_event else [],
        )

    def _recent_memory_similarity(self, progress: SessionEpisodeProgress) -> float:
        current = self._memory_anchor_text(progress)
        if not current:
            return 0.0
        scores = [
            self._text_ngram_similarity(self._memory_anchor_text(previous), current)
            for previous in self.story_arc_manager.state.session_progress_history[-3:]
            if previous.session_number != progress.session_number
            and self._memory_anchor_text(previous)
        ]
        return max(scores, default=0.0)

    @staticmethod
    def _memory_anchor_text(progress: SessionEpisodeProgress) -> str:
        return "|".join(
            part.strip()
            for part in (
                progress.memory_image,
                progress.memory_choice,
                progress.memory_consequence,
            )
            if str(part or "").strip()
        )

    @staticmethod
    def _text_ngram_similarity(left: str, right: str) -> float:
        def grams(value: str) -> set[str]:
            compact = "".join(str(value or "").lower().split())
            return {
                compact[index : index + 2]
                for index in range(max(0, len(compact) - 1))
                if compact[index : index + 2]
            }

        left_grams = grams(left)
        right_grams = grams(right)
        if not left_grams or not right_grams:
            return 0.0
        return len(left_grams & right_grams) / len(left_grams | right_grams)

    def record_feedback(self, feedback: SessionFeedbackSignals) -> SessionFeedbackSignals:
        history = self.story_arc_manager.state.session_feedback_history
        existing = next(
            (
                item
                for item in history
                if item.session_number == feedback.session_number
            ),
            None,
        )
        if existing is not None:
            feedback = self._merge_feedback(existing, feedback)
        history[:] = [item for item in history if item.session_number != feedback.session_number]
        history.append(feedback)
        history.sort(key=lambda item: item.session_number)
        if len(history) > 100:
            del history[:-100]
        return feedback

    @staticmethod
    def _merge_feedback(
        left: SessionFeedbackSignals,
        right: SessionFeedbackSignals,
    ) -> SessionFeedbackSignals:
        villain_moved = left.villain_move_observed or right.villain_move_observed
        notes = list(dict.fromkeys([*left.notes, *right.notes]))
        return SessionFeedbackSignals(
            session_number=right.session_number,
            meaningful_turns=max(left.meaningful_turns, right.meaningful_turns),
            scene_count=max(left.scene_count, right.scene_count),
            resource_spend_events=max(left.resource_spend_events, right.resource_spend_events),
            unresolved_thread_count=max(
                left.unresolved_thread_count,
                right.unresolved_thread_count,
            ),
            villain_drought_sessions=(
                0
                if villain_moved
                else max(left.villain_drought_sessions, right.villain_drought_sessions)
            ),
            reveal_uptake=max(left.reveal_uptake, right.reveal_uptake),
            stalled_beats=max(left.stalled_beats, right.stalled_beats),
            foreground_pressure_count=max(
                left.foreground_pressure_count,
                right.foreground_pressure_count,
            ),
            choice_count=max(left.choice_count, right.choice_count),
            consequence_count=max(left.consequence_count, right.consequence_count),
            villain_move_observed=villain_moved,
            reveal_understood=left.reveal_understood or right.reveal_understood,
            resource_pressure_ratio=max(
                left.resource_pressure_ratio,
                right.resource_pressure_ratio,
            ),
            local_question_changed=(
                left.local_question_changed or right.local_question_changed
            ),
            local_question_resolved=(
                left.local_question_resolved or right.local_question_resolved
            ),
            session_question_resolved=(
                left.session_question_resolved
                or right.session_question_resolved
            ),
            session_close_requested=(
                left.session_close_requested or right.session_close_requested
            ),
            deliberate_cliffhanger=(
                left.deliberate_cliffhanger or right.deliberate_cliffhanger
            ),
            reversal_reached=left.reversal_reached or right.reversal_reached,
            memory_anchor_complete=(
                left.memory_anchor_complete or right.memory_anchor_complete
            ),
            session_identity_distinct=(
                left.session_identity_distinct and right.session_identity_distinct
            ),
            cause_effect_linked=left.cause_effect_linked and right.cause_effect_linked,
            gm_control_present=left.gm_control_present and right.gm_control_present,
            npc_answer_complete=left.npc_answer_complete and right.npc_answer_complete,
            player_agency_preserved=(
                left.player_agency_preserved and right.player_agency_preserved
            ),
            signature_image_evolved=(
                left.signature_image_evolved or right.signature_image_evolved
            ),
            local_payoff_present=left.local_payoff_present or right.local_payoff_present,
            previous_consequence_recalled=(
                left.previous_consequence_recalled
                or right.previous_consequence_recalled
            ),
            memory_similarity_to_recent=max(
                left.memory_similarity_to_recent,
                right.memory_similarity_to_recent,
            ),
            pending_blocking_decision_count=max(
                left.pending_blocking_decision_count,
                right.pending_blocking_decision_count,
            ),
            pending_scene_commitment_count=max(
                left.pending_scene_commitment_count,
                right.pending_scene_commitment_count,
            ),
            notes=notes[:8],
        )

    def adopt_dramatic_contract(
        self,
        contract: SessionDramaticContract,
    ) -> SessionDramaticContract:
        """Install a prepared situation brief without scripting its outcome."""

        self.story_arc_manager.state.current_pacing_plan.dramatic_contract = contract
        self._remember_contract(contract)
        return contract

    def assess_session_completion(
        self,
        feedback: SessionFeedbackSignals,
    ) -> tuple[bool, list[str]]:
        """Decide whether a four-hour session has earned an ending.

        A cliffhanger is valid only after a concrete reversal; an arbitrary turn
        cap is never sufficient on its own.
        """

        plan = self.story_arc_manager.state.current_pacing_plan
        min_scenes = max(2, int(plan.expected_scene_count[0] or 2))
        min_turns = max(20, int(plan.expected_table_turns[0] or 20))
        decision = self.closure_policy.assess_completion(
            feedback,
            minimum_scenes=min_scenes,
            minimum_turns=min_turns,
        )
        return decision.can_end, list(decision.reasons)

    def pressure_budget(
        self,
        *,
        phase: StoryArcPhase | str | None = None,
        conflict_active: bool = False,
        boss_scene: bool = False,
    ) -> PressureBudget:
        phase_value = phase if isinstance(phase, StoryArcPhase) else StoryArcPhase(str(phase or self.story_arc_manager.state.phase.value))
        if boss_scene or phase_value == StoryArcPhase.FINALE:
            return PressureBudget(
                phase=phase_value,
                max_foreground_pressure_clocks=3,
                max_auto_advance_clocks=3,
                max_public_clock_lines=5,
                allow_multi_threat_pressure=True,
                boss_pressure_allowed=True,
                guidance=[
                    "Boss/终局场景可以让多个压力同时推进，但每个命刻必须职责清楚。",
                    "多命刻压迫要配合清晰预兆，让玩家知道哪些能阻止、哪些会爆发。",
                ],
            )
        if phase_value == StoryArcPhase.CRISIS:
            return PressureBudget(
                phase=phase_value,
                max_foreground_pressure_clocks=2,
                max_auto_advance_clocks=2 if conflict_active else 1,
                max_public_clock_lines=4,
                allow_multi_threat_pressure=True,
                guidance=[
                    "危机篇可以让两条压力同屏，但不要让所有后台威胁都同时自动推进。",
                    "至少保留一个可被玩家主动倒转或阻止的目标。",
                ],
            )
        if phase_value == StoryArcPhase.MIDPOINT:
            return PressureBudget(
                phase=phase_value,
                max_foreground_pressure_clocks=2,
                max_auto_advance_clocks=1,
                max_public_clock_lines=4,
                allow_multi_threat_pressure=False,
                guidance=[
                    "中盘适合一个主威胁加一个目标命刻；第二条威胁应多半留作后台或失败代价。",
                    "揭示比堆压力重要，让命刻服务于真相浮现。",
                ],
            )
        return PressureBudget(
            phase=phase_value,
            max_foreground_pressure_clocks=1,
            max_auto_advance_clocks=1,
            max_public_clock_lines=3,
            allow_multi_threat_pressure=False,
            guidance=[
                "开局和上升期只把一个威胁放到前台自动推进；其他威胁作为线索、后果或章节间压力。",
                "目标命刻可以并存，但不能和多个自动威胁一起碾压玩家。",
            ],
        )

    def auto_advance_after_turn(
        self,
        *,
        skip_names: set[str] | None = None,
        boss_scene: bool = False,
        conflict_active: bool = True,
        event_timing: str = "action_round_end",
    ):
        plan = self.refresh_plan(conflict_active=conflict_active, boss_scene=boss_scene)
        skip = skip_names or set()
        if (
            not boss_scene
            and not plan.pressure_budget.allow_multi_threat_pressure
            and self._names_include_pressure_clock(skip)
        ):
            return []
        names = self.auto_advance_clock_names(skip_names=skip, budget=plan.pressure_budget)
        return self.clock_manager.auto_advance_after_turn(
            skip_names=skip_names,
            allowed_names=set(names),
            limit=plan.pressure_budget.max_auto_advance_clocks,
            event_timing=event_timing,
        )

    def auto_advance_clock_names(self, *, skip_names: set[str], budget: PressureBudget) -> list[str]:
        candidates = [
            clock
            for clock in self.clock_manager.all()
            if clock.name not in skip_names
            and clock.auto_advance
            and clock.current < clock.max_segments
            and self._is_foreground(clock)
            and clock.clock_type in self._PRESSURE_TYPES
        ]
        ranked = sorted(candidates, key=self._clock_priority, reverse=True)
        return [clock.name for clock in ranked[: budget.max_auto_advance_clocks]]

    def formatted_public_clocks(
        self,
        *,
        boss_scene: bool = False,
        highlight_names: set[str] | None = None,
        only_highlighted: bool = False,
    ) -> list[str]:
        plan = self.refresh_plan(conflict_active=False, boss_scene=boss_scene)
        clocks = self._public_clock_selection(plan.pressure_budget)
        highlights = highlight_names or set()
        if only_highlighted:
            if not highlights:
                return []
            clocks = [clock for clock in clocks if clock.name in highlights]
        return [
            self.clock_manager.format_clock(
                clock,
                public=True,
                include_hint=highlight_names is None
                or clock.name in highlights
                or self._is_urgent_pressure_clock(clock),
            )
            for clock in clocks
        ]

    def prompt_clock_context(self) -> list[str]:
        plan = self.refresh_plan(conflict_active=False)
        visible = self._public_clock_selection(plan.pressure_budget)
        hidden_pressure = [
            clock
            for clock in self.clock_manager.all()
            if clock not in visible and clock.clock_type in self._PRESSURE_TYPES and clock.current < clock.max_segments
        ]
        lines = [self.clock_manager.format_clock(clock) for clock in visible]
        if hidden_pressure:
            names = "、".join(clock.name for clock in hidden_pressure[:4])
            lines.append(f"后台压力：{names}。它们不是当前前台焦点，除非失败代价、场景切换或 GM 主动切镜头。")
        return lines

    def _is_urgent_pressure_clock(self, clock: Clock) -> bool:
        if clock.clock_type not in self._PRESSURE_TYPES:
            return False
        max_segments = max(1, int(clock.max_segments or 0))
        remaining = max_segments - int(clock.current or 0)
        return remaining <= max(1, max_segments // 4)

    def prompt_guidance(self, *, conflict_active: bool = False, boss_scene: bool = False) -> str:
        plan = self.refresh_plan(conflict_active=conflict_active, boss_scene=boss_scene)
        budget = plan.pressure_budget
        parts = [
            "战役节奏控制（后台使用，不要原样念给玩家）：",
            f"目标长度 {self.story_arc_manager.state.pacing_profile.target_sessions} 场，每场约 {self.story_arc_manager.state.pacing_profile.session_hours} 小时。",
            f"当前为第 {plan.session_number} 场，{plan.arc_title}，阶段 {plan.phase.value}。",
            f"本场桌面粒度：建议 {plan.expected_scene_count[0]}-{plan.expected_scene_count[1]} 个场景片段，约 {plan.expected_table_turns[0]}-{plan.expected_table_turns[1]} 次有意义桌面交换。",
            self._episode_progress_prompt(self.story_arc_manager.state.current_session_progress),
            f"本场结构：{'；'.join(plan.session_structure[:5])}",
            self._dramatic_contract_prompt(plan.dramatic_contract),
            f"GM主动节拍：{'；'.join(plan.gm_autonomy_cadence[:4])}",
            f"反派节奏：{plan.villain_cadence}",
            f"Boss节奏：{plan.boss_cadence}",
            f"命刻预算：前台压力最多 {budget.max_foreground_pressure_clocks} 个，自动推进最多 {budget.max_auto_advance_clocks} 个。",
        ]
        if budget.guidance:
            parts.append("压力准则：" + "；".join(budget.guidance[:3]))
        if plan.gm_notes:
            parts.append("GM注意：" + "；".join(plan.gm_notes[:3]))
        return "；".join(parts)

    def audit_payload(self) -> dict[str, object]:
        # Dashboard reads must never create a session contract, call an LLM, or
        # advance pacing state. Planning happens at explicit session lifecycle
        # boundaries; the audit endpoint only exposes the committed snapshot.
        plan = self.story_arc_manager.state.current_pacing_plan
        foreground = self._public_clock_selection(plan.pressure_budget)
        all_pressure = [clock for clock in self.clock_manager.all() if clock.clock_type in self._PRESSURE_TYPES]
        return {
            "active": True,
            "profile": asdict(self.story_arc_manager.state.pacing_profile),
            "current_plan": asdict(plan),
            "current_session_progress": asdict(self.story_arc_manager.state.current_session_progress),
            "session_identity_prep_audit": asdict(
                self.contract_planner.concretizer.last_identity_assessment
            ),
            "session_prep_concretizer": {
                "model": self.contract_planner.concretizer.model,
                "llm_enabled": bool(
                    self.contract_planner.concretizer.client
                    and self.contract_planner.concretizer.model
                ),
                "last_error": self.contract_planner.concretizer.last_error,
                "gatekeeper_repair_status": (
                    self.contract_planner.concretizer.last_gatekeeper_repair_status
                ),
                "gatekeeper_repair_attempts": (
                    self.contract_planner.concretizer.last_gatekeeper_repair_attempts
                ),
                "gatekeeper_repair_error": (
                    self.contract_planner.concretizer.last_gatekeeper_repair_error
                ),
                "last_cache_hit": (
                    self.contract_planner.concretizer.last_cache_hit
                ),
                "cache_hit_count": (
                    self.contract_planner.concretizer.cache_hit_count
                ),
            },
            "foreground_clock_names": [clock.name for clock in foreground],
            "background_pressure_names": [
                clock.name for clock in all_pressure if clock.name not in {item.name for item in foreground}
            ],
            "usage_note": "战役节奏器只控制场次密度、压力预算和反派节奏；不替玩家或 GM 写死剧情。",
        }

    def _public_clock_selection(self, budget: PressureBudget) -> list[Clock]:
        unfinished = [clock for clock in self.clock_manager.all() if clock.current < clock.max_segments and self._is_foreground(clock)]
        goals = [clock for clock in unfinished if clock.clock_type in self._GOAL_TYPES]
        pressure = [clock for clock in unfinished if clock.clock_type in self._PRESSURE_TYPES]
        selected_pressure = sorted(pressure, key=self._clock_priority, reverse=True)[: budget.max_foreground_pressure_clocks]
        selected: list[Clock] = []
        for clock in [*selected_pressure, *goals]:
            if clock not in selected:
                selected.append(clock)
            if len(selected) >= budget.max_public_clock_lines:
                break
        return selected

    def _clock_priority(self, clock: Clock) -> tuple[float, int, int]:
        max_segments = max(1, int(clock.max_segments or 1))
        ratio = float(clock.current or 0) / max_segments
        type_weight = {"boss": 5, "villain": 4, "threat": 3, "dungeon": 2}.get(str(clock.clock_type), 1)
        return (ratio, type_weight, int(clock.pacing_weight or 1))

    def _is_foreground(self, clock: Clock) -> bool:
        return str(clock.visibility or "foreground").strip().lower() not in {"background", "hidden", "dormant", "后台"}

    def _names_include_pressure_clock(self, names: Iterable[str]) -> bool:
        for name in names:
            if not self.clock_manager.exists(name):
                continue
            if self.clock_manager.get(name).clock_type in self._PRESSURE_TYPES:
                return True
        return False

    def _phase_for_session(self, session_number: int, profile: CampaignPacingProfile) -> StoryArcPhase:
        target = max(1, int(profile.target_sessions or 35))
        ratio = session_number / target
        if ratio >= 0.86:
            return StoryArcPhase.FINALE
        if ratio >= 0.68:
            return StoryArcPhase.CRISIS
        if ratio >= 0.45:
            return StoryArcPhase.MIDPOINT
        if ratio >= 0.18:
            return StoryArcPhase.RISING
        return StoryArcPhase.OPENING

    def _arc_index(self, session_number: int, profile: CampaignPacingProfile) -> int:
        size = max(1, round(profile.target_sessions / max(1, profile.target_arcs)))
        return min(max(1, profile.target_arcs), ((session_number - 1) // size) + 1)

    def _arc_title(self, arc_index: int, total: int) -> str:
        if arc_index <= 1:
            return "第一幕"
        if arc_index >= total:
            return "终幕"
        return f"第{arc_index}幕"

    def _recommended_arc_count(self, target_sessions: int) -> int:
        if target_sessions <= 24:
            return 4
        if target_sessions >= 45:
            return 6
        return 5

    def _expected_table_turns(self, profile: CampaignPacingProfile) -> tuple[int, int]:
        """A four-hour Fabula Ultima session should not collapse into a summary.

        These are not hard gameplay rules; they are backstage pacing targets for
        tests and prompts so GM output keeps scenes breathing.
        """

        hours = max(1, int(profile.session_hours or 4))
        lower = max(20, hours * 7)
        upper = max(lower + 8, hours * 12)
        return (lower, upper)

    def _strong_start_hint(self, phase: StoryArcPhase) -> str:
        if phase == StoryArcPhase.OPENING:
            return "用一个能展示世界希望与问题的现场开局，让英雄立刻面对可行动选择。"
        if phase == StoryArcPhase.RISING:
            return "用玩家上次选择造成的变化开局，同时露出反派计划的可见后果。"
        if phase == StoryArcPhase.MIDPOINT:
            return "用一枚能改写理解的证据或 NPC 失态开局，不要直接公布真相。"
        if phase == StoryArcPhase.CRISIS:
            return "用反派胜利条件逼近开局，把代价和可阻止路径同时放到镜头里。"
        return "用最终威胁改变世界的画面开局，让每个英雄主题都能进入终局选择。"

    def _session_structure(self, phase: StoryArcPhase) -> list[str]:
        common = [
            "冷开场：从上次选择的后果或一个正在发生的麻烦开始",
            "展开：至少两个可变场景，让调查、交涉、旅行或冲突改变局面",
            "转折：证词、代价或反派行动改变玩家对本场问题的理解",
            "高潮：由玩家选择引发的对决、仪式、逃亡、谈判或艰难取舍",
            "收束：解决或实质改变本场问题，留下具体画面、选择与后果作为记忆锚点",
        ]
        if phase in {StoryArcPhase.CRISIS, StoryArcPhase.FINALE}:
            common.insert(2, "高压段：可出现多命刻或 Boss 机制，但每条命刻职责要清楚")
        else:
            common.insert(2, "压力段：最多一个自动威胁命刻进入前台")
        return common

    def _ensure_session_progress(self, session_number: int) -> SessionEpisodeProgress:
        state = self.story_arc_manager.state
        progress = state.current_session_progress
        if progress.session_number != session_number:
            progress = SessionEpisodeProgress(session_number=session_number)
            state.current_session_progress = progress
        return progress

    def _continuation_contract_due(self, session_number: int) -> bool:
        state = self.story_arc_manager.state
        previous_plan = state.current_pacing_plan
        previous_progress = state.current_session_progress
        return bool(
            previous_plan.dramatic_contract.title
            and previous_plan.dramatic_contract.status == "continuing"
            and previous_plan.session_number < session_number
            and previous_progress.session_number == previous_plan.session_number
            and not previous_progress.closure_ready
        )

    @staticmethod
    def _append_unique(values: list[str], value: str, *, limit: int) -> None:
        clean = str(value or "").strip()
        if not clean or clean in values:
            return
        values.append(clean[:500])
        if len(values) > limit:
            del values[:-limit]

    @staticmethod
    def _memory_choice_candidate(value: str) -> str:
        """Exclude window acknowledgements from the session memory anchor."""

        clean = " ".join(str(value or "").split()).strip()
        compact = clean.strip("。！？!?，,；;：:")
        if not compact:
            return ""
        if compact in {
            "投",
            "投骰",
            "重掷",
            "不重掷",
            "揭示",
            "发现",
            "不用",
            "不使用",
            "不消耗物语点",
            "嗯",
            "好",
            "可以",
        }:
            return ""
        if len(compact) <= 32 and compact.startswith(
            ("目标是", "我援用", "援用特质", "援用身份", "援用主题", "援用故乡")
        ):
            return ""
        return clean[:300]

    def _episode_evidence_complete(self, progress: SessionEpisodeProgress) -> bool:
        plan = self.story_arc_manager.state.current_pacing_plan
        min_scenes = max(2, int(plan.expected_scene_count[0] or 2))
        min_turns = max(20, int(plan.expected_table_turns[0] or 20))
        feedback = self.feedback_from_episode(progress)
        enough_scene_evidence = bool(
            len(progress.substantial_scene_ids) >= min_scenes
            or self.closure_policy.dense_two_scene_resolution(
                feedback,
                minimum_turns=min_turns,
            )
        )
        local_payoff = (
            bool(progress.local_payoffs)
            or progress.local_question_changed
            or progress.local_question_resolved
            or (progress.deliberate_cliffhanger and progress.reversal_reached)
        )
        return bool(
            enough_scene_evidence
            and progress.meaningful_turns >= min_turns
            and progress.player_choices
            and progress.concrete_consequences
            and progress.climax_events
            and progress.opposition_moves
            and local_payoff
            and (
                progress.session_question_resolved
                or (
                    progress.deliberate_cliffhanger
                    and progress.reversal_reached
                )
            )
            and progress.signature_image_evolved
            and progress.memory_image
            and progress.memory_choice
            and progress.memory_consequence
            and not progress.awaiting_player_response
            and (
                not progress.opening_signature
                or progress.opening_signature_realized
            )
        )

    @staticmethod
    def _episode_progress_prompt(progress: SessionEpisodeProgress) -> str:
        return (
            "本场实际进展（后台使用）："
            f"阶段={progress.stage}；实质行动={progress.meaningful_turns}；"
            f"实质场景段落={len(progress.substantial_scene_ids)}/{len(progress.scene_ids)}；"
            f"已发生选择={len(progress.player_choices)}；已兑现后果={len(progress.concrete_consequences)}；"
            f"局部回报={len(progress.local_payoffs)}；"
            f"转折证据={len(progress.revealed_changes)}；高潮事件={len(progress.climax_events)}；"
            f"连续未产生局面变化的行动权重={progress.stagnant_player_turns}；"
            f"可收束={'是' if progress.closure_ready else '否'}。"
            "没有实际发生的计划不得算作进展；未可收束时不要宣布下一场开始。"
            + (
                "当前已明显停滞：下一次GM回应必须让行动对象、NPC、环境或对立方产生具体变化，"
                "必要时让对立方立即行动或切换镜头；不要继续复述玩家手段。"
                if progress.stagnant_player_turns >= 3
                else ""
            )
        )

    def _remember_contract(self, contract: SessionDramaticContract) -> None:
        history = self.story_arc_manager.state.session_contract_history
        for index, existing in enumerate(history):
            if existing.session_number == contract.session_number:
                history[index] = contract
                return
        history.append(contract)
        history.sort(key=lambda item: item.session_number)
        if len(history) > 100:
            del history[:-100]

    @staticmethod
    def _dramatic_contract_prompt(contract: SessionDramaticContract) -> str:
        return (
            "本场戏剧契约（后台使用，不要逐项念给玩家）："
            f"标题={contract.title}；地点={contract.location}；核心问题={contract.dramatic_question}；"
            f"开场扰动={contract.opening_disruption}；标志画面={contract.signature_image}；"
            f"聚光角色={contract.spotlight_hero or '按桌面参与度选择'}；"
            f"对立目标={contract.opposition_goal}；抉择={contract.dilemma}；"
            f"可变秘密={'｜'.join(contract.flexible_secrets[:2])}；"
            f"潜在场景={'｜'.join(item.title for item in contract.potential_scenes[:5])}；"
            f"线索路径={'｜'.join(f'{item.approach}:{item.visible_lead}' for item in contract.clue_routes[:3])}；"
            f"主动NPC={'｜'.join(f'{item.name}:{item.goal_now};要求={item.concrete_demand};接受={item.acceptance_rule};受阻={item.refusal_move}' for item in contract.important_npcs[:3])}；"
            f"奇幻细节={'｜'.join(contract.fantastic_details[:3])}；"
            f"升级阶梯={'｜'.join(contract.escalation_ladder[:3])}；"
            f"转折={contract.reversal}；高潮={contract.climax_type}；"
            f"收束={contract.closure_requirement}；可兑现结果={'｜'.join(contract.possible_payoffs[:3])}；"
            f"不可逆改变={contract.irreversible_change}；结尾回响={contract.ending_echo}；"
            f"记忆锚点={contract.memory_anchor}"
        )

    def _gm_autonomy_cadence(self, phase: StoryArcPhase, *, boss_scene: bool) -> list[str]:
        beats = [
            "开场先由 GM 给出地点、在场 NPC、可互动对象和眼前压力，再交给玩家选择。",
            "每过一轮主要玩家行动，若玩家只在原地打转，GM 用 NPC 明确答复、环境变化或短镜头推进局势。",
            "玩家追问 NPC 时，NPC 必须按动机给出可回应的答复；不要只复述玩家问题。",
            "玩家准备跳场前，GM 先收束本场已公开线索、未解决压力和下一步现场选择。",
        ]
        if boss_scene or phase in {StoryArcPhase.CRISIS, StoryArcPhase.FINALE}:
            beats.insert(2, "危机/Boss段允许反派主动行动，但要公开可阻止路径和代价。")
        else:
            beats.insert(2, "前期反派多用痕迹、代理人或后果出现，不要每场都把所有暗线推到台前。")
        return beats

    def _villain_cadence(
        self,
        session_number: int,
        profile: CampaignPacingProfile,
        phase: StoryArcPhase,
        *,
        boss_scene: bool,
    ) -> str:
        if boss_scene:
            return "反派可亲自登场，并消耗终结点改变局面。"
        if phase in {StoryArcPhase.CRISIS, StoryArcPhase.FINALE}:
            return "反派应主动推进计划或派出强代理人，后果要公开可见。"
        if session_number % max(2, profile.minor_climax_every_sessions) == 0:
            return "适合让反派代理人、痕迹或短镜头出现，但不必亲自收束。"
        return "以前兆、传闻、代理人压力或后果呈现反派，不要每场都亲自压场。"

    def _boss_cadence(self, session_number: int, profile: CampaignPacingProfile, *, boss_scene: bool) -> str:
        if boss_scene:
            return "当前就是 Boss/首领节奏，可以使用多阶段、多部件或多个命刻。"
        interval = max(3, profile.boss_every_sessions)
        remaining = interval - ((session_number - 1) % interval)
        if remaining <= 1:
            return "接近篇章小高潮：可以准备精英、悍将、地下城核心或小 Boss。"
        return f"距离下一次 Boss/小高潮建议还有约 {remaining} 场；本场更适合铺线索和选择。"

    def _gm_notes(self, phase: StoryArcPhase, budget: PressureBudget) -> list[str]:
        notes = [
            "准备局势、线索和 NPC 目标，不准备固定剧情路线。",
            "玩家失败时给画面、代价或替代路径，不让关键线索一次失败就消失。",
        ]
        if phase in {StoryArcPhase.OPENING, StoryArcPhase.RISING}:
            notes.append("前期少堆命刻，多让玩家理解世界、队伍和反派痕迹。")
        if budget.max_auto_advance_clocks <= 1:
            notes.append("如果已有一个威胁自动推进，其他威胁应留在后台，等待失败代价或切镜头。")
        return notes
