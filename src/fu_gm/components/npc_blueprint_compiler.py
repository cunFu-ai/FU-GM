from __future__ import annotations

import copy

from fu_gm.models import Character, EnemyRank, NPCCombatBlueprint
from fu_gm.spellbook import is_known_spell


class NPCBlueprintCompiler:
    """Compile one validated private blueprint into live combat state.

    The designer may use an isolated model to choose an inheritance template,
    but this compiler is deterministic and owns every authoritative number.
    It deliberately has no access to conversation text or a language model.
    """

    @staticmethod
    def materialize(blueprint: NPCCombatBlueprint) -> Character:
        if blueprint.status != "ready":
            raise ValueError("NPC规则蓝图尚未完成。")
        if not blueprint.attacks:
            raise ValueError("NPC规则蓝图至少需要一种基础攻击。")
        primary = blueprint.attacks[0]
        traits = [blueprint.combat_side, blueprint.species, *blueprint.traits]
        if blueprint.is_villain:
            traits.append("villain")
        known_spells = [
            profile.rules_name or profile.name
            for profile in blueprint.spells
            if is_known_spell(profile.rules_name or profile.name)
        ]
        spell_attributes = {
            (profile.rules_name or profile.name): list(profile.attributes)
            for profile in blueprint.spells
            if (profile.rules_name or profile.name) in known_spells
            and len(profile.attributes) == 2
        }
        level_damage_bonus = (
            15
            if blueprint.level >= 60
            else 10
            if blueprint.level >= 40
            else 5
            if blueprint.level >= 20
            else 0
        )
        return Character(
            name=blueprint.npc_name,
            attributes=dict(blueprint.attributes),
            max_hp=blueprint.max_hp,
            hp=blueprint.max_hp,
            max_mp=blueprint.max_mp,
            mp=blueprint.max_mp,
            level=blueprint.level,
            crisis_threshold=blueprint.crisis_threshold,
            defenses=dict(blueprint.defenses),
            affinities=dict(blueprint.affinities),
            traits=list(dict.fromkeys(traits)),
            weapon_damage=primary.damage_bonus,
            weapon_type=primary.damage_type,
            weapon_accuracy_attributes=list(primary.attributes),
            weapon_accuracy_modifier=primary.accuracy_modifier,
            weapon_range=primary.range,
            initiative=blueprint.initiative,
            abilities=list(blueprint.selected_skills),
            spells=known_spells,
            npc_spell_check_bonus=blueprint.level // 10,
            npc_spell_damage_bonus=level_damage_bonus,
            npc_spell_attributes=spell_attributes,
            npc_attacks=copy.deepcopy(blueprint.attacks),
            npc_spell_profiles=copy.deepcopy(blueprint.spells),
            npc_other_actions=list(blueprint.other_actions),
            npc_trait_rules=list(blueprint.trait_rules),
            npc_ability_profiles=copy.deepcopy(blueprint.ability_profiles),
            npc_tactics=copy.deepcopy(blueprint.tactics),
            npc_source_template=blueprint.source_template,
            permanent_status_immunities=set(blueprint.status_immunities),
            equipped_main_hand=primary.name,
            equipment_attack_targets_magic_defense=primary.targets_magic_defense,
            equipment_multi_attack=primary.multi_attack,
            equipment_on_hit_status=primary.status_effect_on_hit,
            equipment_notes=list(primary.notes),
        )

    @staticmethod
    def rank_registration(
        blueprint: NPCCombatBlueprint,
    ) -> tuple[EnemyRank, int]:
        rank = EnemyRank(blueprint.rank)
        action_count = (
            2
            if rank == EnemyRank.ELITE
            else blueprint.champion_value
            if rank == EnemyRank.CHAMPION
            else 1
        )
        return rank, action_count


__all__ = ["NPCBlueprintCompiler"]
