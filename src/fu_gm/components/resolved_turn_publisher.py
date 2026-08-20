from __future__ import annotations

import time
from typing import Any

from fu_gm.conversation import plan_resolution_speech
from fu_gm.models import Action, ActionResolution, GamePanel
from fu_gm.turn_pipeline import TurnReplyContext


class ResolvedTurnPublisher:
    """Commit one resolved transaction and publish exactly one table reply."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def publish(
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
        host = self.host
        prior_scene_public_facts = list(
            host.scene_frame_manager.current_frame.public_facts
            if host.scene_frame_manager.current_frame is not None
            else []
        )
        if recovery:
            span["recovery"] = recovery
        if route_decision is not None:
            resolution.payload.setdefault("route_decision", dict(route_decision))
        span["post_check_windows"] = host._post_check_window_summary(resolution)
        span["combat_trait_events"] = host._combat_trait_event_summary(resolution)
        resolution.payload.setdefault(
            "speech_intent",
            plan_resolution_speech(resolution).to_dict(),
        )

        span["rules_ms"] = int(span.get("rules_ms", 0))
        phase_started = time.monotonic()
        host.resolution_committer.commit(resolution)
        span["memory_writeback_ms"] = int((time.monotonic() - phase_started) * 1000)
        resolution.payload["safety_guidance"] = host.safety_manager.render_guidance()
        host._attach_public_memory_to_resolution(resolution, panel)

        phase_started = time.monotonic()
        if resolution.action.parameters.get("player_facing_reply"):
            span["response_author"] = "focused_component"
            span["general_expressor_bypassed"] = True
        else:
            span["response_author"] = "general_expressor"
            span["general_expressor_bypassed"] = False
        try:
            raw_reply = host.turn_response_renderer.render(
                resolution,
                expressor=host.expressor,
            )
        except Exception as exc:
            # Rules have already produced an authoritative resolution at this
            # point. A prose-provider outage must not turn that valid result into
            # a rejected action or roll back spent resources. Only use the
            # expressor's deterministic rules renderer here; never invent a
            # heuristic result.
            canonical_expressor = getattr(host.expressor, "fallback", None)
            if canonical_expressor is None or not callable(
                getattr(canonical_expressor, "render", None)
            ):
                raise
            raw_reply = host.turn_response_renderer.render(
                resolution,
                expressor=canonical_expressor,
            )
            if not str(raw_reply or "").strip():
                raise
            span["expression_degraded"] = True
            span["expressor_error"] = str(exc)
            resolution.payload["expression_degraded"] = True
            if hasattr(host.expressor, "last_used_fallback"):
                host.expressor.last_used_fallback = True
        reply, reply_stages = host.turn_reply_pipeline.run(
            raw_reply,
            resolution,
            TurnReplyContext(
                recent_chat=recent_chat,
                prior_public_facts=tuple(prior_scene_public_facts),
            ),
        )
        if reply_stages:
            span["reply_pipeline_stages"] = reply_stages
        published_information = host.resolution_committer.publish(resolution, reply)
        if published_information:
            span["published_information"] = list(published_information)

        transition_actor = host._transition_actor_for_turn(
            resolution=resolution,
            route_decision=route_decision,
        )
        structured_transition = (
            host.scene_transition_coordinator.observe_structured_check_transition(
                resolution=resolution,
                public_reply=reply,
            )
        )
        transition_anchor = structured_transition
        if transition_anchor is not None:
            if host.conflict_manager.state.active:
                deferred = host.conflict_manager.register_exit_transition(
                    participants=list(transition_anchor.participants),
                    destination=transition_anchor.location,
                    scene_name=transition_anchor.scene_name,
                    objective=transition_anchor.objective,
                    reason=transition_anchor.reason,
                )
                for participant in transition_anchor.participants:
                    host.scene_manager.set_participant_location(
                        participant,
                        transition_anchor.location,
                    )
                span["scene_transition_anchor"] = {
                    **deferred,
                    "mode": "conflict_exit_pending",
                }
            else:
                moved_scene, transition_mode = host.scene_manager.move_participants_to_location(
                    list(transition_anchor.participants),
                    transition_anchor.location,
                    scene_name=transition_anchor.scene_name,
                    objective=transition_anchor.objective,
                )
                if host.scene_frame_manager.current_frame is None:
                    pacing_plan = getattr(
                        getattr(host.story_arc_manager, "state", None),
                        "current_pacing_plan",
                        None,
                    )
                    host.scene_frame_manager.ensure_frame(
                        scene=moved_scene,
                        recent_chat=reply,
                        world_state=host.world_state,
                        character_manager=host.character_manager,
                        contract=getattr(pacing_plan, "dramatic_contract", None),
                    )
                host.scene_frame_manager.synchronize_current_location(
                    transition_anchor.location
                )
                current_scene = host.scene_manager.current_scene
                for participant in transition_anchor.participants:
                    if (
                        not participant
                        or participant == transition_actor
                        or host._is_player_character(participant)
                    ):
                        continue
                    npc_name = host.world_state.resolve_npc_name(participant) or participant
                    host.world_state.update_npc_state(
                        npc_name,
                        location=transition_anchor.location,
                        scene=str(getattr(current_scene, "scene_id", "") or ""),
                    )
                span["scene_transition_anchor"] = {
                    "location": transition_anchor.location,
                    "participants": list(transition_anchor.participants),
                    "mode": transition_mode,
                }

        route_actor = str((route_decision or {}).get("actor") or "").strip()
        action_actor = str(action.parameters.get("actor") or "").strip()
        player_actor = route_actor or action_actor
        if not resolution.payload.get("check_result_provisional"):
            host.session_episode_tracker.turn_resolved(
                resolution,
                player_message=str(player_message or "").strip(),
                public_reply=reply,
                player_actor=player_actor,
            )
        host._audit_transparency(recent_chat, reply, resolution)
        span["expressor_ms"] = int((time.monotonic() - phase_started) * 1000)
        span["total_ms"] = int((time.monotonic() - total_started) * 1000)
        span["ok"] = True
        host._record_pipeline_span(span)
        return reply
