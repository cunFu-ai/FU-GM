from __future__ import annotations

import random

from fu_gm.models import (
    Affinity,
    Character,
    OpposedCheckOutcome,
    RollOutcome,
    StatusEffect,
    SupportOutcome,
    TeamCheckOutcome,
)
from fu_gm.skill_library import skill_rank


_DIE_STEPS = [6, 8, 10, 12]
_ATTRIBUTE_ALIASES = {
    "DEX": "DEX",
    "dex": "DEX",
    "敏捷": "DEX",
    "AGI": "DEX",
    "agi": "DEX",
    "INS": "INS",
    "ins": "INS",
    "洞察": "INS",
    "MIG": "MIG",
    "mig": "MIG",
    "力量": "MIG",
    "STR": "MIG",
    "str": "MIG",
    "WLP": "WLP",
    "wlp": "WLP",
    "意志": "WLP",
    "WILL": "WLP",
    "will": "WLP",
}
_STATUS_ATTRIBUTE_PENALTIES = {
    StatusEffect.SLOW: {"DEX"},
    StatusEffect.DAZED: {"INS"},
    StatusEffect.WEAKENED: {"MIG"},
    StatusEffect.SHAKEN: {"WLP"},
    StatusEffect.ENRAGED: {"DEX", "INS"},
    StatusEffect.POISONED: {"MIG", "WLP"},
}


def resolve_affinity(
    base_affinity: Affinity | str | None = Affinity.NORMAL,
    equipment_affinity: Affinity | str | None = None,
    temporary_affinity: Affinity | str | None = None,
    *,
    ignore_resist: bool = False,
    ignore_immune: bool = False,
    ignore_all_affinities: bool = False,
) -> Affinity:
    if ignore_all_affinities:
        return Affinity.NORMAL

    affinities: list[Affinity] = []
    for raw_affinity in (base_affinity, equipment_affinity, temporary_affinity):
        if raw_affinity is None:
            continue
        affinity = raw_affinity if isinstance(raw_affinity, Affinity) else Affinity(raw_affinity)
        if affinity == Affinity.NORMAL:
            continue
        if ignore_resist and affinity == Affinity.RESIST:
            continue
        if ignore_immune and affinity == Affinity.IMMUNE:
            continue
        affinities.append(affinity)

    if Affinity.ABSORB in affinities:
        return Affinity.ABSORB
    if Affinity.IMMUNE in affinities:
        return Affinity.IMMUNE
    if Affinity.WEAK in affinities and Affinity.RESIST in affinities:
        return Affinity.NORMAL
    if Affinity.WEAK in affinities:
        return Affinity.WEAK
    if Affinity.RESIST in affinities:
        return Affinity.RESIST
    return Affinity.NORMAL


