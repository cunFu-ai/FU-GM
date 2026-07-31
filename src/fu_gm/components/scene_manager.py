from __future__ import annotations

import ast
import re
from dataclasses import asdict, is_dataclass
from collections.abc import Callable
from typing import Any

from fu_gm.models import SceneRecord, SceneType


class SceneManager:
    """管理冲突以外的场景骨架与场景历史。"""

    def __init__(self) -> None:
        self.current_scene: SceneRecord | None = None
        self.history: list[SceneRecord] = []
        # A split party may create several still-active dramatic threads. Only
        # one is the camera focus at a time; the others are suspended rather
        # than ended so scene clocks, effects and pending decisions survive.
        self.suspended_scenes: list[SceneRecord] = []
        # Campaign-level last confirmed locations survive camera changes.  A
        # scene remains the current dramatic focus; this ledger prevents PCs
        # left outside that focus from silently ceasing to exist.
        self.actor_locations: dict[str, str] = {}
        # Fine-grained positions never participate in camera-branch routing.
        self.actor_positions: dict[str, str] = {}
        self._scene_counter = 0
        # A campaign can briefly be in a free scene without an explicit
        # SceneRecord (for example after loading an old save).  Keep the same
        # action-round semantics there and persist these fields with the scene
        # manager snapshot.
        self.free_action_round_number = 1
        self.free_action_round_required_actors: list[str] = []
        self.free_action_round_acted_actors: list[str] = []
        self.free_action_round_auto_advance_skip_names: list[str] = []
        self._start_listeners: list[Callable[[SceneRecord], None]] = []
        self._end_listeners: list[Callable[[SceneRecord], None]] = []
        self._focus_listeners: list[Callable[[SceneRecord], None]] = []

    def register_lifecycle_listener(
        self,
        *,
        on_start: Callable[[SceneRecord], None] | None = None,
        on_end: Callable[[SceneRecord], None] | None = None,
        on_focus: Callable[[SceneRecord], None] | None = None,
    ) -> None:
        if on_start is not None and on_start not in self._start_listeners:
            self._start_listeners.append(on_start)
        if on_end is not None and on_end not in self._end_listeners:
            self._end_listeners.append(on_end)
        if on_focus is not None and on_focus not in self._focus_listeners:
            self._focus_listeners.append(on_focus)

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
        if self.current_scene is not None:
            self.end_scene("场景被新的场景切换。")
        # Parallel branches share one table-level action round. Starting the
        # next scene for the focused branch must not reset time for branches
        # that are still active elsewhere.
        if not self.suspended_scenes:
            self._reset_free_action_round()
        self._scene_counter += 1
        normalized_participants = self.normalize_participants(participants or [])
        self.current_scene = SceneRecord(
            name=name,
            scene_type=scene_type,
            location=location,
            participants=normalized_participants,
            participant_locations={
                participant: str(location or "").strip()
                for participant in normalized_participants
            },
            objective=objective,
            summary=summary,
            scene_id=f"scene-{self._scene_counter}",
            session_opportunity_key=str(session_opportunity_key or "").strip(),
            session_opportunity_role=str(session_opportunity_role or "").strip(),
            session_opportunity_title=str(session_opportunity_title or "").strip(),
            session_opportunity_purpose=str(session_opportunity_purpose or "").strip(),
            session_opportunity_situation=str(session_opportunity_situation or "").strip(),
        )
        for participant in normalized_participants:
            self.actor_locations[participant] = str(location or "").strip()
            self.actor_positions.pop(participant, None)
        for listener in tuple(self._start_listeners):
            listener(self.current_scene)
        return self.current_scene

    def end_scene(self, summary: str = "") -> SceneRecord | None:
        if self.current_scene is None:
            return None
        if summary:
            self.current_scene.summary = summary
        self.current_scene.active = False
        ended = self.current_scene
        for listener in tuple(self._end_listeners):
            listener(ended)
        self.history.append(ended)
        self.current_scene = None
        if not self.suspended_scenes:
            self._reset_free_action_round()
        return ended

    def restore_latest_suspended(self) -> SceneRecord | None:
        """Return the camera to the most recently parked active branch."""

        if self.current_scene is not None:
            return self.current_scene
        if not self.suspended_scenes:
            return None
        scene = self.suspended_scenes.pop()
        scene.active = True
        self.current_scene = scene
        for listener in tuple(self._focus_listeners):
            listener(scene)
        return scene

    def end_all_scenes(self, summary: str = "") -> list[SceneRecord]:
        """End every active split-party branch as one lifecycle operation."""

        active: list[SceneRecord] = []
        seen_ids: set[str] = set()
        for scene in [self.current_scene, *self.suspended_scenes]:
            if scene is None:
                continue
            key = str(scene.scene_id or id(scene))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            active.append(scene)
        self.current_scene = None
        self.suspended_scenes = []
        for scene in active:
            if summary:
                scene.summary = summary
            scene.active = False
            for listener in tuple(self._end_listeners):
                listener(scene)
            if not any(item.scene_id == scene.scene_id for item in self.history):
                self.history.append(scene)
        self._reset_free_action_round()
        return active

    def focus_actor_branch(
        self,
        actor: str,
        *,
        name: str,
        scene_type: SceneType = SceneType.STANDARD,
        location: str = "",
        objective: str = "",
    ) -> tuple[SceneRecord, str]:
        """Move the camera to an actor's parallel scene without ending either branch.

        Returns ``(scene, mode)`` where mode is ``current``, ``restored`` or
        ``created``. This changes camera authority only; callers still commit
        the player's actual action through the appropriate typed action tool.
        """

        clean_actor = str(actor or "").strip()
        clean_location = str(location or self.location_of(clean_actor) or "").strip()
        if not clean_actor:
            raise ValueError("并行镜头必须指定玩家角色。")
        if self.current_scene is not None and clean_actor in self.current_scene.participants:
            return self.current_scene, "current"

        target_index = next(
            (
                index
                for index, scene in enumerate(self.suspended_scenes)
                if clean_actor in scene.participants
            ),
            -1,
        )
        if target_index < 0 and self.current_scene is not None and self._same_exact_location(
            self.current_scene.location,
            clean_location,
        ):
            self._join_actor_to_scene(
                self.current_scene,
                clean_actor,
                clean_location,
            )
            return self.current_scene, "joined"

        location_target_index = (
            next(
                (
                    index
                    for index, scene in enumerate(self.suspended_scenes)
                    if self._same_exact_location(scene.location, clean_location)
                ),
                -1,
            )
            if target_index < 0
            else -1
        )
        if self.current_scene is not None:
            self._suspend_current_scene()

        if target_index >= 0:
            # Suspending the previous focus may append one item, but it cannot
            # change the index of the earlier target.
            scene = self.suspended_scenes.pop(target_index)
            scene.active = True
            self.current_scene = scene
            for listener in tuple(self._focus_listeners):
                listener(scene)
            return scene, "restored"

        if location_target_index >= 0:
            scene = self.suspended_scenes.pop(location_target_index)
            self._join_actor_to_scene(scene, clean_actor, clean_location)
            scene.active = True
            self.current_scene = scene
            for listener in tuple(self._focus_listeners):
                listener(scene)
            return scene, "joined"

        self._scene_counter += 1
        scene = SceneRecord(
            name=str(name or f"{clean_actor}的镜头").strip(),
            scene_type=scene_type,
            location=clean_location,
            participants=[clean_actor],
            participant_locations={clean_actor: clean_location},
            objective=str(objective or "").strip(),
            scene_id=f"scene-{self._scene_counter}",
        )
        self.current_scene = scene
        self.actor_locations[clean_actor] = clean_location
        self.actor_positions.pop(clean_actor, None)
        for listener in tuple(self._start_listeners):
            listener(scene)
        return scene, "created"

    def coalesce_active_scenes_by_exact_location(self) -> dict[str, list[str]]:
        """Repair duplicate camera branches that represent the same room.

        Parallel scenes are useful only when the party is in distinct dramatic
        spaces. Older agent turns could create a second branch when another PC
        entered the already-focused destination. Merge those exact-location
        duplicates without treating either branch as ended.
        """

        primary = self.current_scene
        if primary is None:
            return {}
        duplicates = [
            scene
            for scene in self.suspended_scenes
            if scene.scene_type == primary.scene_type
            and self._same_exact_location(scene.location, primary.location)
        ]
        if not duplicates:
            return {}
        duplicate_ids: list[str] = []
        for duplicate in duplicates:
            duplicate_ids.append(str(duplicate.scene_id or ""))
            for participant in duplicate.participants:
                location = str(
                    duplicate.participant_locations.get(participant)
                    or duplicate.location
                    or primary.location
                ).strip()
                position = str(
                    duplicate.participant_positions.get(participant) or ""
                ).strip()
                self._join_actor_to_scene(primary, participant, location)
                if position:
                    primary.participant_positions[participant] = position
                    self.actor_positions[participant] = position
            for participant, activity in duplicate.participant_activities.items():
                if participant not in primary.participant_activities and str(activity or "").strip():
                    primary.participant_activities[participant] = activity
            for effect in duplicate.narrative_effects:
                if effect not in primary.narrative_effects:
                    primary.narrative_effects.append(effect)
            for condition in duplicate.open_conditions:
                if condition not in primary.open_conditions:
                    primary.open_conditions.append(condition)
            for field_name in (
                "objective",
                "session_opportunity_key",
                "session_opportunity_role",
                "session_opportunity_title",
                "session_opportunity_purpose",
                "session_opportunity_situation",
                "pending_transition_location",
                "pending_transition_reason",
            ):
                if not str(getattr(primary, field_name, "") or "").strip():
                    setattr(primary, field_name, getattr(duplicate, field_name, ""))
            for participant in duplicate.pending_transition_participants:
                if participant not in primary.pending_transition_participants:
                    primary.pending_transition_participants.append(participant)
            duplicate.active = False
        duplicate_id_set = set(duplicate_ids)
        self.suspended_scenes = [
            scene
            for scene in self.suspended_scenes
            if str(scene.scene_id or "") not in duplicate_id_set
        ]
        if not self.suspended_scenes:
            primary.action_round_number = max(
                1,
                int(self.free_action_round_number or primary.action_round_number or 1),
            )
            primary.action_round_required_actors = list(
                self.free_action_round_required_actors
                or primary.action_round_required_actors
            )
            primary.action_round_acted_actors = list(
                self.free_action_round_acted_actors
                or primary.action_round_acted_actors
            )
            primary.action_round_auto_advance_skip_names = list(
                self.free_action_round_auto_advance_skip_names
                or primary.action_round_auto_advance_skip_names
            )
            self._reset_free_action_round()
        return {str(primary.scene_id or ""): duplicate_ids}

    def _join_actor_to_scene(
        self,
        scene: SceneRecord,
        actor: str,
        location: str,
    ) -> None:
        clean_actor = str(actor or "").strip()
        clean_location = str(location or scene.location or "").strip()
        if not clean_actor:
            return
        for suspended in self.suspended_scenes:
            if suspended is scene or clean_actor not in suspended.participants:
                continue
            suspended.participants = [
                item for item in suspended.participants if item != clean_actor
            ]
            suspended.participant_locations.pop(clean_actor, None)
            suspended.participant_positions.pop(clean_actor, None)
            suspended.participant_activities.pop(clean_actor, None)
        if clean_actor not in scene.participants:
            scene.participants.append(clean_actor)
        scene.participant_locations[clean_actor] = clean_location
        self.actor_locations[clean_actor] = clean_location
        scene.participant_positions.pop(clean_actor, None)
        self.actor_positions.pop(clean_actor, None)

    @staticmethod
    def _same_exact_location(left: str, right: str) -> bool:
        def normalize(value: str) -> str:
            return re.sub(r"[\s，,。；;：:]+", "", str(value or "")).strip()

        left_normalized = normalize(left)
        right_normalized = normalize(right)
        return bool(left_normalized and left_normalized == right_normalized)

    def actors_share_movement_origin(self, left: str, right: str) -> bool:
        """Return whether two actors can truthfully depart together.

        Location labels may use different levels of precision. The reliable
        proof of co-presence is that both actors' latest valid scene record is
        the same branch; the location ledger prevents stale history records
        from authorizing movement after either actor has gone elsewhere.
        """

        left_scene = self._latest_valid_scene_for_actor(left)
        right_scene = self._latest_valid_scene_for_actor(right)
        if left_scene is None or right_scene is None:
            return self._same_exact_location(
                self.location_of(left),
                self.location_of(right),
            )
        return bool(
            str(left_scene.scene_id or "")
            and str(left_scene.scene_id or "") == str(right_scene.scene_id or "")
        )

    def _latest_valid_scene_for_actor(self, actor: str) -> SceneRecord | None:
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            return None
        candidates = [
            self.current_scene,
            *reversed(self.suspended_scenes),
            *reversed(self.history),
        ]
        authoritative_location = self.location_of(clean_actor)
        for scene in candidates:
            if scene is None or clean_actor not in scene.participants:
                continue
            recorded_location = str(
                scene.participant_locations.get(clean_actor)
                or scene.location
                or ""
            ).strip()
            if not authoritative_location or self._same_exact_location(
                authoritative_location,
                recorded_location,
            ):
                return scene
        return None

    def _suspend_current_scene(self) -> SceneRecord | None:
        scene = self.current_scene
        if scene is None:
            return None
        if not self.suspended_scenes:
            self.free_action_round_number = max(1, int(scene.action_round_number or 1))
            self.free_action_round_required_actors = list(scene.action_round_required_actors)
            self.free_action_round_acted_actors = list(scene.action_round_acted_actors)
            self.free_action_round_auto_advance_skip_names = list(
                scene.action_round_auto_advance_skip_names
            )
        self.suspended_scenes = [
            item for item in self.suspended_scenes if item.scene_id != scene.scene_id
        ]
        scene.active = True
        self.suspended_scenes.append(scene)
        self.current_scene = None
        return scene

    def active_scenes(self) -> list[SceneRecord]:
        scenes = list(self.suspended_scenes)
        if self.current_scene is not None:
            scenes.append(self.current_scene)
        return scenes

    def add_participant(self, name: str, *, location: str = "") -> bool:
        """Register a creature or speaking NPC as present in the current scene."""

        clean_names = self.normalize_participants([name])
        if self.current_scene is None or not clean_names:
            return False
        changed = False
        for clean_name in clean_names:
            # Rejoining the focused party moves the actor out of any parked
            # branch; otherwise one character would exist in two scenes.
            for suspended in self.suspended_scenes:
                if clean_name not in suspended.participants:
                    continue
                suspended.participants = [
                    item for item in suspended.participants if item != clean_name
                ]
                suspended.participant_locations.pop(clean_name, None)
                suspended.participant_positions.pop(clean_name, None)
                suspended.participant_activities.pop(clean_name, None)
                changed = True
            resolved_location = str(
                location
                or self.current_scene.location
                or self.actor_locations.get(clean_name, "")
            ).strip()
            if clean_name in self.current_scene.participants:
                self.set_participant_location(clean_name, resolved_location)
                continue
            self.current_scene.participants.append(clean_name)
            self.set_participant_location(clean_name, resolved_location)
            changed = True
        return changed

    def remove_participant(self, name: str) -> bool:
        """Remove an actor from every active branch without erasing history."""

        clean_name = str(name or "").strip()
        if not clean_name:
            return False
        changed = False
        for scene in [self.current_scene, *self.suspended_scenes]:
            if scene is None or clean_name not in scene.participants:
                continue
            scene.participants = [
                participant
                for participant in scene.participants
                if participant != clean_name
            ]
            scene.participant_locations.pop(clean_name, None)
            scene.participant_positions.pop(clean_name, None)
            scene.participant_activities.pop(clean_name, None)
            changed = True
        self.actor_locations.pop(clean_name, None)
        self.actor_positions.pop(clean_name, None)
        return changed

    def move_participants_to_location(
        self,
        names: list[str],
        location: str,
        *,
        scene_name: str = "",
        objective: str = "",
    ) -> tuple[SceneRecord, str]:
        """Move a resolved group together and focus its destination branch.

        This method is intentionally outcome-only.  Callers must first settle
        any check or NPC consent window.  A destination that already has an
        active split-party branch is reused; otherwise a new branch is opened
        without ending the branches left behind.
        """

        participants = self.normalize_participants(names)
        destination = str(location or "").strip()
        if not participants:
            raise ValueError("转场结果必须包含至少一个实际移动的人物。")
        if not destination:
            raise ValueError("转场结果缺少实际抵达地点。")

        target: SceneRecord | None = None
        mode = "current"
        if self.current_scene is not None and self._same_exact_location(
            self.current_scene.location,
            destination,
        ):
            target = self.current_scene
        else:
            target = next(
                (
                    scene
                    for scene in self.suspended_scenes
                    if self._same_exact_location(scene.location, destination)
                ),
                None,
            )

        if target is not None and target is not self.current_scene:
            if self.current_scene is not None:
                self._suspend_current_scene()
            self.suspended_scenes = [
                scene for scene in self.suspended_scenes if scene is not target
            ]
            target.active = True
            self.current_scene = target
            mode = "restored"
            for listener in tuple(self._focus_listeners):
                listener(target)
        elif target is None:
            if self.current_scene is not None:
                self._suspend_current_scene()
            self._scene_counter += 1
            target = SceneRecord(
                name=str(scene_name or destination).strip(),
                scene_type=SceneType.STANDARD,
                location=destination,
                participants=[],
                participant_locations={},
                objective=str(objective or "").strip(),
                scene_id=f"scene-{self._scene_counter}",
            )
            self.current_scene = target
            mode = "created"
            for listener in tuple(self._start_listeners):
                listener(target)

        for participant in participants:
            self._join_actor_to_scene(target, participant, destination)
        target.location = destination
        target.active = True
        return target, mode

    def set_participant_location(self, name: str, location: str) -> bool:
        """Persist the branch-level location used for scene membership."""

        clean_name = str(name or "").strip()
        clean_location = str(location or "").strip()
        if not clean_name:
            return False
        previous_location = self.actor_locations.get(clean_name)
        changed = previous_location != clean_location
        self.actor_locations[clean_name] = clean_location
        if self.current_scene is not None and clean_name in self.current_scene.participants:
            if self.current_scene.participant_locations.get(clean_name) != clean_location:
                self.current_scene.participant_locations[clean_name] = clean_location
                changed = True
            if previous_location != clean_location:
                self.current_scene.participant_positions.pop(clean_name, None)
                self.actor_positions.pop(clean_name, None)
        return changed

    def set_participant_position(self, name: str, position: str) -> bool:
        """Remember a stance inside the focused scene without moving branches."""

        clean_name = str(name or "").strip()
        clean_position = str(position or "").strip()
        if (
            self.current_scene is None
            or not clean_name
            or clean_name not in self.current_scene.participants
        ):
            return False
        changed = self.current_scene.participant_positions.get(clean_name) != clean_position
        if clean_position:
            self.current_scene.participant_positions[clean_name] = clean_position
            self.actor_positions[clean_name] = clean_position
        else:
            self.current_scene.participant_positions.pop(clean_name, None)
            self.actor_positions.pop(clean_name, None)
        return changed

    def record_participant_activity(self, name: str, activity: str) -> bool:
        """Remember one completed deterministic action in the current scene."""

        clean_name = str(name or "").strip()
        clean_activity = " ".join(str(activity or "").split()).strip()
        if self.current_scene is None or not clean_name or not clean_activity:
            return False
        if clean_name not in self.current_scene.participants:
            return False
        changed = self.current_scene.participant_activities.get(clean_name) != clean_activity
        self.current_scene.participant_activities[clean_name] = clean_activity
        return changed

    def location_of(self, name: str) -> str:
        clean_name = str(name or "").strip()
        if not clean_name:
            return ""
        if (
            self.current_scene is not None
            and clean_name in self.current_scene.participants
        ):
            value = self.current_scene.participant_locations.get(clean_name)
            if value is not None:
                return str(value or "").strip()
        return str(self.actor_locations.get(clean_name, "") or "").strip()

    def position_of(self, name: str) -> str:
        clean_name = str(name or "").strip()
        if not clean_name:
            return ""
        if (
            self.current_scene is not None
            and clean_name in self.current_scene.participants
        ):
            value = self.current_scene.participant_positions.get(clean_name)
            if value is not None:
                return str(value or "").strip()
        return str(self.actor_positions.get(clean_name, "") or "").strip()

    def participants_at(self, location: str) -> list[str]:
        clean_location = str(location or "").strip()
        if not clean_location:
            return []
        return [
            name
            for name, value in self.actor_locations.items()
            if str(value or "").strip() == clean_location
        ]

    @classmethod
    def normalize_participants(cls, values: list[Any]) -> list[str]:
        """Flatten legacy stringified target lists into real participant names."""

        result: list[str] = []
        pending: list[Any] = list(values)
        while pending:
            value = pending.pop(0)
            if isinstance(value, (list, tuple, set)):
                pending[:0] = list(value)
                continue
            text = str(value or "").strip()
            if not text:
                continue
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    parsed = None
                if isinstance(parsed, (list, tuple, set)):
                    pending[:0] = list(parsed)
                    continue
            # Older semantic routes occasionally persisted several NPC targets
            # as one transport string.  A semicolon or pipe is never part of a
            # table-facing character name, so it is safe to repair on load.
            bundled = [
                item.strip()
                for item in re.split(r"\s*(?:；|;|\|)\s*", text)
                if item.strip()
            ]
            if len(bundled) > 1:
                pending[:0] = bundled
                continue
            if text not in result:
                result.append(text)
        return result

    def is_participant(self, name: str) -> bool:
        clean_name = str(name or "").strip()
        return bool(
            self.current_scene is not None
            and clean_name
            and clean_name in self.current_scene.participants
        )

    def record_action_round_action(
        self,
        actor: str,
        active_actors: list[str],
        *,
        auto_advance_skip_names: set[str] | None = None,
    ) -> dict[str, Any]:
        """Record one meaningful free-scene action and report round progress.

        The required roster is frozen when a round begins.  A newly arriving
        actor may contribute immediately without making somebody else wait,
        while actors who have explicitly left the table are removed from the
        outstanding roster.  Repeated actions by the same actor count once.
        """

        clean_actor = str(actor or "").strip()
        active = self._unique_names(active_actors)
        if clean_actor and clean_actor not in active:
            active.append(clean_actor)
        if not active:
            # Internal and legacy callers may not carry a speaker envelope.
            # Treat that as a one-participant action round rather than falling
            # back to the old per-message clock semantics.
            clean_actor = clean_actor or "__anonymous_scene_actor__"
            active = [clean_actor]
        elif not clean_actor and len(active) == 1:
            clean_actor = active[0]
        if not clean_actor:
            return {
                "completed": False,
                "round_number": self._action_round_number(),
                "required": list(active),
                "acted": [],
                "waiting": list(active),
                "actor": "",
                "auto_advance_skip_names": [],
            }

        number, required, acted, round_skip_names = self._action_round_state()
        for name in sorted(auto_advance_skip_names or set()):
            clean_name = str(name or "").strip()
            if clean_name and clean_name not in round_skip_names:
                round_skip_names.append(clean_name)
        if not required:
            required = list(active)
        else:
            # Explicitly absent or departed actors must not deadlock time.
            required = [name for name in required if name in active]
            acted = [name for name in acted if name in required]
            if not required:
                required = list(active)

        # Mid-round arrivals are not added merely because they are visible in
        # the roster.  If they actually act, however, their contribution belongs
        # to this round and cannot create an extra tick.
        if clean_actor not in required:
            required.append(clean_actor)
        if clean_actor not in acted:
            acted.append(clean_actor)

        waiting = [name for name in required if name not in acted]
        completed = not waiting
        completed_round = number
        if completed:
            self._set_action_round_state(number + 1, [], [], [])
        else:
            self._set_action_round_state(number, required, acted, round_skip_names)
        return {
            "completed": completed,
            "round_number": completed_round,
            "next_round_number": number + 1 if completed else number,
            "required": list(required),
            "acted": list(acted),
            "waiting": waiting,
            "actor": clean_actor,
            "auto_advance_skip_names": list(round_skip_names),
        }

    @staticmethod
    def _unique_names(names: list[str]) -> list[str]:
        result: list[str] = []
        for name in names:
            clean = str(name or "").strip()
            if clean and clean not in result:
                result.append(clean)
        return result

    def _action_round_number(self) -> int:
        if self.suspended_scenes:
            return max(1, int(self.free_action_round_number or 1))
        if self.current_scene is not None:
            return max(1, int(self.current_scene.action_round_number or 1))
        return max(1, int(self.free_action_round_number or 1))

    def _action_round_state(self) -> tuple[int, list[str], list[str], list[str]]:
        if self.suspended_scenes:
            return (
                self._action_round_number(),
                list(self.free_action_round_required_actors),
                list(self.free_action_round_acted_actors),
                list(self.free_action_round_auto_advance_skip_names),
            )
        if self.current_scene is not None:
            return (
                self._action_round_number(),
                list(self.current_scene.action_round_required_actors),
                list(self.current_scene.action_round_acted_actors),
                list(self.current_scene.action_round_auto_advance_skip_names),
            )
        return (
            self._action_round_number(),
            list(self.free_action_round_required_actors),
            list(self.free_action_round_acted_actors),
            list(self.free_action_round_auto_advance_skip_names),
        )

    def action_round_snapshot(self) -> dict[str, Any]:
        """Return the persisted free-scene timing boundary for supervision."""

        number, required, acted, skip_names = self._action_round_state()
        return {
            "round_number": number,
            "required": list(required),
            "acted": list(acted),
            "waiting": [
                name for name in required if name not in set(acted)
            ],
            "auto_advance_skip_names": list(skip_names),
        }

    def _set_action_round_state(
        self,
        number: int,
        required: list[str],
        acted: list[str],
        auto_advance_skip_names: list[str],
    ) -> None:
        if self.suspended_scenes:
            self.free_action_round_number = max(1, int(number))
            self.free_action_round_required_actors = list(required)
            self.free_action_round_acted_actors = list(acted)
            self.free_action_round_auto_advance_skip_names = list(
                auto_advance_skip_names
            )
            return
        if self.current_scene is not None:
            self.current_scene.action_round_number = max(1, int(number))
            self.current_scene.action_round_required_actors = list(required)
            self.current_scene.action_round_acted_actors = list(acted)
            self.current_scene.action_round_auto_advance_skip_names = list(
                auto_advance_skip_names
            )
            return
        self.free_action_round_number = max(1, int(number))
        self.free_action_round_required_actors = list(required)
        self.free_action_round_acted_actors = list(acted)
        self.free_action_round_auto_advance_skip_names = list(auto_advance_skip_names)

    def _reset_free_action_round(self) -> None:
        self.free_action_round_number = 1
        self.free_action_round_required_actors = []
        self.free_action_round_acted_actors = []
        self.free_action_round_auto_advance_skip_names = []

    def record_narrative_effect(self, effect: Any) -> dict[str, Any]:
        """Persist a scene effect on a present entity without inventing stats."""

        if self.current_scene is None:
            raise ValueError("当前没有可承载该效果的场景。")
        target = str(getattr(effect, "target", "") or "").strip()
        if not self.is_participant(target):
            raise ValueError(f"{target or '该目标'}不在当前场景中。")
        if is_dataclass(effect):
            payload = asdict(effect)
        elif isinstance(effect, dict):
            payload = dict(effect)
        else:
            payload = {"target": target, "note": str(effect)}
        effect_key = str(payload.get("effect_key") or "").strip()
        if effect_key:
            self.current_scene.narrative_effects = [
                item
                for item in self.current_scene.narrative_effects
                if str(item.get("effect_key") or "") != effect_key
            ]
        self.current_scene.narrative_effects.append(payload)
        return payload

    def format_phase(self) -> str:
        if self.current_scene is None:
            return "自由场景"
        scene = self.current_scene
        type_text = {
            SceneType.STANDARD: "普通场景",
            SceneType.SESSION_ZERO: "Session 0 世界创建",
            SceneType.CONFLICT: "冲突场景",
            SceneType.INTERLUDE: "插曲场景",
            SceneType.GM: "GM场景",
            SceneType.REST: "休息场景",
            SceneType.TRAVEL: "旅行场景",
            SceneType.DUNGEON: "地下城场景",
        }[scene.scene_type]
        location = f"，地点：{scene.location}" if scene.location else ""
        objective = f"，目标：{scene.objective}" if scene.objective else ""
        return f"{type_text}（{scene.name}{location}{objective}）"
