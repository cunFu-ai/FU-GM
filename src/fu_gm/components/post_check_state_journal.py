from __future__ import annotations

from copy import deepcopy
from typing import Callable

from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import Action, Character, ClockChange


class PostCheckStateJournal:
    """Own the volatile state associated with one unresolved check.

    ``DecisionWindow`` remains the persisted source of truth for player
    choices.  This journal only keeps replay data that is meaningful inside a
    live rules transaction: the latest roll, clock baselines, and a one-shot
    advantage.  Keeping those values together prevents the interceptor from
    growing another parallel decision system.
    """

    def __init__(
        self,
        *,
        rules_engine: RulesEngine,
        clock_manager: ClockManager,
        ensure_clock_exists: Callable[..., bool],
    ) -> None:
        self.rules_engine = rules_engine
        self.clock_manager = clock_manager
        self.ensure_clock_exists = ensure_clock_exists
        self.rolls: dict[str, object] = {}
        self.clock_checks: dict[str, dict[str, object]] = {}
        self.advantages: dict[str, int] = {}

    def clear_roll_context(self) -> None:
        self.rolls.clear()
        self.clock_checks.clear()

    def remember_roll(self, outcome: object) -> None:
        actor = str(getattr(outcome, "actor", "") or "").strip()
        if actor:
            self.rolls[actor] = outcome

    def roll_for(self, actor: str) -> object | None:
        return self.rolls.get(str(actor or "").strip())

    def replace_roll(self, actor: str, outcome: object) -> None:
        actor_name = str(actor or "").strip()
        if actor_name:
            self.rolls[actor_name] = outcome

    def discard_actor(self, actor: str) -> None:
        actor_name = str(actor or "").strip()
        self.rolls.pop(actor_name, None)
        self.clock_checks.pop(actor_name, None)

    def grant_advantage(self, actor: str, bonus: int = 4) -> int:
        actor_name = str(actor or "").strip()
        self.advantages[actor_name] = self.advantages.get(actor_name, 0) + int(bonus)
        return self.advantages[actor_name]

    def consume_advantage(self, actor: str) -> int:
        return self.advantages.pop(str(actor or "").strip(), 0)

    def snapshot_advantages(self) -> dict[str, int]:
        return deepcopy(self.advantages)

    def restore_advantages(self, snapshot: dict[str, int]) -> None:
        self.advantages.clear()
        self.advantages.update(deepcopy(snapshot))

    def remember_clock_check(
        self,
        action: Action,
        outcome: object,
        payload: dict[str, object],
    ) -> None:
        if payload.get("clock_skill_trigger_effects"):
            return
        clock_names = [
            str(action.parameters.get("clock_name") or "").strip(),
            str(action.parameters.get("threat_clock_name") or "").strip(),
        ]
        clock_names = [name for name in clock_names if name]
        if not clock_names:
            return
        change = payload.get("clock_change")
        baselines: dict[str, int] = {}
        for name in clock_names:
            if change is not None and getattr(change, "clock_name", "") == name:
                baselines[name] = int(getattr(change, "before", 0))
            elif self.clock_manager.exists(name):
                baselines[name] = self.clock_manager.get(name).current
            else:
                baselines[name] = 0
        actor = str(getattr(outcome, "actor", "") or "").strip()
        if actor:
            self.clock_checks[actor] = {
                "action": Action(action.action_type, dict(action.parameters)),
                "baselines": baselines,
            }

    def reconcile_clock_check(
        self,
        actor: Character,
        outcome: object,
    ) -> dict[str, object]:
        transaction = self.clock_checks.get(actor.name)
        if not transaction:
            return {}
        action = transaction["action"]
        baselines = dict(transaction.get("baselines") or {})
        for name, baseline in baselines.items():
            if self.clock_manager.exists(name):
                current = self.clock_manager.get(name).current
                self.clock_manager.advance(name, int(baseline) - current)

        parameters = action.parameters
        clock_name = str(parameters.get("clock_name") or "").strip()
        threat_clock_name = str(parameters.get("threat_clock_name") or "").strip()
        selected_name = ""
        delta = 0
        reason = "援用后重新结算检定，命刻进度未变化。"
        corrected_threat_direction = False

        if bool(getattr(outcome, "success", False)) and clock_name:
            self.ensure_clock_exists(action, clock_name, default_clock_type="objective")
            clock = self.clock_manager.get(clock_name)
            delta = self.rules_engine.clock_segments_from_roll(
                outcome,
                spend_critical_opportunity=bool(
                    parameters.get("spend_critical_opportunity_on_clock", False)
                ),
            ) * int(parameters.get("clock_direction", 1))
            if (
                clock.clock_type == "threat"
                and delta > 0
                and "pc" in actor.traits
                and not parameters.get("allow_advance_threat_on_success", False)
            ):
                delta = -delta
                corrected_threat_direction = True
            selected_name = clock_name
            reason = (
                "援用后按新结果压制威胁命刻。"
                if corrected_threat_direction
                else "援用后按新结果改变命刻。"
            )
        elif not bool(getattr(outcome, "success", False)) and threat_clock_name:
            self.ensure_clock_exists(
                action,
                threat_clock_name,
                default_clock_type="threat",
                prefix="threat_clock_",
            )
            if "threat_clock_delta" in parameters:
                delta = self._int_parameter(parameters, "threat_clock_delta", 1, minimum=0)
            else:
                delta = self.rules_engine.threat_clock_segments_from_roll(
                    outcome,
                    spend_fumble_opportunity=bool(
                        parameters.get("spend_fumble_opportunity_on_threat_clock", False)
                    ),
                )
            selected_name = threat_clock_name
            reason = "援用后检定仍失败，按新结果推进威胁命刻。"

        if not selected_name:
            return {"clock_reconciled": True}
        clock = self.clock_manager.get(selected_name)
        before, after = self.clock_manager.advance(selected_name, delta)
        change = ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=after - before,
            max_segments=clock.max_segments,
            reason=reason,
            clock_type=clock.clock_type,
            stakes=clock.stakes,
            completion_consequence=clock.completion_consequence,
        )
        payload: dict[str, object] = {
            "clock_reconciled": True,
            "clock_change": change,
        }
        if corrected_threat_direction:
            payload["clock_direction_corrected"] = True
        return payload

    @staticmethod
    def _int_parameter(
        parameters: dict[str, object],
        key: str,
        default: int,
        *,
        minimum: int | None = None,
    ) -> int:
        try:
            value = int(parameters.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value