class RulesEngine:
    """算数与随机结算的唯一真实来源。"""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def roll_die(self, die_size: int) -> int:
        return self._rng.randint(1, die_size)

    def roll_check(
        self,
        actor: Character,
        attributes: list[str],
        target_number: int,
        modifier: int = 0,
        target: str | None = None,
        reason: str = "",
    ) -> RollOutcome:
        attributes = self.normalize_check_attributes(actor, attributes)
        dice = []
        values = []
        for attribute in attributes:
            die_size = self._effective_die_size(actor, attribute)
            rolled = self._rng.randint(1, die_size)
            dice.append((die_size, rolled))
            values.append(rolled)

        total = sum(values) + modifier
        high_roll = max(values)
        critical_success = len(values) == 2 and values[0] == values[1] and values[0] >= 6
        fumble = len(values) == 2 and values[0] == values[1] == 1
        success = critical_success or (total >= target_number and not fumble)
        margin = total - target_number
        opportunity_count = 1 if critical_success or fumble else 0

        return RollOutcome(
            actor=actor.name,
            attributes=attributes,
            dice=dice,
            total=total,
            modifier=modifier,
            high_roll=high_roll,
            target_number=target_number,
            success=success,
            critical_success=critical_success,
            fumble=fumble,
            opportunity_count=opportunity_count,
            margin=margin,
            target=target,
            reason=reason,
        )

    def reroll_outcome(
        self,
        outcome: RollOutcome,
        reroll_indices: list[int] | tuple[int, ...] | None = None,
    ) -> RollOutcome:
        """Reroll one or both dice from an already rolled check.

        Trait invocation happens after the dice are seen but before the result is
        settled. The returned RollOutcome keeps the same target number,
        attributes, modifier, target and reason, but replaces the selected dice.
        Indices may be 0/1 or 1/2; invalid values are ignored.
        """

        dice = list(outcome.dice)
        if not dice:
            return outcome
        selected = self._normalize_reroll_indices(reroll_indices, len(dice))
        if not selected:
            selected = tuple(range(len(dice)))
        for index in selected:
            die_size, _ = dice[index]
            dice[index] = (die_size, self._rng.randint(1, die_size))
        return self.recompute_outcome(outcome, dice=dice)

    def apply_bond_bonus(self, outcome: RollOutcome, bonus: int) -> RollOutcome:
        """Apply a bond bonus without changing the original dice."""

        if bonus <= 0:
            return outcome
        return self.recompute_outcome(outcome, modifier=outcome.modifier + bonus)

    def recompute_outcome(
        self,
        outcome: RollOutcome,
        *,
        dice: list[tuple[int, int]] | None = None,
        modifier: int | None = None,
    ) -> RollOutcome:
        final_dice = list(dice if dice is not None else outcome.dice)
        final_modifier = outcome.modifier if modifier is None else modifier
        values = [rolled for _, rolled in final_dice]
        total = sum(values) + final_modifier
        high_roll = max(values) if values else 0
        critical_success = len(values) == 2 and values[0] == values[1] and values[0] >= 6
        fumble = len(values) == 2 and values[0] == values[1] == 1
        success = critical_success or (total >= outcome.target_number and not fumble)
        margin = total - outcome.target_number
        opportunity_count = 1 if critical_success or fumble else 0
        return RollOutcome(
            actor=outcome.actor,
            attributes=list(outcome.attributes),
            dice=final_dice,
            total=total,
            modifier=final_modifier,
            high_roll=high_roll,
            target_number=outcome.target_number,
            success=success,
            critical_success=critical_success,
            fumble=fumble,
            opportunity_count=opportunity_count,
            margin=margin,
            target=outcome.target,
            reason=outcome.reason,
            damage=outcome.damage,
            damage_type=outcome.damage_type,
            applied_affinity=outcome.applied_affinity,
            hp_after=outcome.hp_after,
        )

    def _normalize_reroll_indices(self, indices, dice_count: int) -> tuple[int, ...]:
        if indices is None:
            return tuple(range(dice_count))
        if isinstance(indices, int):
            raw_values = [indices]
        else:
            raw_values = list(indices)
        normalized: list[int] = []
        for raw_value in raw_values:
            try:
                index = int(raw_value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= dice_count:
                index -= 1
            if 0 <= index < dice_count and index not in normalized:
                normalized.append(index)
        return tuple(normalized)

    def normalize_check_attributes(self, actor: Character, attributes: list[str] | tuple[str, ...] | None) -> list[str]:
        """Final Fantasy-style checks always roll exactly two attribute dice.

        LLM adapters may accidentally send both English abbreviations and Chinese labels
        for the same declared check. The rules engine is the last authority, so it
        canonicalizes labels and truncates/fills the list before any dice are rolled.
        """

        raw_items: list[str]
        if attributes is None:
            raw_items = []
        elif isinstance(attributes, str):
            raw_items = [piece for piece in attributes.replace("＋", "+").replace("/", "+").split("+")]
        else:
            raw_items = [str(item) for item in attributes]

        canonical: list[str] = []
        for raw_item in raw_items:
            clean = str(raw_item or "").strip()
            if not clean:
                continue
            code = _ATTRIBUTE_ALIASES.get(clean, _ATTRIBUTE_ALIASES.get(clean.upper(), clean.upper()))
            if code in actor.attributes:
                canonical.append(code)

        if not canonical:
            canonical = [code for code in ("INS", "WLP") if code in actor.attributes]
        if len(canonical) == 1:
            canonical.append(canonical[0])

        if len(canonical) > 2:
            canonical = canonical[:2]

        while len(canonical) < 2:
            fallback = next(iter(actor.attributes.keys()), "INS")
            canonical.append(fallback)
        return canonical[:2]

    def compute_damage(
        self,
        high_roll: int,
        weapon_damage: int,
        damage_type: str,
        target: Character,
        ignore_resist: bool = False,
        ignore_all_affinities: bool = False,
    ) -> tuple[int, Affinity]:
        raw_damage = max(0, high_roll + weapon_damage)
        defensive_mastery = skill_rank(target.skills, "防御精通")
        if raw_damage > 0 and defensive_mastery > 0:
            raw_damage = max(0, raw_damage - defensive_mastery)
        affinity = resolve_affinity(
            target.affinities.get(damage_type, Affinity.NORMAL),
            target.equipment_affinities.get(damage_type),
            target.temporary_affinities.get(damage_type),
            ignore_resist=ignore_resist,
            ignore_all_affinities=ignore_all_affinities,
        )

        if affinity == Affinity.WEAK:
            raw_damage *= 2
        elif affinity == Affinity.RESIST:
            raw_damage = max(0, raw_damage // 2)
        elif affinity == Affinity.IMMUNE:
            raw_damage = 0
        elif affinity == Affinity.ABSORB:
            raw_damage = -raw_damage

        if target.guarding and raw_damage > 0 and affinity != Affinity.RESIST:
            raw_damage = max(0, raw_damage // 2)

        return raw_damage, affinity

    def clock_segments_from_roll(
        self,
        outcome: RollOutcome,
        *,
        spend_critical_opportunity: bool = False,
    ) -> int:
        if not outcome.success:
            return 0

        if outcome.margin >= 6:
            segments = 3
        elif outcome.margin >= 3:
            segments = 2
        else:
            segments = 1

        if outcome.critical_success and spend_critical_opportunity:
            segments += 2
        return segments

    def threat_clock_segments_from_roll(
        self,
        outcome: RollOutcome,
        *,
        spend_fumble_opportunity: bool = False,
    ) -> int:
        if outcome.success:
            return 0

        if outcome.margin <= -6:
            segments = 3
        elif outcome.margin <= -3:
            segments = 2
        else:
            segments = 1

        if outcome.fumble and spend_fumble_opportunity:
            segments += 2
        return segments

    def roll_team_check(
        self,
        leader: Character,
        supporters: list[Character],
        attributes: list[str],
        target_number: int,
        leader_modifier: int = 0,
    ) -> TeamCheckOutcome:
        leader_roll = self.roll_check(
            actor=leader,
            attributes=attributes,
            target_number=target_number,
            modifier=leader_modifier,
        )
        support_outcomes = []
        successful_supporters = 0
        highest_bond_strength = 0
        for supporter in supporters:
            support_roll = self.roll_check(
                actor=supporter,
                attributes=attributes,
                target_number=10,
                target=leader.name,
                reason="团队检定支援",
            )
            bonus = 0
            if support_roll.success:
                bonus = 1
                successful_supporters += 1
                highest_bond_strength = max(highest_bond_strength, supporter.bond_strength_with(leader.name))
            support_outcomes.append(
                SupportOutcome(
                    supporter=supporter.name,
                    roll=support_roll,
                    bonus=bonus,
                )
            )

        support_bonus = successful_supporters + highest_bond_strength
        final_total = leader_roll.total + support_bonus
        success = leader_roll.critical_success or (not leader_roll.fumble and final_total >= target_number)
        return TeamCheckOutcome(
            leader=leader.name,
            attributes=attributes,
            leader_roll=leader_roll,
            support_outcomes=support_outcomes,
            support_bonus=support_bonus,
            final_total=final_total,
            target_number=target_number,
            success=success,
        )

    def roll_opposed_check(
        self,
        left: Character,
        right: Character,
        attributes: list[str],
        left_modifier: int = 0,
        right_modifier: int = 0,
    ) -> OpposedCheckOutcome:
        attempts = 0
        while True:
            attempts += 1
            left_roll = self.roll_check(
                actor=left,
                attributes=attributes,
                target_number=0,
                modifier=left_modifier + (2 if left.guarding else 0),
                target=right.name,
                reason="对抗检定",
            )
            right_roll = self.roll_check(
                actor=right,
                attributes=attributes,
                target_number=0,
                modifier=right_modifier + (2 if right.guarding else 0),
                target=left.name,
                reason="对抗检定",
            )
            left_rank = self._opposed_rank(left_roll)
            right_rank = self._opposed_rank(right_roll)
            if left_rank == right_rank and left_rank != 0:
                continue
            if left_rank == right_rank and left_roll.total == right_roll.total:
                continue
            if left_rank != right_rank:
                winner = left.name if left_rank > right_rank else right.name
            else:
                winner = left.name if left_roll.total > right_roll.total else right.name
            return OpposedCheckOutcome(
                left=left.name,
                right=right.name,
                attributes=attributes,
                left_roll=left_roll,
                right_roll=right_roll,
                winner=winner,
                attempts=attempts,
            )

    def _opposed_rank(self, roll: RollOutcome) -> int:
        if roll.critical_success:
            return 1
        if roll.fumble:
            return -1
        return 0

    def initiative_target(self, enemies: list[Character]) -> int:
        return max((enemy.initiative for enemy in enemies), default=10)

    def _effective_die_size(self, actor: Character, attribute: str) -> int:
        die_size = actor.attributes[attribute]
        for _ in range(max(0, actor.equipment_attribute_bonuses.get(attribute, 0))):
            die_size = self._step_up_die(die_size)
        for _ in range(max(0, actor.attribute_bonuses.get(attribute, 0))):
            die_size = self._step_up_die(die_size)
        for status in actor.statuses:
            if attribute in _STATUS_ATTRIBUTE_PENALTIES.get(status, set()):
                die_size = self._step_down_die(die_size)
        return die_size

    def _step_down_die(self, die_size: int) -> int:
        if die_size <= 6:
            return 6
        try:
            index = _DIE_STEPS.index(die_size)
        except ValueError:
            return 6 if die_size < 6 else die_size
        return _DIE_STEPS[max(0, index - 1)]

    def _step_up_die(self, die_size: int) -> int:
        if die_size >= 12:
            return 12
        try:
            index = _DIE_STEPS.index(die_size)
        except ValueError:
            return 12 if die_size > 12 else 6
        return _DIE_STEPS[min(len(_DIE_STEPS) - 1, index + 1)]
