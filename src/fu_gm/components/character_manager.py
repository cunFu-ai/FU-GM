from __future__ import annotations

from dataclasses import replace

from fu_gm.components.rules_engine import resolve_affinity
from fu_gm.models import Affinity, Bond, Character, StatusEffect


_BOND_EMOTION_ALIASES = {
    "赞赏": "钦佩",
    "钦佩": "钦佩",
    "自卑": "自卑",
    "不信任": "猜忌",
    "猜忌": "猜忌",
    "怀疑": "猜忌",
    "信赖": "信赖",
    "信任": "信赖",
    "忠诚": "信赖",
    "喜爱": "喜爱",
    "爱": "喜爱",
    "憎恨": "憎恨",
    "仇恨": "憎恨",
    "恨": "憎恨",
}
_BOND_EMOTION_GROUPS = {
    "钦佩": "esteem",
    "自卑": "esteem",
    "信赖": "trust",
    "猜忌": "trust",
    "喜爱": "affection",
    "憎恨": "affection",
}


class CharacterManager:
    """维护 PC 与 NPC 的权威状态。"""

    def __init__(self) -> None:
        self._characters: dict[str, Character] = {}

    def add(self, character: Character) -> None:
        self._characters[character.name] = replace(character)

    def get(self, name: str) -> Character:
        return self._characters[name]

    def exists(self, name: str) -> bool:
        return name in self._characters

    def all(self) -> list[Character]:
        return list(self._characters.values())

    def modify_resource(self, name: str, resource: str, amount: int) -> tuple[int, int]:
        character = self.get(name)
        before = getattr(character, resource)
        if resource in {"hp", "mp"}:
            max_value = getattr(character, f"max_{resource}")
            after = max(0, min(max_value, before + amount))
        elif resource == "inventory_points":
            max_value = character.max_inventory_points or 6
            after = max(0, min(max_value, before + amount))
        else:
            after = max(0, before + amount)
        setattr(character, resource, after)
        return before, after

    def apply_damage(self, name: str, amount: int) -> tuple[int, int]:
        return self.modify_resource(name, "hp", -amount)

    def add_status(self, name: str, status: StatusEffect) -> bool:
        character = self.get(name)
        if status in character.permanent_status_immunities:
            return False
        if status in character.equipment_status_immunities:
            return False
        if status in character.temporary_status_immunities:
            return False
        if status in character.statuses:
            return False
        character.statuses.append(status)
        return True

    def remove_status(self, name: str, status: StatusEffect) -> bool:
        character = self.get(name)
        if status not in character.statuses:
            return False
        character.statuses.remove(status)
        return True

    def clear_statuses(self, name: str) -> None:
        self.get(name).statuses.clear()

    def set_guarding(self, name: str, guarding: bool, guarded_target: str | None = None) -> None:
        character = self.get(name)
        character.guarding = guarding
        character.guarded_target = guarded_target if guarding else None

    def set_temporary_affinity(self, name: str, damage_type: str, affinity) -> None:
        self.get(name).temporary_affinities[damage_type] = affinity

    def clear_temporary_affinity(self, name: str, damage_type: str) -> None:
        self.get(name).temporary_affinities.pop(damage_type, None)

    def add_defense_bonus(self, name: str, defense_type: str, amount: int) -> None:
        character = self.get(name)
        character.defense_bonuses[defense_type] = character.defense_bonuses.get(defense_type, 0) + amount

    def remove_defense_bonus(self, name: str, defense_type: str, amount: int) -> None:
        character = self.get(name)
        current = character.defense_bonuses.get(defense_type, 0) - amount
        character.defense_bonuses[defense_type] = current

    def effective_affinity(self, name: str, damage_type: str):
        character = self.get(name)
        return resolve_affinity(
            character.affinities.get(damage_type, Affinity.NORMAL),
            character.equipment_affinities.get(damage_type),
            character.temporary_affinities.get(damage_type),
        )

    def effective_defense(self, name: str, defense_type: str) -> int:
        character = self.get(name)
        base = character.defenses[defense_type] + character.defense_bonuses.get(defense_type, 0)
        return max(base, character.defense_floors.get(defense_type, 0))

    def add_defense_floor(self, name: str, defense_type: str, amount: int) -> None:
        character = self.get(name)
        character.defense_floors[defense_type] = max(character.defense_floors.get(defense_type, 0), amount)

    def set_defense_floor(self, name: str, defense_type: str, amount: int) -> None:
        self.get(name).defense_floors[defense_type] = max(0, amount)

    def add_status_immunity(self, name: str, status: StatusEffect) -> None:
        character = self.get(name)
        character.temporary_status_immunities.add(status)
        self.remove_status(name, status)

    def remove_status_immunity(self, name: str, status: StatusEffect) -> None:
        self.get(name).temporary_status_immunities.discard(status)

    def clear_status_immunities(self, name: str) -> None:
        self.get(name).temporary_status_immunities.clear()

    def add_attribute_bonus(self, name: str, attribute: str, steps: int) -> None:
        character = self.get(name)
        character.attribute_bonuses[attribute] = character.attribute_bonuses.get(attribute, 0) + steps

    def remove_attribute_bonus(self, name: str, attribute: str, steps: int) -> None:
        character = self.get(name)
        character.attribute_bonuses[attribute] = character.attribute_bonuses.get(attribute, 0) - steps

    def set_weapon_damage_type_override(self, name: str, damage_type: str | None) -> None:
        self.get(name).weapon_damage_type_override = damage_type

    def effective_weapon_damage_type(self, name: str) -> str:
        character = self.get(name)
        return character.weapon_damage_type_override or character.weapon_type

    def guardian_for(self, protected_name: str) -> Character | None:
        for character in self._characters.values():
            if character.guarding and character.guarded_target == protected_name:
                return character
        return None

    def manage_bond(
        self,
        actor_name: str,
        target: str,
        emotions: list[str] | tuple[str, ...] | None = None,
        *,
        mode: str = "upsert",
        replace: bool = False,
    ) -> Bond | None:
        character = self.get(actor_name)
        clean_target = str(target or "").strip()
        if not clean_target:
            raise ValueError("羁绊目标不能为空。")
        if clean_target == actor_name:
            raise ValueError("角色不能对自己建立羁绊。")

        if mode in {"erase", "remove", "delete"}:
            before = len(character.bonds)
            character.bonds = [bond for bond in character.bonds if bond.target != clean_target]
            return None if len(character.bonds) != before else None

        normalized_emotions = self.normalize_bond_emotions(list(emotions or []))
        existing = next((bond for bond in character.bonds if bond.target == clean_target), None)
        if existing is None:
            if len(character.bonds) >= 6:
                raise ValueError("角色最多只能同时拥有 6 段羁绊；请先抹除一段旧羁绊。")
            if not normalized_emotions:
                raise ValueError("建立羁绊时至少需要一种情感。")
            bond = Bond(target=clean_target, emotions=normalized_emotions)
            character.bonds.append(bond)
            return bond

        if replace:
            existing.emotions = normalized_emotions
        else:
            merged = list(existing.emotions)
            for emotion in normalized_emotions:
                group = _BOND_EMOTION_GROUPS[emotion]
                merged = [current for current in merged if _BOND_EMOTION_GROUPS.get(current) != group]
                merged.append(emotion)
            existing.emotions = self.normalize_bond_emotions(merged)
        return existing

    def normalize_bond_emotions(self, emotions: list[str]) -> list[str]:
        by_group: dict[str, str] = {}
        for raw_emotion in emotions:
            text = str(raw_emotion or "").strip()
            if not text:
                continue
            canonical = _BOND_EMOTION_ALIASES.get(text, text)
            if canonical not in _BOND_EMOTION_GROUPS:
                raise ValueError(f"未知羁绊情感：{raw_emotion}")
            by_group[_BOND_EMOTION_GROUPS[canonical]] = canonical
        ordered = []
        for group in ("esteem", "trust", "affection"):
            if group in by_group:
                ordered.append(by_group[group])
        return ordered[:3]

    def format_status(self, character: Character) -> str:
        crisis = " (危机状态!)" if character.in_crisis else ""
        statuses = ""
        if character.statuses:
            statuses = "，异常：" + "、".join(status.value for status in character.statuses)
        buffs = []
        if any(value != 0 for value in character.defense_bonuses.values()):
            buffs.append(
                "防御加值 "
                + "/".join(f"{kind}+{value}" for kind, value in character.defense_bonuses.items() if value)
            )
        if character.temporary_affinities:
            buffs.append(
                "临时相性 " + "、".join(f"{damage}:{affinity.value}" for damage, affinity in character.temporary_affinities.items())
            )
        if any(value for value in character.defense_floors.values()):
            buffs.append(
                "防御下限 "
                + "/".join(f"{kind}:{value}" for kind, value in character.defense_floors.items() if value)
            )
        if character.temporary_status_immunities:
            buffs.append(
                "状态免疫 "
                + "、".join(status.value for status in sorted(character.temporary_status_immunities, key=lambda item: item.value))
            )
        if character.equipment_status_immunities:
            buffs.append(
                "装备免疫 "
                + "、".join(status.value for status in sorted(character.equipment_status_immunities, key=lambda item: item.value))
            )
        if character.equipment_affinities:
            buffs.append(
                "装备相性 "
                + "、".join(f"{damage}:{affinity.value}" for damage, affinity in character.equipment_affinities.items())
            )
        if any(value for value in character.attribute_bonuses.values()):
            buffs.append(
                "属性强化 "
                + "、".join(f"{attr}+{value}" for attr, value in character.attribute_bonuses.items() if value)
            )
        if any(value for value in character.equipment_attribute_bonuses.values()):
            buffs.append(
                "装备属性 "
                + "、".join(f"{attr}+{value}" for attr, value in character.equipment_attribute_bonuses.items() if value)
            )
        if character.weapon_damage_type_override:
            buffs.append(f"武器附魔 {character.weapon_damage_type_override}")
        buff_text = f"，增益：{'；'.join(buffs)}" if buffs else ""
        return (
            f"{character.name}: HP {character.hp}/{character.max_hp}{crisis}, "
            f"MP {character.mp}/{character.max_mp}, 物语点 {character.fabula_points}{statuses}{buff_text}"
        )
