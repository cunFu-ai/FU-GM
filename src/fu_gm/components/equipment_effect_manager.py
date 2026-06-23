from __future__ import annotations

import re

from fu_gm.equipment_catalog import EquipmentExample, get_equipment_example
from fu_gm.models import Affinity, Character, StatusEffect


STATUS_ALIASES = {
    "迟缓": StatusEffect.SLOW,
    "缓慢": StatusEffect.SLOW,
    "眩晕": StatusEffect.DAZED,
    "晕眩": StatusEffect.DAZED,
    "虚弱": StatusEffect.WEAKENED,
    "动摇": StatusEffect.SHAKEN,
    "颤抖": StatusEffect.SHAKEN,
    "激怒": StatusEffect.ENRAGED,
    "中毒": StatusEffect.POISONED,
}

DAMAGE_ALIASES = {
    "物理": "physical",
    "雷系": "lightning",
    "雷": "lightning",
    "电": "lightning",
    "风系": "wind",
    "风": "wind",
    "冰系": "ice",
    "冰": "ice",
    "火系": "fire",
    "火": "fire",
    "土系": "earth",
    "土": "earth",
    "毒系": "poison",
    "毒": "poison",
    "光系": "light",
    "光": "light",
    "暗系": "dark",
    "暗": "dark",
}


