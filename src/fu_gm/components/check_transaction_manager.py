from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Callable, Iterable

from fu_gm.components.post_check_state_journal import PostCheckStateJournal
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Affinity,
    DecisionWindow,
    RollOutcome,
)


class CheckTransactionManager:
    """Own provisional player-check snapshots and rollback state.

    Rules are still resolved by ``ActionInterceptor``. This component only
    controls when a check becomes public and restores the exact pre-check
    state before a reroll or explicit acceptance is replayed.
    """

    def __init__(
        self,
        *,
        character_manager,
        clock_manager,
        conflict_manager,
        world_state,
        post_check_state: PostCheckStateJournal,
        ritual_manager: Callable[[], object | None],
        project_manager: Callable[[], object | None],
        dungeon_manager: Callable[[], object | None],
        transactional_actions: Iterable[ActionType],
    ) -> None:
        self.character_manager = character_manager
        self.clock_manager = clock_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state
        self.post_check_state = post_check_state
        # Compatibility aliases for transaction replay code. Ownership remains
        # with PostCheckStateJournal rather than ActionInterceptor.
        self.pending_rolls = post_check_state.rolls
        self.pending_clock_checks = post_check_state.clock_checks
        self.pending_advantages = post_check_state.advantages
        self._ritual_manager = ritual_manager
        self._project_manager = project_manager
        self._dungeon_manager = dungeon_manager
        self.transactional_actions = set(transactional_actions)
        self.pending: dict[str, dict[str, object]] = {}
        self.candidate: dict[str, object] | None = None
        self.replaying = False
        self.allow_restage = False

    def clear(self) -> None:
        self.pending.clear()
        self.candidate = None
        self.allow_restage = False

    @staticmethod
    def _is_pre_final_window(window: dict[str, object]) -> bool:
        kind = str(window.get("kind") or "")
        if kind in {"trait_invocation", "bond_invocation"}:
            return True
        label = str(window.get("label") or window.get("skill") or "")
        if kind == "skill_judgement" and label == "幸运七":
            return True
        return kind == "skill_parameter" and label == "予以信任"

    @staticmethod
    def _is_pre_final_decision(window: DecisionWindow) -> bool:
        if window.kind in {"trait_invocation", "bond_invocation"}:
            return True
        label = str(window.payload.get("label") or window.payload.get("skill") or "")
        if window.kind == "skill_judgement" and label == "幸运七":
            return True
        return window.kind == "skill_parameter" and label == "予以信任"

    def build_candidate(self, action: Action) -> dict[str, object] | None:
        if action.action_type not in self.transactional_actions:
            return None
        actor_name = self.actor_name_for_action(action)
        if not actor_name or not self.character_manager.exists(actor_name):
            return None
        actor = self.character_manager.get(actor_name)
        if "pc" not in actor.traits:
            return None
        if any(
            action.parameters.get(key)
            for key in ("invoke_trait", "trait_name", "invoke_bond_target", "bond_target")
        ):
            return None
        return {
            "action": deepcopy(action),
            "snapshot": self.snapshot(),
            "invocation_history": [],
            "bond_invoked": False,
        }

    def actor_name_for_action(self, action: Action) -> str:
        """Resolve the PC whose check will be produced by a top-level action."""

        direct = str(
            action.parameters.get("actor")
            or action.parameters.get("caster")
            or ""
        ).strip()
        if direct:
            return direct
        if action.action_type != ActionType.CAST_RITUAL:
            return ""
        manager = self._ritual_manager()
        if manager is None:
            return ""
        raw_name = str(
            action.parameters.get("clock_name")
            or action.parameters.get("name")
            or ""
        ).strip()
        if not raw_name:
            return ""
        candidates = [raw_name]
        if not raw_name.startswith("仪式："):
            candidates.append(f"仪式：{raw_name}")
        for clock_name in candidates:
            plan = manager.active_rituals.get(clock_name)
            if plan is not None:
                return str(plan.caster or "").strip()
        return ""

    def snapshot(self) -> dict[str, object]:
        state: dict[str, object] = {
            "characters": deepcopy(self.character_manager._characters),
            "clocks": deepcopy(self.clock_manager._clocks),
            "archived_clocks": deepcopy(self.clock_manager._archived_clocks),
            "current_scene_id": self.clock_manager._current_scene_id,
            "conflict_state": deepcopy(self.conflict_manager.state),
            "world_state": deepcopy(self.world_state.__dict__),
            "pending_advantages": self.post_check_state.snapshot_advantages(),
        }
        ritual_manager = self._ritual_manager()
        if ritual_manager is not None:
            state["active_rituals"] = deepcopy(ritual_manager.active_rituals)
        project_manager = self._project_manager()
        if project_manager is not None:
            state["projects"] = deepcopy(project_manager.projects)
        dungeon_manager = self._dungeon_manager()
        if dungeon_manager is not None:
            state["dungeon_state"] = deepcopy(dungeon_manager.state)
            state["dungeon_maps"] = deepcopy(dungeon_manager.maps)
            state["dungeon_history"] = deepcopy(dungeon_manager.history)
            state["dungeon_design_history"] = deepcopy(dungeon_manager.design_history)
        return state

    def restore(self, snapshot: dict[str, object]) -> None:
        # Decision windows are created after a check is rolled.  They are the
        # control plane for deciding whether that provisional roll is accepted
        # or replayed, so restoring the pre-check rules snapshot must never
        # erase them.  Doing so leaves a player-facing prompt whose matching
        # window no longer exists and makes a legal invocation fail later.
        live_decision_windows = deepcopy(self.world_state.decision_windows)
        live_check_batches = deepcopy(
            getattr(self.world_state, "pending_check_batches", {})
        )
        live_check_batch_history = deepcopy(
            getattr(self.world_state, "check_batch_history", [])
        )
        self.character_manager._characters = deepcopy(snapshot["characters"])
        self.clock_manager._clocks = deepcopy(snapshot["clocks"])
        self.clock_manager._archived_clocks = deepcopy(snapshot["archived_clocks"])
        self.clock_manager._current_scene_id = str(snapshot["current_scene_id"])
        self.conflict_manager.state = deepcopy(snapshot["conflict_state"])
        self.world_state.__dict__.clear()
        self.world_state.__dict__.update(deepcopy(snapshot["world_state"]))
        self.world_state.decision_windows = live_decision_windows
        self.world_state.pending_check_batches = live_check_batches
        self.world_state.check_batch_history = live_check_batch_history
        self.post_check_state.restore_advantages(snapshot["pending_advantages"])
        ritual_manager = self._ritual_manager()
        if ritual_manager is not None and "active_rituals" in snapshot:
            ritual_manager.active_rituals = deepcopy(snapshot["active_rituals"])
        project_manager = self._project_manager()
        if project_manager is not None and "projects" in snapshot:
            project_manager.projects = deepcopy(snapshot["projects"])
        dungeon_manager = self._dungeon_manager()
        if dungeon_manager is not None and "dungeon_state" in snapshot:
            dungeon_manager.state = deepcopy(snapshot["dungeon_state"])
            dungeon_manager.maps = deepcopy(snapshot["dungeon_maps"])
            dungeon_manager.history = deepcopy(snapshot["dungeon_history"])
            dungeon_manager.design_history = deepcopy(snapshot["dungeon_design_history"])
        self.post_check_state.clear_roll_context()

    def stage(self, resolution: ActionResolution) -> None:
        candidate = self.candidate
        self.candidate = None
        if not candidate or (self.replaying and not self.allow_restage):
            return
        outcome = resolution.payload.get("roll")
        if outcome is None or not hasattr(outcome, "actor"):
            return
        actor_name = str(getattr(outcome, "actor", "") or "")
        windows = [
            *(resolution.payload.get("post_check_windows") or []),
            *(resolution.payload.get("skill_decision_windows") or []),
        ]
        supported_window = any(
            isinstance(window, dict) and self._is_pre_final_window(window)
            for window in windows
        )
        if not supported_window:
            return
        transaction = {
            **candidate,
            "roll": deepcopy(outcome),
            "roll_sequence": self._roll_sequence(resolution, outcome),
            "roll_index": self._roll_index(resolution),
            "invocation_history": list(candidate.get("invocation_history") or []),
            "bond_invoked": bool(candidate.get("bond_invoked")),
        }
        self.pending[actor_name] = transaction

        portable_resume = self.portable_resume_payload(
            action=resolution.action,
            outcome=outcome,
            roll_sequence=transaction["roll_sequence"],
            roll_index=transaction["roll_index"],
        )
        for window in self.world_state.decision_windows.values():
            if (
                window.status.value == "pending"
                and self._is_pre_final_decision(window)
                and str(window.payload.get("source_actor") or window.owner) == actor_name
            ):
                window.payload.update(portable_resume)
                window.payload["source_actor"] = actor_name

        blocking_invocation = any(
            isinstance(window, dict)
            and bool(window.get("blocking"))
            and self._is_pre_final_window(window)
            for window in windows
        )
        if not blocking_invocation:
            return
        self.restore(candidate["snapshot"])
        self.post_check_state.replace_roll(actor_name, deepcopy(outcome))
        self.pending[actor_name] = transaction
        resolution.payload["check_result_provisional"] = True
        resolution.payload["provisional_actor"] = actor_name

    def portable_resume_payload(
        self,
        *,
        action: Action,
        outcome: RollOutcome,
        roll_sequence: list[RollOutcome] | None = None,
        roll_index: int = 0,
    ) -> dict[str, object]:
        """Serialize the minimum journal needed to replay an unsettled check.

        The full rollback snapshot deliberately stays out of ``WorldState``:
        it contains the decision windows themselves and would recursively
        embed the campaign.  A blocking check has already restored the live
        campaign to its pre-check state, so after loading we only need the
        source action and rolled result to rebuild a fresh local snapshot.
        """

        candidate = self.candidate or {}
        source_action = candidate.get("action")
        if not isinstance(source_action, Action):
            source_action = action
        return {
            "portable_check_resume": True,
            "portable_check_resume_version": 1,
            "source_action": self._encode_action(source_action),
            "source_roll": self._encode_roll(outcome),
            "source_roll_sequence": [
                self._encode_roll(item)
                for item in (roll_sequence or [outcome])
                if isinstance(item, RollOutcome)
            ],
            "source_roll_index": max(0, int(roll_index or 0)),
            "invocation_history": self._portable_value(
                candidate.get("invocation_history") or []
            ),
            "bond_invoked": bool(candidate.get("bond_invoked")),
        }

    def hydrate_from_window(self, window: DecisionWindow) -> bool:
        """Rebuild an in-memory failed-check transaction after save/load."""

        payload = dict(window.payload or {})
        actor_name = str(payload.get("source_actor") or window.owner or "").strip()
        if actor_name in self.pending and actor_name in self.pending_rolls:
            return True
        if (
            not window.blocking
            or not self._is_pre_final_decision(window)
            or not bool(payload.get("portable_check_resume"))
        ):
            return False
        try:
            action = self._decode_action(payload.get("source_action"))
            outcome = self._decode_roll(payload.get("source_roll"))
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not actor_name
            or outcome.actor != actor_name
            or action.action_type not in self.transactional_actions
        ):
            return False
        transaction = {
            "action": action,
            "roll": outcome,
            "roll_sequence": [
                self._decode_roll(item)
                for item in payload.get("source_roll_sequence", [])
                if isinstance(item, dict)
            ]
            or [deepcopy(outcome)],
            "roll_index": int(payload.get("source_roll_index", 0) or 0),
            "snapshot": self.snapshot(),
            "hydrated_from_window": window.window_id,
            "invocation_history": [
                dict(item)
                for item in payload.get("invocation_history", [])
                if isinstance(item, dict)
            ],
            "bond_invoked": bool(payload.get("bond_invoked")),
        }
        self.pending[actor_name] = transaction
        self.post_check_state.replace_roll(actor_name, deepcopy(outcome))
        self.candidate = None
        return True

    @staticmethod
    def _roll_sequence(
        resolution: ActionResolution,
        outcome: RollOutcome,
    ) -> list[RollOutcome]:
        raw = resolution.payload.get("check_roll_sequence")
        if not isinstance(raw, list):
            return [deepcopy(outcome)]
        sequence = [
            deepcopy(item)
            for item in raw
            if isinstance(item, RollOutcome)
        ]
        return sequence or [deepcopy(outcome)]

    @staticmethod
    def _roll_index(resolution: ActionResolution) -> int:
        try:
            return max(0, int(resolution.payload.get("check_roll_index", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _encode_action(cls, action: Action) -> dict[str, object]:
        return {
            "action_type": action.action_type.value,
            "parameters": cls._portable_value(action.parameters),
        }

    @classmethod
    def _encode_roll(cls, outcome: RollOutcome) -> dict[str, object]:
        return cls._portable_value(asdict(outcome))

    @classmethod
    def _portable_value(cls, value):
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return cls._portable_value(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._portable_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._portable_value(item) for item in value]
        if isinstance(value, set):
            return [cls._portable_value(item) for item in sorted(value, key=str)]
        return value

    @staticmethod
    def _decode_action(raw: object) -> Action:
        if not isinstance(raw, dict):
            raise TypeError("source_action must be a mapping")
        action_type = ActionType(str(raw["action_type"]))
        parameters = raw.get("parameters", {})
        if not isinstance(parameters, dict):
            raise TypeError("source action parameters must be a mapping")
        return Action(action_type, deepcopy(parameters))

    @staticmethod
    def _decode_roll(raw: object) -> RollOutcome:
        if not isinstance(raw, dict):
            raise TypeError("source_roll must be a mapping")
        affinity_value = str(raw.get("applied_affinity") or Affinity.NORMAL.value)
        try:
            affinity = Affinity(affinity_value)
        except ValueError:
            affinity = Affinity.NORMAL
        dice = [
            (int(item[0]), int(item[1]))
            for item in raw.get("dice", [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
        return RollOutcome(
            actor=str(raw["actor"]),
            attributes=[str(item) for item in raw.get("attributes", [])],
            dice=dice,
            total=int(raw.get("total", 0)),
            modifier=int(raw.get("modifier", 0)),
            high_roll=int(raw.get("high_roll", 0)),
            target_number=int(raw.get("target_number", 0)),
            success=bool(raw.get("success", False)),
            critical_success=bool(raw.get("critical_success", False)),
            fumble=bool(raw.get("fumble", False)),
            opportunity_count=int(raw.get("opportunity_count", 0)),
            margin=int(raw.get("margin", 0)),
            target=str(raw["target"]) if raw.get("target") is not None else None,
            reason=str(raw.get("reason", "")),
            damage=int(raw.get("damage", 0)),
            damage_type=str(raw.get("damage_type", "physical")),
            applied_affinity=affinity,
            hp_after=int(raw["hp_after"]) if raw.get("hp_after") is not None else None,
        )
