from __future__ import annotations

from typing import Any

from fu_gm.components.campaign_pacing_manager import CampaignPacingManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.session_resource_tracker import SessionResourceTracker
from fu_gm.models import Action, ActionResolution, ActionType, ClockChange, SceneRecord

if False:  # pragma: no cover - typing-only import without a runtime cycle
    from fu_gm.components.scene_frame_manager import SceneFrame


class SessionEpisodeTracker:
    """Translate authoritative play events into pacing evidence.

    The tracker never invents a reversal or ending.  It only summarizes state
    changes already committed by the rules layer, keeping the large scene
    orchestrator free from another family of pacing-specific conditionals.
    """

    def __init__(
        self,
        pacing_manager: CampaignPacingManager,
        character_manager: CharacterManager,
    ) -> None:
        self.pacing_manager = pacing_manager
        self.character_manager = character_manager
        self.resource_tracker = SessionResourceTracker(character_manager)
        self.character_manager.register_resource_listener(self._resource_changed)

    def scene_started(self, scene: SceneRecord) -> None:
        progress = self.pacing_manager.observe_scene_started(
            scene.scene_id or scene.name,
            scene_role=str(getattr(scene, "session_opportunity_role", "") or ""),
            location=str(getattr(scene, "location", "") or ""),
        )
        self.resource_tracker.begin(progress)

    def scene_focused(self, scene: SceneRecord) -> None:
        self.pacing_manager.observe_scene_focused(scene.scene_id or scene.name)

    def scene_ended(self, scene: SceneRecord) -> None:
        self.pacing_manager.observe_scene_ended(
            scene.scene_id or scene.name,
            summary=scene.summary,
            close_reason=scene.summary,
        )

    def record_opening_image(self, text: str) -> None:
        progress = self.pacing_manager.observe_turn(
            player_action=False,
            public_image=self._first_sentence(text),
        )

    def turn_resolved(
        self,
        resolution: ActionResolution,
        *,
        player_message: str = "",
        public_reply: str = "",
        player_actor: str = "",
    ) -> None:
        action = self._effective_action(resolution)
        actor_name = str(action.parameters.get("actor") or player_actor or "").strip()
        player_action = bool(
            actor_name
            and self.character_manager.exists(actor_name)
            and "pc" in self.character_manager.get(actor_name).traits
            and action.action_type != ActionType.NPCACT
            and not action.parameters.get("gm_beat_request")
            and not action.parameters.get("scene_open_request")
        )
        committed_source = action is not resolution.action
        progress = self.pacing_manager.observe_turn(
            player_action=player_action,
            action_summary=self._action_summary(
                resolution,
                "" if committed_source else player_message,
            ) if player_action else "",
            consequence=self._consequence(resolution),
            local_payoff=self._local_payoff(resolution),
            reveal=self._reveal(resolution),
            reversal=bool(
                resolution.payload.get("session_reversal")
                or resolution.payload.get("reversal_reached")
                or action.parameters.get("session_reversal")
                or action.parameters.get("reversal_reached")
            ),
            climax=self._climax(resolution),
            opposition_move=self._opposition_move(resolution),
            public_image=self._first_sentence(public_reply),
            local_question_changed=bool(
                resolution.payload.get("local_question_changed")
                or action.parameters.get("local_question_changed")
            ),
            local_question_resolved=bool(
                resolution.payload.get("local_question_resolved")
                or action.parameters.get("local_question_resolved")
            ),
            deliberate_cliffhanger=bool(
                resolution.payload.get("deliberate_cliffhanger")
                or action.parameters.get("deliberate_cliffhanger")
            ),
            signature_image_evolved=bool(
                resolution.payload.get("signature_image_evolved")
                or action.parameters.get("signature_image_evolved")
            ),
            callback_to_previous=str(
                resolution.payload.get("callback_to_previous")
                or action.parameters.get("callback_to_previous")
                or ""
            ),
        )
        if progress is not None:
            self.resource_tracker.observe(progress)

    def finish_session(self):
        progress = self.pacing_manager.story_arc_manager.state.current_session_progress
        self.resource_tracker.observe(progress)
        return self.pacing_manager.finish_session_progress()

    def reconcile_scene_frames(self, frames: list[Any]) -> list[str]:
        """Backfill public completed bargains from legacy scene-frame saves.

        Older snapshots persisted finite NPC exchanges but did not forward them
        into the episode tracker.  Reconciliation is idempotent because the
        pacing manager de-duplicates local payoff text.
        """

        contract = self.pacing_manager.story_arc_manager.state.current_pacing_plan.dramatic_contract
        current_title = str(getattr(contract, "title", "") or "").strip()
        recovered: list[str] = []
        for frame in frames:
            if frame is None:
                continue
            frame_title = str(getattr(frame, "session_title", "") or "").strip()
            if current_title and frame_title and frame_title != current_title:
                continue
            for exchange in list(getattr(frame, "settled_exchanges", []) or []):
                if str(exchange.get("outcome") or "").strip() != "accepted":
                    continue
                if str(exchange.get("player_performance") or "").strip() != "complete":
                    continue
                terms = " ".join(str(exchange.get("settled_terms") or "").split()).strip()
                if not terms:
                    continue
                self.pacing_manager.observe_turn(
                    player_action=False,
                    local_payoff=terms[:500],
                )
                recovered.append(terms[:500])
        return list(dict.fromkeys(recovered))

    def _resource_changed(
        self,
        character_name: str,
        resource: str,
        before: int,
        after: int,
    ) -> None:
        story_arc_manager = getattr(self.pacing_manager, "story_arc_manager", None)
        if story_arc_manager is None:
            return
        progress = story_arc_manager.state.current_session_progress
        self.resource_tracker.record_change(
            progress,
            character_name=character_name,
            field_name=resource,
            before=before,
            after=after,
        )

    @staticmethod
    def _action_summary(resolution: ActionResolution, player_message: str) -> str:
        clean_message = str(player_message or "").strip()
        if clean_message:
            return clean_message[-500:]
        action = SessionEpisodeTracker._effective_action(resolution)
        actor = str(action.parameters.get("actor") or "英雄")
        target = str(action.parameters.get("target") or action.parameters.get("clock_name") or "").strip()
        return f"{actor}执行{action.action_type.value}{f'，目标是{target}' if target else ''}。"

    @classmethod
    def _consequence(cls, resolution: ActionResolution) -> str:
        payload = resolution.payload
        completed = cls._completed_clock_change(payload)
        if completed is not None:
            return str(
                completed.completion_consequence
                or payload.get("clock_completion_consequence")
                or f"命刻【{completed.clock_name}】完成。"
            ).strip()
        event = payload.get("conflict_event") or payload.get("zero_hp_event")
        summary = str(getattr(event, "summary", "") or "").strip()
        if summary:
            return summary
        explicit = cls._first_text(
            payload,
            "failure_consequence",
            "consequence",
            "story_change",
            "public_consequence",
            "project_completion",
            "ritual_completion",
        )
        if explicit:
            return explicit
        # Typed character actions may change the scene without damage, a check,
        # or a completed clock.  These committed effects are real pacing
        # consequences: treating them as mere declarations makes the beat
        # director believe that players acted but the world never responded.
        if payload.get("protect_reaction_triggered") and payload.get(
            "immediate_scene_protection"
        ):
            return cls._first_sentence(resolution.rules_text)
        if payload.get("spell_effect") is not None:
            return cls._first_sentence(resolution.rules_text)
        if any(
            cls._effective_action(resolution).parameters.get(flag)
            for flag in ("establish_fact", "scene_object_response", "care_action_response")
        ):
            summary = str(
                resolution.payload.get("summary")
                or cls._effective_action(resolution).parameters.get("summary")
                or ""
            ).strip()
            if summary:
                return summary[:500]
        damage_results = payload.get("damage_results")
        if isinstance(damage_results, list):
            affected = [
                str(item.get("target") or "").strip()
                for item in damage_results
                if isinstance(item, dict) and int(item.get("damage", 0) or 0) > 0
            ]
            if affected:
                return f"{'、'.join(affected)}在本次行动中实际失去生命值。"
        roll = payload.get("roll")
        if roll is not None and not bool(getattr(roll, "success", True)):
            failure = str(cls._effective_action(resolution).parameters.get("failure_consequence") or "").strip()
            if failure:
                return failure
        return ""

    @classmethod
    def _reveal(cls, resolution: ActionResolution) -> str:
        payload = resolution.payload
        explicit = cls._first_text(
            payload,
            "investigation_reveal",
            "revealed_clue",
            "reveal",
            "new_public_fact",
        )
        if explicit:
            return explicit
        values = payload.get("revealed_clues")
        if isinstance(values, list):
            return "；".join(str(item).strip() for item in values if str(item).strip())[:500]
        action = cls._effective_action(resolution)
        if action.action_type != ActionType.INVESTIGATE:
            return ""
        # A planned investigation keeps its answer on the source action while
        # the confirmation/reroll lifecycle stores the final roll in payload.
        # Only the committed successful result is pacing evidence: declarations
        # and provisional failures must not reveal the hidden answer early.
        if bool(payload.get("check_result_provisional")):
            return ""
        roll = payload.get("roll")
        succeeded = bool(getattr(roll, "success", False))
        if roll is None:
            receipt = payload.get("check_receipt")
            succeeded = bool(
                isinstance(receipt, dict) and receipt.get("success") is True
            )
        if succeeded:
            return str(
                action.parameters.get("success_observation")
                or action.parameters.get("success_answer")
                or ""
            ).strip()[:500]
        return ""

    @classmethod
    def _local_payoff(cls, resolution: ActionResolution) -> str:
        action = cls._effective_action(resolution)
        settlement_outcome = str(
            resolution.payload.get("settled_exchange_outcome")
            or action.parameters.get("settled_exchange_outcome")
            or ""
        ).strip()
        settlement_performance = str(
            resolution.payload.get("settled_exchange_player_performance")
            or action.parameters.get("settled_exchange_player_performance")
            or ""
        ).strip()
        if settlement_outcome == "accepted" and settlement_performance == "complete":
            terms = str(
                resolution.payload.get("settled_exchange_terms")
                or action.parameters.get("settled_exchange_terms")
                or ""
            ).strip()
            if terms:
                return terms[:500]
        resolved_id = str(
            resolution.payload.get("resolved_scene_condition_id")
            or action.parameters.get("resolved_scene_condition_id")
            or ""
        ).strip()
        if not resolved_id:
            return ""
        promised_result = str(
            resolution.payload.get("scene_condition_promised_result")
            or action.parameters.get("scene_condition_promised_result")
            or ""
        ).strip()
        if promised_result:
            return promised_result[:500]
        plan = (
            resolution.payload.get("scene_condition_npc_plan")
            or action.parameters.get("npc_speech_plan")
        )
        if isinstance(plan, dict) and str(plan.get("condition_outcome") or "").strip() == "fulfilled":
            return str(plan.get("promised_result") or "").strip()[:500]
        return ""

    def _climax(self, resolution: ActionResolution) -> str:
        explicit = self._first_text(
            resolution.payload,
            "session_climax",
            "climax",
            "decisive_result",
        )
        if explicit:
            return explicit
        completed = self._completed_clock_change(resolution.payload)
        if completed is not None and self._clock_completion_is_climactic(
            completed,
            resolution,
        ):
            return f"命刻【{completed.clock_name}】完成并实质改变了本场核心问题。"
        decisive = bool(
            resolution.payload.get("local_question_changed")
            or resolution.payload.get("local_question_resolved")
            or self._effective_action(resolution).parameters.get("local_question_changed")
            or self._effective_action(resolution).parameters.get("local_question_resolved")
        )
        action_type = self._effective_action(resolution).action_type
        if (
            decisive
            and action_type in {ActionType.CAST_RITUAL}
            and not resolution.payload.get("ritual_failed")
        ):
            return str(resolution.rules_text or "仪式完成并改变了局面。").strip()[:500]
        if (
            decisive and resolution.payload.get("project_completed")
        ) or resolution.payload.get("conflict_resolved"):
            return str(resolution.rules_text or "本场核心行动已经兑现结果。").strip()[:500]
        event = resolution.payload.get("conflict_event") or resolution.payload.get("zero_hp_event")
        if event is not None and str(getattr(event, "event_type", "")) in {
            "defeated",
            "surrendered",
            "sacrificed",
            "escaped",
        }:
            return str(getattr(event, "summary", "") or "冲突产生决定性结果。").strip()[:500]
        return ""

    def _clock_completion_is_climactic(
        self,
        change: ClockChange,
        resolution: ActionResolution,
    ) -> bool:
        if bool(
            resolution.payload.get("clock_is_session_climax")
            or self._effective_action(resolution).parameters.get("clock_is_session_climax")
        ):
            return True
        try:
            clock = self.pacing_manager.clock_manager.get(change.clock_name)
        except (KeyError, ValueError):
            return False
        return bool(
            clock.scope in {"session", "campaign"}
            and (
                clock.clock_type in {"boss", "villain"}
                or int(clock.pacing_weight or 0) >= 3
            )
        )

    def _opposition_move(self, resolution: ActionResolution) -> str:
        explicit = self._first_text(
            resolution.payload,
            "opposition_move",
            "villain_move",
            "npc_consequence",
        )
        if explicit:
            return explicit
        completed = self._completed_clock_change(resolution.payload)
        if completed is not None and completed.clock_type in {"threat", "villain", "boss", "crisis"}:
            return str(
                completed.completion_consequence
                or completed.stakes
                or f"命刻【{completed.clock_name}】完成，对立方取得了局面上的进展。"
            ).strip()[:500]
        actor_name = str(self._effective_action(resolution).parameters.get("actor") or "").strip()
        if not actor_name or not self.character_manager.exists(actor_name):
            return ""
        actor = self.character_manager.get(actor_name)
        if "pc" in actor.traits:
            return ""
        return str(resolution.rules_text or f"{actor_name}采取了行动。").strip()[:500]

    @staticmethod
    def _completed_clock_change(payload: dict[str, Any]) -> ClockChange | None:
        candidates: list[Any] = []
        for key in ("clock_change", "ritual_clock_change", "project_clock_change"):
            if payload.get(key) is not None:
                candidates.append(payload[key])
        for key in ("clock_changes", "auto_clock_changes"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        for candidate in candidates:
            if (
                isinstance(candidate, ClockChange)
                and candidate.max_segments > 0
                and candidate.after >= candidate.max_segments
            ):
                return candidate
        return None

    @staticmethod
    def _first_text(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return ""

    @staticmethod
    def _first_sentence(text: str) -> str:
        clean = " ".join(str(text or "").split())
        if not clean:
            return ""
        for marker in ("。", "！", "？", "!", "?"):
            if marker in clean:
                return clean.split(marker, 1)[0].strip()[:300] + marker
        return clean[:300]

    @staticmethod
    def _effective_action(resolution: ActionResolution) -> Action:
        committed = resolution.payload.get("committed_source_action")
        return committed if isinstance(committed, Action) else resolution.action
