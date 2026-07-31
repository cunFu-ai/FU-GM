from __future__ import annotations

from dataclasses import dataclass
import re

from fu_gm.models import Action, ActionResolution, SceneRecord


@dataclass(frozen=True)
class SceneTransitionAnchor:
    """A player-established landing point for the next scene."""

    location: str
    reason: str = ""
    participants: tuple[str, ...] = ()
    scene_name: str = ""
    objective: str = ""


class SceneTransitionCoordinator:
    """Commit cross-scene movement only after the world has answered it.

    Prepared scenes are movable possibilities, not authority over character
    location.  The semantic router describes the attempted movement; this
    coordinator waits for a non-blocked *and explicitly completed* movement
    resolution before making that destination authoritative for scene
    navigation and persistence.  A reply about a proposed trip, an NPC's
    permission, or a failed attempt must never move the camera by itself.
    """

    @classmethod
    def observe_structured_check_transition(
        cls,
        *,
        resolution: ActionResolution,
        public_reply: str,
    ) -> SceneTransitionAnchor | None:
        """Return a typed movement outcome after the final check is public.

        A provisional roll may carry the same source action, so neither its
        prose nor its requested destination is enough.  The transition becomes
        authoritative only after the final roll succeeds and the destination
        appears in the table-facing result.
        """

        source_action = resolution.payload.get("committed_source_action")
        if not isinstance(source_action, Action):
            source_action = resolution.action
        transition = source_action.parameters.get("success_transition")
        if not isinstance(transition, dict):
            return None
        if cls._resolution_blocks_transition(resolution):
            return None
        destination = " ".join(
            str(transition.get("destination") or "").split()
        ).strip()
        participants = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in list(transition.get("participants") or [])
                if str(item or "").strip()
            )
        )
        if not destination or not participants:
            raise ValueError("成功转场缺少权威目的地或参与者。")
        if not cls.public_reply_names_destination(public_reply, destination):
            raise ValueError(
                f"成功转场的公开结果没有明确写出目的地【{destination}】。"
            )
        return SceneTransitionAnchor(
            location=destination,
            reason=" ".join(str(public_reply or "").split()).strip()[:300],
            participants=participants,
            scene_name=" ".join(str(transition.get("scene_name") or "").split()).strip(),
            objective=" ".join(str(transition.get("objective") or "").split()).strip(),
        )

    @staticmethod
    def public_reply_names_destination(public_reply: str, destination: str) -> bool:
        """Accept a full location or its natural final place name.

        Players do not need to hear ``白花碑驿站·风铃廊`` every time once
        the parent location is established, but ``门外退路`` is too vague to
        authorize a durable move to ``风铃廊``.  This keeps persistence exact
        without forcing robotic fully-qualified prose.
        """

        reply = " ".join(str(public_reply or "").split()).strip()
        target = " ".join(str(destination or "").split()).strip()
        if not reply or not target:
            return False
        if target in reply:
            return True
        aliases = [
            part.strip()
            for part in re.split(r"[·/／>＞→]", target)
            if len(part.strip()) >= 3
        ]
        return bool(aliases and aliases[-1] in reply)

    @classmethod
    def observe_turn(
        cls,
        *,
        route_decision: dict[str, object] | None,
        resolution: ActionResolution,
        public_reply: str,
        scene: SceneRecord | None,
        actor: str = "",
    ) -> SceneTransitionAnchor | None:
        if scene is None:
            return None
        route = dict(route_decision or {})
        if str(route.get("movement_scope") or "none") != "cross_scene":
            return None
        if not bool(route.get("performed_action")) or bool(route.get("table_proposal_only")):
            return None
        destination = " ".join(str(route.get("movement_destination") or "").split()).strip()
        if not destination or not str(public_reply or "").strip():
            return None
        if cls._resolution_blocks_transition(resolution):
            return None
        if not cls._movement_resolution_matches(
            resolution,
            destination,
            public_reply=public_reply,
            companion_resolution_required=bool(route.get("movement_companions")),
        ):
            return None

        participants = cls._participants(
            route,
            actor=actor,
            resolution=resolution,
        )
        reason = " ".join(
            str(
                public_reply
                or route.get("action_evidence_text")
                or route.get("action_summary")
                or route.get("action_goal")
            ).split()
        ).strip()[:300]
        scene.pending_transition_location = destination
        scene.pending_transition_reason = reason
        scene.pending_transition_participants = list(participants)
        # The current camera has physically followed the resolved movement.  The
        # pending fields preserve why this location outranks prepared material
        # when the next formal scene record is opened.
        scene.location = destination
        return SceneTransitionAnchor(
            location=destination,
            reason=reason,
            participants=participants,
        )

    @staticmethod
    def anchor_for_scene(scene: SceneRecord | None) -> SceneTransitionAnchor | None:
        if scene is None:
            return None
        location = " ".join(str(scene.pending_transition_location or "").split()).strip()
        if not location:
            return None
        return SceneTransitionAnchor(
            location=location,
            reason=str(scene.pending_transition_reason or "").strip(),
            participants=tuple(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in scene.pending_transition_participants
                    if str(item or "").strip()
                )
            ),
        )

    @staticmethod
    def _resolution_blocks_transition(resolution: ActionResolution) -> bool:
        payload = dict(resolution.payload or {})
        parameters = dict(resolution.action.parameters or {})
        if payload.get("check_result_provisional"):
            return True
        if any(
            bool(payload.get(key) or parameters.get(key))
            for key in (
                "scene_access_blocked",
                "rules_blocked",
                "action_blocked",
                "decision_window_guard",
            )
        ):
            return True
        roll = payload.get("roll")
        if roll is not None and not bool(getattr(roll, "success", False)):
            return True
        return False

    @staticmethod
    def _movement_resolution_matches(
        resolution: ActionResolution,
        destination: str,
        *,
        public_reply: str,
        companion_resolution_required: bool = False,
    ) -> bool:
        """Return whether this transaction visibly completed this exact move.

        ``movement_scope`` describes only the player's attempt.  The resolver
        that owns the fiction must additionally set ``movement_resolved`` and
        name the landing point.  Keeping this proof structured prevents an
        NPC's unrelated answer from silently moving the whole scene.
        """

        payload = dict(resolution.payload or {})
        action = resolution.action
        parameters = dict(action.parameters or {})
        resolved = bool(
            parameters.get("movement_resolved")
            or payload.get("movement_resolved")
        )
        if not resolved:
            return False
        actual_destination = " ".join(
            str(
                parameters.get("resolved_movement_destination")
                or payload.get("resolved_movement_destination")
                or ""
            ).split()
        ).strip()
        if not (actual_destination and actual_destination == destination):
            return False
        if companion_resolution_required:
            participants = list(
                parameters.get("resolved_movement_participants")
                or payload.get("resolved_movement_participants")
                or []
            )
            if not participants:
                return False
        # The public text still has to name the landing point, but prose can
        # never create the structured result by itself.
        return destination in " ".join(str(public_reply or "").split())

    @staticmethod
    def _participants(
        route: dict[str, object],
        *,
        actor: str,
        resolution: ActionResolution | None = None,
    ) -> tuple[str, ...]:
        ordered = [str(actor or "").strip()]
        resolved: list[object] = []
        if resolution is not None:
            parameters = dict(resolution.action.parameters or {})
            payload = dict(resolution.payload or {})
            resolved = list(
                parameters.get("resolved_movement_participants")
                or payload.get("resolved_movement_participants")
                or []
            )
        companions = resolved or list(route.get("movement_companions") or [])
        ordered.extend(
            str(item or "").strip()
            for item in companions
            if str(item or "").strip()
        )
        return tuple(dict.fromkeys(item for item in ordered if item))
