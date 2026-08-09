from __future__ import annotations

from collections.abc import Iterable

from fu_gm.models import Affinity, Character, NPCAbilityProfile


def active_npc_abilities(
    character: Character,
    *,
    trigger: str | None = None,
    effect_type: str | None = None,
) -> Iterable[NPCAbilityProfile]:
    for profile in character.npc_ability_profiles:
        if trigger is not None and profile.trigger != trigger:
            continue
        if effect_type is not None and profile.effect_type != effect_type:
            continue
        if profile.trigger == "while_in_crisis" and not character.in_crisis:
            continue
        yield profile


def npc_check_bonus(character: Character, context: str = "") -> int:
    total = sum(
        max(0, int(profile.amount))
        for profile in active_npc_abilities(
            character,
            trigger="while_in_crisis",
            effect_type="check_bonus",
        )
    )
    clean_context = str(context or "").strip()
    if clean_context:
        total += npc_context_check_bonus(character, clean_context)
    return total


def npc_context_check_bonus(character: Character, context: str) -> int:
    clean_context = str(context or "").strip()
    if not clean_context:
        return 0
    return sum(
        max(0, int(profile.amount))
        for profile in active_npc_abilities(
            character,
            trigger="check_context",
            effect_type="check_bonus",
        )
        if not profile.keywords
        or any(keyword in clean_context for keyword in profile.keywords)
    )


def npc_attack_adjustment(
    character: Character,
    attack_name: str,
) -> tuple[int, str]:
    damage_bonus = 0
    damage_type = ""
    for profile in active_npc_abilities(
        character,
        trigger="while_in_crisis",
        effect_type="modify_attack",
    ):
        if profile.attack_name and profile.attack_name != attack_name:
            continue
        damage_bonus += int(profile.amount)
        if profile.damage_type:
            damage_type = profile.damage_type
    return damage_bonus, damage_type


def npc_affinity_override(
    character: Character,
    damage_type: str,
) -> Affinity | None:
    result: Affinity | None = None
    for profile in active_npc_abilities(
        character,
        trigger="while_in_crisis",
        effect_type="affinity_change",
    ):
        if damage_type in profile.affinity_changes:
            result = profile.affinity_changes[damage_type]
    return result


def npc_clock_extra_segments(character: Character, clock_name: str) -> int:
    clean_name = str(clock_name or "").strip()
    return sum(
        max(0, int(profile.amount))
        for profile in active_npc_abilities(
            character,
            trigger="clock_change",
            effect_type="clock_extra_segments",
        )
        if not profile.keywords
        or any(keyword in clean_name for keyword in profile.keywords)
    )


def is_living_creature(character: Character) -> bool:
    tags = {str(value or "").strip().lower() for value in character.traits}
    return not bool(tags & {"construct", "undead", "构装体", "不死族"})


__all__ = [
    "active_npc_abilities",
    "is_living_creature",
    "npc_affinity_override",
    "npc_attack_adjustment",
    "npc_check_bonus",
    "npc_context_check_bonus",
    "npc_clock_extra_segments",
]