class EquipmentEffectManager:
    """把结构化装备库里的效果文本落地到角色派生状态。"""

    def refresh_character(self, character: Character) -> None:
        self._reset_equipment_effects(character)
        for item_name in self._equipped_item_names(character):
            template_name = self._template_name(character, item_name)
            example = get_equipment_example(template_name)
            if example is None:
                continue
            self._apply_weapon_profile(character, item_name, example)
            if template_name != item_name:
                character.equipment_notes.append(f"{item_name} 按【{template_name}】数值模板结算。")
            for effect in example.effects:
                self._apply_effect_text(character, effect)

    def _reset_equipment_effects(self, character: Character) -> None:
        character.equipment_status_immunities.clear()
        character.equipment_affinities.clear()
        character.equipment_defense_bonuses = {"physical": 0, "magic": 0}
        character.equipment_attribute_bonuses = {"DEX": 0, "INS": 0, "MIG": 0, "WLP": 0}
        character.equipment_accuracy_bonus = 0
        character.equipment_spell_bonus = 0
        character.equipment_initiative_bonus = 0
        character.equipment_attack_damage_bonus = 0
        character.equipment_spell_damage_bonus = 0
        character.equipment_healing_bonus = 0
        character.equipment_multi_attack = 0
        character.equipment_attack_targets_magic_defense = False
        character.equipment_ignore_resist = False
        character.equipment_ignore_all_affinities = False
        character.equipment_on_hit_status = None
        character.equipment_notes = []

    def _equipped_item_names(self, character: Character) -> list[str]:
        names = [
            character.equipped_main_hand,
            character.equipped_off_hand,
            character.equipped_armor,
            character.equipped_shield,
            character.equipped_accessory,
        ]
        return [name for name in names if name and name != "无防具" and name != "徒手攻击"]

    def _apply_weapon_profile(self, character: Character, item_name: str, example: EquipmentExample) -> None:
        if item_name != character.equipped_main_hand:
            return
        if not example.accuracy_attributes:
            return
        character.weapon_accuracy_attributes = list(example.accuracy_attributes)
        character.weapon_accuracy_modifier = example.accuracy_modifier
        character.weapon_damage = example.damage_bonus
        character.weapon_type = example.damage_type or "physical"
        character.weapon_range = example.range_type or "melee"

    def _template_name(self, character: Character, item_name: str) -> str:
        return character.equipment_templates.get(item_name, item_name)

    def _apply_effect_text(self, character: Character, effect: str) -> None:
        normalized = effect.replace(" ", "")
        if not normalized or "无特殊效果" in normalized:
            return
        if ("处于危机状态" in normalized or "危机状态时" in normalized) and not character.in_crisis:
            character.equipment_notes.append(effect)
            return

        self._apply_status_immunity(character, normalized)
        self._apply_affinity(character, normalized)
        self._apply_numeric_bonus(character, normalized)
        self._apply_attack_profile(character, normalized)
        self._apply_on_hit_status(character, normalized)
        self._apply_attribute_bonus(character, normalized)
        character.equipment_notes.append(effect)

    def _apply_status_immunity(self, character: Character, text: str) -> None:
        if "免疫所有异常状态" in text:
            character.equipment_status_immunities.update(STATUS_ALIASES.values())
            self._clear_immunized_statuses(character)
            return
        for label, status in STATUS_ALIASES.items():
            if f"免疫{label}" in text:
                character.equipment_status_immunities.add(status)
        self._clear_immunized_statuses(character)

    def _clear_immunized_statuses(self, character: Character) -> None:
        character.statuses = [
            status for status in character.statuses if status not in character.equipment_status_immunities
        ]

    def _apply_affinity(self, character: Character, text: str) -> None:
        for label, damage_type in DAMAGE_ALIASES.items():
            if ("抵抗相性" in text and "伤害" in text and label in text) or (
                f"对{label}伤害获得抵抗相性" in text
            ):
                character.equipment_affinities[damage_type] = Affinity.RESIST
            if "免疫" in text and "伤害" in text and label in text:
                character.equipment_affinities[damage_type] = Affinity.IMMUNE
            if "吸收" in text and "伤害" in text and label in text:
                character.equipment_affinities[damage_type] = Affinity.ABSORB
            if "弱点状态" in text and "伤害" in text and label in text:
                character.equipment_affinities.setdefault(damage_type, Affinity.WEAK)

    def _apply_numeric_bonus(self, character: Character, text: str) -> None:
        already_included = "已计入" in text
        if "物防和魔防获得+1" in text and not already_included:
            character.equipment_defense_bonuses["physical"] += 1
            character.equipment_defense_bonuses["magic"] += 1
        elif "物防获得+1" in text and not already_included:
            character.equipment_defense_bonuses["physical"] += 1
        elif "魔防获得+1" in text and not already_included:
            character.equipment_defense_bonuses["magic"] += 1

        if "命中检定获得+1" in text:
            character.equipment_accuracy_bonus += 1
        if "施法检定获得+1" in text:
            character.equipment_spell_bonus += 1
        if "先攻获得+4" in text and not already_included:
            character.equipment_initiative_bonus += 4
        if "法术造成5点额外伤害" in text or "法术将会造成5点额外伤害" in text:
            character.equipment_spell_damage_bonus += 5
        if "恢复HP的法术时，额外恢复5HP" in text or "恢复生命值的法术时，额外恢复5HP" in text:
            character.equipment_healing_bonus += 5
        if "攻击造成5点额外伤害" in text:
            character.equipment_attack_damage_bonus += 5

    def _apply_attack_profile(self, character: Character, text: str) -> None:
        if "攻击针对目标魔防" in text or "针对目标的魔防" in text or "针对魔防" in text:
            character.equipment_attack_targets_magic_defense = True
        if "伤害无视抵抗相性" in text:
            character.equipment_ignore_resist = True
        if "伤害无视所有相性" in text:
            character.equipment_ignore_all_affinities = True
        multi_match = re.search(r"多重攻击[（(](\d+)[）)]", text)
        if multi_match:
            character.equipment_multi_attack = max(character.equipment_multi_attack, int(multi_match.group(1)))

    def _apply_on_hit_status(self, character: Character, text: str) -> None:
        if "对每个命中的目标施加" not in text and "命中后施加" not in text:
            return
        for label, status in STATUS_ALIASES.items():
            if label in text:
                character.equipment_on_hit_status = status
                return

    def _apply_attribute_bonus(self, character: Character, text: str) -> None:
        attribute_labels = {
            "DEX": "DEX",
            "敏捷": "DEX",
            "INS": "INS",
            "洞察": "INS",
            "MIG": "MIG",
            "力量": "MIG",
            "WLP": "WLP",
            "意志": "WLP",
        }
        for label, attribute in attribute_labels.items():
            if f"{label}骰尺寸提升一级" in text or f"{label}骰等级提升一级" in text:
                character.equipment_attribute_bonuses[attribute] += 1
