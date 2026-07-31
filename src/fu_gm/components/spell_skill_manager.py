from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.models import Character, EquipmentItemType, SpellDefinition, SpellEffectType
from fu_gm.skill_library import skill_rank

from .skill_trigger_manager import SkillTriggerManager


@dataclass(frozen=True)
class PreparedSpellSkills:
    mp_cost: int
    attributes: tuple[str, str]
    check_modifier: int = 0
    damage_bonus: int = 0
    sources: tuple[str, ...] = ()
    weapon_name: str = ""
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error

    def as_payload(self) -> dict[str, object]:
        return {
            "mp_cost": self.mp_cost,
            "attributes": list(self.attributes),
            "check_modifier": self.check_modifier,
            "damage_bonus": self.damage_bonus,
            "sources": list(self.sources),
            "weapon_name": self.weapon_name,
        }


class SpellSkillManager:
    """Prepares spell modifiers before resources or dice are committed."""

    ATTACK_EFFECTS = {SpellEffectType.DAMAGE, SpellEffectType.MP_DAMAGE}

    def __init__(self, trigger_manager: SkillTriggerManager) -> None:
        self.trigger_manager = trigger_manager

    def prepare(
        self,
        actor: Character,
        definition: SpellDefinition,
        *,
        base_mp_cost: int,
        target_count: int,
        parameters: dict[str, Any],
    ) -> PreparedSpellSkills:
        attack_spell = definition.effect_type in self.ATTACK_EFFECTS
        instant_spell = definition.duration is None
        single_target = target_count == 1
        extra_mp = max(0, self._int(parameters.get("cataclysm_extra_mp"), 0))
        use_weapon_formula = bool(parameters.get("use_weapon_formula", False))
        magic_weapon_equipped = self.trigger_manager.has_magic_weapon(actor)

        error = self._validate_cataclysm(
            actor,
            extra_mp=extra_mp,
            instant_spell=instant_spell,
            magic_weapon_equipped=magic_weapon_equipped,
        )
        if error:
            return self._invalid(base_mp_cost, definition, error)

        attributes = tuple(definition.attributes[:2])
        chimerist_species = actor.chimerist_spell_species.get(
            definition.name
        )
        if chimerist_species:
            choice = list(actor.skill_options.get("形意咒法", []))
            if not choice:
                return self._invalid(
                    base_mp_cost,
                    definition,
                    "【形意咒法】尚未记录固定施法属性组合。",
                )
            attributes = (
                ("MIG", "WLP")
                if str(choice[0]).replace(" ", "") in {"力量+意志", "MIG+WLP"}
                else ("INS", "WLP")
            )
        weapon_modifier = 0
        weapon_name = ""
        if use_weapon_formula:
            weapon_name, attributes, weapon_modifier, error = self._weapon_formula(
                actor,
                parameters,
                total_mp_cost=base_mp_cost + extra_mp,
                attack_spell=attack_spell,
                single_target=single_target,
            )
            if error:
                return self._invalid(base_mp_cost, definition, error)

        event = self.trigger_manager.emit(
            "prepare_spell",
            actor,
            attack_spell=attack_spell,
            instant_spell=instant_spell,
            magic_weapon_equipped=magic_weapon_equipped,
            extra_mp=extra_mp,
        )
        check_modifier = weapon_modifier
        damage_bonus = 0
        sources: list[str] = []
        for effect in event.effects:
            if effect.resource == "check_modifier":
                check_modifier += effect.amount
            elif effect.resource == "damage_bonus" and definition.effect_type == SpellEffectType.DAMAGE:
                damage_bonus += effect.amount
            if effect.source not in sources:
                sources.append(effect.source)
        if use_weapon_formula:
            sources.append("以械引咒")
        if chimerist_species:
            sources.append(f"形意咒法：{chimerist_species}")
        return PreparedSpellSkills(
            mp_cost=base_mp_cost + extra_mp,
            attributes=(str(attributes[0]), str(attributes[1])),
            check_modifier=check_modifier,
            damage_bonus=damage_bonus,
            sources=tuple(sources),
            weapon_name=weapon_name,
        )

    def _validate_cataclysm(
        self,
        actor: Character,
        *,
        extra_mp: int,
        instant_spell: bool,
        magic_weapon_equipped: bool,
    ) -> str:
        if extra_mp <= 0:
            return ""
        rank = skill_rank(actor.skills, "天灾骤降")
        if rank <= 0:
            return f"{actor.name} 未习得【天灾骤降】，不能提高法术的精神值消耗。"
        if not instant_spell:
            return "【天灾骤降】只能用于持续时间为瞬发的法术。"
        if not magic_weapon_equipped:
            return "【天灾骤降】需要当前装备魔法类武器。"
        if extra_mp > rank * 10:
            return f"【天灾骤降】至多额外消耗 {rank * 10} 点精神值。"
        return ""

    def _weapon_formula(
        self,
        actor: Character,
        parameters: dict[str, Any],
        *,
        total_mp_cost: int,
        attack_spell: bool,
        single_target: bool,
    ) -> tuple[str, tuple[str, str], int, str]:
        rank = skill_rank(actor.skills, "以械引咒")
        if rank <= 0:
            return "", ("INS", "WLP"), 0, f"{actor.name} 未习得【以械引咒】。"
        if not attack_spell or not single_target:
            return "", ("INS", "WLP"), 0, "【以械引咒】只能用于针对单个目标的攻击性法术。"
        if total_mp_cost > rank * 20:
            return "", ("INS", "WLP"), 0, f"该法术的精神值总消耗超过【以械引咒】上限 {rank * 20}。"

        requested = str(parameters.get("spell_weapon") or actor.equipped_main_hand or "").strip()
        equipped = [name for name in (actor.equipped_main_hand, actor.equipped_off_hand) if name]
        if requested not in equipped:
            return "", ("INS", "WLP"), 0, f"【{requested or '所选武器'}】当前并未装备。"
        template_name = actor.equipment_templates.get(requested, requested)
        example = get_equipment_example(template_name)
        if example is None or example.item_type != EquipmentItemType.WEAPON or len(example.accuracy_attributes) != 2:
            return "", ("INS", "WLP"), 0, f"【{requested}】没有可用于【以械引咒】的命中算式。"
        if example.category == "魔法":
            return "", ("INS", "WLP"), 0, "【以械引咒】必须选择一件非魔法类武器。"
        attributes = (str(example.accuracy_attributes[0]), str(example.accuracy_attributes[1]))
        modifier = int(example.accuracy_modifier)
        if "DEX" in attributes:
            modifier += rank
        return requested, attributes, modifier, ""

    @staticmethod
    def _invalid(base_mp_cost: int, definition: SpellDefinition, error: str) -> PreparedSpellSkills:
        attributes = tuple(definition.attributes[:2])
        return PreparedSpellSkills(
            mp_cost=base_mp_cost,
            attributes=(str(attributes[0]), str(attributes[1])),
            error=error,
        )

    @staticmethod
    def _int(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
