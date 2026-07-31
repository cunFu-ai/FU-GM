from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.skill_trigger_manager import SkillEventResult, SkillTriggerManager
from fu_gm.models import Affinity, Character, EffectTiming, StatusEffect, TimedEffect


_DAMAGE_TYPES = ("physical", "wind", "lightning", "dark", "earth", "fire", "ice", "light", "poison")


@dataclass
class SkillLifecycleOutcome:
    event: str
    result: SkillEventResult
    records: list[dict[str, object]] = field(default_factory=list)
    windows: list[dict[str, object]] = field(default_factory=list)


class SkillLifecycleCoordinator:
    """Commit skill events without teaching rule components about UI state.

    ``SkillTriggerManager`` decides what a trigger means.  This coordinator is
    the transaction boundary that applies automatic resource/effect changes and
    persists every optional choice as a ``DecisionWindow``.  Callers therefore
    emit semantic rule events rather than maintaining one-off pending flags.
    """

    def __init__(
        self,
        triggers: SkillTriggerManager,
        decisions: DecisionWindowManager,
        characters: CharacterManager,
        conflict: ConflictManager,
    ) -> None:
        self.triggers = triggers
        self.decisions = decisions
        self.characters = characters
        self.conflict = conflict

    def trigger(
        self,
        event_name: str,
        actor: Character,
        *,
        target: Character | None = None,
        effect_targets: list[str] | None = None,
        apply_resources: bool = True,
        **context: object,
    ) -> SkillLifecycleOutcome:
        result = self.triggers.emit(event_name, actor, target=target, **context)
        outcome = SkillLifecycleOutcome(event=event_name, result=result)
        if apply_resources:
            self._apply_resource_effects(
                actor,
                result,
                outcome,
                effect_targets=list(effect_targets or []),
            )
        self._apply_facts(actor, result, outcome)
        self._persist_windows(event_name, actor, result, outcome, target=target, context=context)
        return outcome

    def _apply_resource_effects(
        self,
        actor: Character,
        result: SkillEventResult,
        outcome: SkillLifecycleOutcome,
        *,
        effect_targets: list[str],
    ) -> None:
        for effect in result.effects:
            if effect.resource not in {"hp", "mp", "inventory_points"} or effect.amount <= 0:
                continue
            targets = effect_targets if effect.source == "治愈之力" and effect_targets else [actor.name]
            for target_name in targets:
                if not self.characters.exists(target_name):
                    continue
                before, after = self.characters.modify_resource(target_name, effect.resource, effect.amount)
                gained = after - before
                if gained <= 0:
                    continue
                outcome.records.append(
                    {
                        "event": outcome.event,
                        "source": effect.source,
                        "target": target_name,
                        "resource": effect.resource,
                        "amount": gained,
                        "note": effect.note,
                    }
                )

    def _apply_facts(
        self,
        actor: Character,
        result: SkillEventResult,
        outcome: SkillLifecycleOutcome,
    ) -> None:
        for fact in result.facts:
            source = str(fact.get("source") or "")
            if fact.get("effect") == "apply_status":
                target_name = str(fact.get("target") or "")
                try:
                    status = StatusEffect(str(fact.get("status") or ""))
                except ValueError:
                    status = None
                if target_name and status is not None and self.characters.exists(target_name):
                    applied = self.conflict.apply_status(target_name, status)
                    outcome.records.append(
                        {
                            "event": outcome.event,
                            "source": source,
                            "target": target_name,
                            "effect": "apply_status",
                            "status": status.value,
                            "applied": applied,
                        }
                    )
                    continue
            if fact.get("effect") == "all_damage_resistance":
                target_name = str(fact.get("target") or "")
                if target_name and self.characters.exists(target_name):
                    effect = TimedEffect(
                        owner=actor.name,
                        effect_type="affinity_buff",
                        expires_on=EffectTiming.OWNER_TURN_START,
                        target=target_name,
                        source=source or "保镖",
                        effect_key=f"skill:{source or '保镖'}:{target_name}",
                        data={"affinity_changes": {damage_type: Affinity.RESIST for damage_type in _DAMAGE_TYPES}},
                        note="被掩护者对所有伤害类型获得抵抗，直到守护者的下个回合开始。",
                    )
                    self.conflict.register_effect(effect)
                    outcome.records.append(
                        {
                            "event": outcome.event,
                            "source": source,
                            "target": target_name,
                            "effect": "all_damage_resistance",
                        }
                    )
                    continue
            outcome.records.append(
                {
                    "event": outcome.event,
                    "source": source,
                    "target": actor.name,
                    "fact": dict(fact),
                }
            )

    def _persist_windows(
        self,
        event_name: str,
        actor: Character,
        result: SkillEventResult,
        outcome: SkillLifecycleOutcome,
        *,
        target: Character | None,
        context: dict[str, object],
    ) -> None:
        scope_kind = "conflict" if self.conflict.state.active else "scene"
        scope_id = self.conflict.state.scene_name if self.conflict.state.active else str(context.get("scene_id") or "current")
        turn_serial = int(getattr(self.conflict.state, "turn_serial", 0) or 0)
        for raw in result.windows:
            skill = str(raw.get("label") or raw.get("skill") or "技能触发")
            owner = str(raw.get("actor") or actor.name)
            target_key = target.name if target is not None else str(context.get("source_name") or "")
            dedupe_key = str(raw.get("dedupe_key") or "").strip()
            if not dedupe_key:
                dedupe_key = ":".join(
                    part
                    for part in (
                        "skill",
                        scope_kind,
                        scope_id,
                        str(turn_serial),
                        event_name,
                        owner,
                        skill,
                        target_key,
                    )
                    if part
                )
            payload = {
                "skill": skill,
                "label": skill,
                "event": event_name,
                "required_parameter": str(raw.get("required_parameter") or "choice"),
                "trigger_context": dict(context),
            }
            if target is not None:
                payload["target"] = target.name
            source_actor = str(context.get("source_actor") or "").strip()
            if source_actor:
                payload["source_actor"] = source_actor
            source_action_type = str(context.get("source_action_type") or "").strip()
            if source_action_type:
                payload["source_action_type"] = source_action_type
            decision = self.decisions.create(
                kind=str(raw.get("kind") or "skill_judgement"),
                owner=owner,
                prompt=str(raw.get("guidance") or raw.get("timing") or ""),
                options=[dict(option) for option in raw.get("options", []) if isinstance(option, dict)],
                scope_kind=scope_kind,
                scope_id=scope_id,
                blocking=bool(raw.get("blocking", False)),
                action_type=str(raw.get("action_type") or "Skill"),
                transaction_id=str(context.get("transaction_id") or ""),
                payload=payload,
                dedupe_key=dedupe_key,
            )
            outcome.windows.append(
                {
                    "window_id": decision.window_id,
                    "kind": decision.kind,
                    "owner": decision.owner,
                    "skill": skill,
                    "prompt": decision.prompt,
                    "options": list(decision.options),
                    "blocking": decision.blocking,
                }
            )
