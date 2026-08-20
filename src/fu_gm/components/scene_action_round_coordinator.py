from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fu_gm.models import Action, ActionResolution, ActionType


class SceneActionRoundCoordinator:
    """Advance free-scene time only after every active PC has acted once."""

    _ACTOR_PARAMETER_KEYS = (
        "actor",
        "caster",
        "inventor",
        "opener",
        "explorer",
        "user",
        "payer",
        "buyer",
    )
    _POST_CHECK_FOLLOWUP_ACTIONS = {
        ActionType.INVOKE_TRAIT,
        ActionType.INVOKE_BOND,
        ActionType.TRIGGER_OPPORTUNITY,
    }

    def __init__(
        self,
        *,
        scenes: Any,
        characters: Any,
        world: Any,
        conflicts: Any,
        clocks: Any,
        pacing: Any,
        clock_lifecycle: Any | None = None,
        session_ledger: Any | None = None,
    ) -> None:
        self.scenes = scenes
        self.characters = characters
        self.world = world
        self.conflicts = conflicts
        self.clocks = clocks
        self.pacing = pacing
        self.clock_lifecycle = clock_lifecycle
        self.session_ledger = session_ledger

    def record_action(
        self,
        action: Action,
        resolution: ActionResolution,
        *,
        actor_hint: str = "",
        boss_scene: bool = False,
        is_turn_consuming: Callable[[Action], bool],
    ) -> dict[str, object]:
        """Commit one resolved action to the free-scene action round.

        This is the sole free-scene timing boundary.  A post-check choice,
        provisional check, out-of-turn message, or pure clarification cannot
        accidentally count as another passage of fictional time.
        """

        if resolution.payload.get("check_result_provisional") or resolution.payload.get("action_uncommitted"):
            return {}
        if self.conflicts.state.active:
            return {}
        if action.parameters.get("post_check_acceptance") or action.action_type in self._POST_CHECK_FOLLOWUP_ACTIONS:
            return {}
        if resolution.payload.get("out_of_turn") or action.action_type == ActionType.NEXT_TURN:
            return {}

        resume_deferred = bool(resolution.payload.get("resume_deferred_action"))
        if not resume_deferred and not self._is_time_advancing_action(
            action,
            is_turn_consuming=is_turn_consuming,
        ):
            return {}
        if not self.clocks.all():
            return {}

        changed_clock_names = self.changed_clock_names(resolution)
        skip_names = self.auto_advance_skip_names(resolution)
        if resolution.payload.get("clock_created"):
            skip_names.update(changed_clock_names)
            created_change = resolution.payload.get("clock_change")
            created_name = (
                str(created_change.get("clock_name") or "")
                if isinstance(created_change, dict)
                else str(getattr(created_change, "clock_name", "") or "")
            )
            if created_name:
                skip_names.add(created_name)

        return self.record(
            self.actor_for_action(action, actor_hint=actor_hint),
            changed_clock_names=changed_clock_names,
            auto_advance_skip_names=skip_names,
            boss_scene=boss_scene,
        )

    def record(
        self,
        actor: str,
        *,
        changed_clock_names: Iterable[str] = (),
        auto_advance_skip_names: Iterable[str] = (),
        boss_scene: bool = False,
    ) -> dict[str, object]:
        clean_actor = str(actor or "").strip()
        if self.conflicts.state.active or not self.clocks.all():
            return {}
        if not clean_actor or not self.characters.exists(clean_actor):
            return {}
        if "pc" not in self.characters.get(clean_actor).traits:
            return {}

        round_progress = self.scenes.record_action_round_action(
            clean_actor,
            self.active_pc_names(clean_actor),
            auto_advance_skip_names={
                str(name or "").strip()
                for name in auto_advance_skip_names
                if str(name or "").strip()
            },
        )
        payload: dict[str, object] = {
            "action_round_progress": {
                key: value
                for key, value in round_progress.items()
                if key != "auto_advance_skip_names"
            },
            "action_round_completed": bool(round_progress["completed"]),
            "action_round_waiting_for": [
                name
                for name in round_progress["waiting"]
                if not str(name).startswith("__")
            ],
            "free_scene_action_elapsed": True,
        }

        auto_changes: list[object] = []
        if round_progress["completed"]:
            auto_changes = list(
                self.pacing.auto_advance_after_turn(
                    skip_names=set(round_progress["auto_advance_skip_names"]),
                    boss_scene=boss_scene,
                    conflict_active=False,
                    event_timing="action_round_end",
                )
            )
            payload["turn_auto_advanced"] = True
            payload["completed_action_round"] = round_progress["round_number"]
        round_clock_names = [
            clock.name
            for clock in self.clocks.subscribed_auto_clocks(
                "action_round_end"
            )
        ]
        if round_clock_names or auto_changes:
            payload["timeline_phases"] = [
                {
                    "kind": "automatic_clock",
                    "timing": "action_round_end",
                    "round": round_progress["round_number"],
                    "status": (
                        "completed" if round_progress["completed"] else "pending"
                    ),
                    "clock_names": list(
                        dict.fromkeys(
                            [
                                *round_clock_names,
                                *[
                                    str(getattr(change, "clock_name", "") or "")
                                    for change in auto_changes
                                ],
                            ]
                        )
                    ),
                }
            ]
        if auto_changes:
            payload["auto_clock_changes"] = auto_changes
            if self.clock_lifecycle is not None:
                self.clock_lifecycle.settle_changes(
                    auto_changes,
                    payload=payload,
                )

        highlights = {
            str(name or "").strip()
            for name in changed_clock_names
            if str(name or "").strip()
        }
        highlights.update(
            str(getattr(change, "clock_name", "") or "").strip()
            for change in auto_changes
            if str(getattr(change, "clock_name", "") or "").strip()
        )
        payload["clock_progress"] = self.pacing.formatted_public_clocks(
            boss_scene=boss_scene,
            highlight_names=highlights,
            # Active foreground clocks remain visible after every committed
            # action; highlights only control whether an urgent hint is added.
            only_highlighted=False,
        )
        payload["clock_status_refresh"] = bool(
            payload["clock_progress"] or auto_changes
        )
        return payload

    def active_pc_names(self, acting_actor: str = "") -> list[str]:
        pcs = [
            character.name
            for character in self.characters.all()
            if "pc" in character.traits
        ]
        ledger = self.session_ledger
        participating = {
            str(name or "").strip()
            for name in set(getattr(ledger, "participating_pcs", set()) or set())
            if str(name or "").strip()
        }
        if bool(getattr(ledger, "active", False)) and participating:
            pcs = [name for name in pcs if name in participating]

        absent = set(self.world.absent_players)
        active: list[str] = []
        for pc_name in pcs:
            owner_names = {pc_name}
            for key, draft in self.world.world_profile.hero_drafts.items():
                hero_name = str(draft.hero_name or key).strip()
                if hero_name != pc_name:
                    continue
                owner_names.update(
                    name
                    for name in (str(key).strip(), str(draft.player_name or "").strip())
                    if name
                )
            if owner_names & absent:
                continue
            active.append(pc_name)

        actor = str(acting_actor or "").strip()
        if actor and self.characters.exists(actor):
            if "pc" in self.characters.get(actor).traits and actor not in active:
                active.append(actor)
        elif actor and not pcs:
            active.append(actor)
        return active

    @classmethod
    def actor_for_action(cls, action: Action, *, actor_hint: str = "") -> str:
        hinted = str(actor_hint or "").strip()
        if hinted:
            return hinted
        for key in cls._ACTOR_PARAMETER_KEYS:
            value = action.parameters.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def changed_clock_names(resolution: ActionResolution) -> set[str]:
        names: set[str] = set()
        candidates = [resolution.payload.get("clock_change")]
        candidates.extend(resolution.payload.get("threat_clock_changes") or [])
        candidates.extend(resolution.payload.get("clock_changes") or [])
        for change in candidates:
            if change is None:
                continue
            if isinstance(change, dict):
                name = change.get("clock_name", "")
                delta = change.get("delta")
                before = change.get("before")
                after = change.get("after")
            else:
                name = getattr(change, "clock_name", "")
                delta = getattr(change, "delta", None)
                before = getattr(change, "before", None)
                after = getattr(change, "after", None)
            if delta is not None:
                actually_changed = int(delta or 0) != 0
            elif before is not None and after is not None:
                actually_changed = int(before) != int(after)
            else:
                actually_changed = True
            if name and actually_changed:
                names.add(str(name))
        return names

    def auto_advance_skip_names(self, resolution: ActionResolution) -> set[str]:
        """Avoid double ticks while preserving time after counter-progress."""

        names = self.changed_clock_names(resolution)
        candidates = [resolution.payload.get("clock_change")]
        candidates.extend(resolution.payload.get("threat_clock_changes") or [])
        candidates.extend(resolution.payload.get("clock_changes") or [])
        for change in candidates:
            if change is None:
                continue
            if isinstance(change, dict):
                name = str(change.get("clock_name") or "")
                delta = int(change.get("delta") or 0)
            else:
                name = str(getattr(change, "clock_name", "") or "")
                delta = int(getattr(change, "delta", 0) or 0)
            if not name or delta >= 0 or not self.clocks.exists(name):
                continue
            clock = self.clocks.get(name)
            if clock.auto_advance and clock.clock_type in {"threat", "villain", "dungeon", "boss"}:
                names.discard(name)
        return names

    @staticmethod
    def _is_time_advancing_action(
        action: Action,
        *,
        is_turn_consuming: Callable[[Action], bool],
    ) -> bool:
        if action.action_type == ActionType.NARRATE:
            if any(
                action.parameters.get(flag)
                for flag in (
                    "scene_clarification",
                    "scene_open_request",
                    "gm_beat_request",
                    "out_of_turn_comment",
                    "rest_proposal",
                )
            ):
                return False
            return any(
                action.parameters.get(flag)
                for flag in (
                    "consume_turn",
                    "npc_answer_generated",
                    "scene_object_response",
                    "care_action_response",
                )
            )
        return is_turn_consuming(action)
