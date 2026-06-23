from __future__ import annotations

import re
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.models import Character, ResourceChange, StatusEffect, TriggerResult, TriggerTiming


RESOURCE_LABELS = {
    "HP": "hp",
    "MP": "mp",
    "IP": "inventory_points",
}

STATUS_LABELS = {
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


class TriggerManager:
    """统一处理装备、技能与未来神器的时机触发效果。"""

    def __init__(self, character_manager: CharacterManager) -> None:
        self.character_manager = character_manager

    def on_critical_success(
        self,
        actor_name: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> list[TriggerResult]:
        actor = self.character_manager.get(actor_name)
        context = context or {}
        results: list[TriggerResult] = []
        for source, effect_text in self._equipped_effects(actor):
            text = self._normalize(effect_text)
            if "大成功" in text and "物语点" in text and context.get("auto_optional_triggers", True):
                results.append(
                    self._modify_resource(
                        actor.name,
                        "fabula_points",
                        1,
                        source,
                        TriggerTiming.CRITICAL_SUCCESS,
                        f"{actor.name} 触发【{source}】：将大成功机会转化为 1 点物语点。",
                    )
                )
            if "大成功" in text and "额外伤害" in text:
                results.append(
                    TriggerResult(
                        actor=actor.name,
                        source=source,
                        timing=TriggerTiming.CRITICAL_SUCCESS,
                        summary=f"{actor.name} 的【{source}】可把本次大成功机会转化为额外伤害。",
                        details={"pending_extra_damage": True, "effect_text": effect_text},
                    )
                )
        return results

    def on_fumble(self, actor_name: str) -> list[TriggerResult]:
        actor = self.character_manager.get(actor_name)
        results: list[TriggerResult] = []
        for source, effect_text in self._equipped_effects(actor):
            text = self._normalize(effect_text)
            if "大失败" in text and "经验值" in text and actor.experience_points < 10:
                before = actor.experience_points
                actor.experience_points += 1
                after = actor.experience_points
                change = ResourceChange(
                    target=actor.name,
                    resource="experience_points",
                    amount=after - before,
                    before=before,
                    after=after,
                    reason=f"【{source}】在大失败时提供经验。",
                )
                results.append(
                    TriggerResult(
                        actor=actor.name,
                        source=source,
                        timing=TriggerTiming.FUMBLE,
                        summary=f"{actor.name} 触发【{source}】：获得 {after - before} 点 XP。",
                        resource_change=change,
                    )
                )
        return results

    def after_hit(
        self,
        actor_name: str,
        target_name: str,
        *,
        is_spell: bool = False,
        is_critical: bool = False,
        target_was_zero_hp: bool = False,
        context: dict[str, Any] | None = None,
    ) -> list[TriggerResult]:
        actor = self.character_manager.get(actor_name)
        target = self.character_manager.get(target_name)
        context = context or {}
        results: list[TriggerResult] = []
        for source, effect_text in self._equipped_effects(actor):
            text = self._normalize(effect_text)
            if not self._is_hit_effect_relevant(text, is_spell=is_spell):
                continue
            if "若拥有至少一段包含自卑" in text and not self._has_bond_emotion(actor, "自卑"):
                continue
            if "处于危机状态" in text and "目标" not in text and not actor.in_crisis:
                continue
            if "处于危机状态" in text and "目标" in text and not target.in_crisis:
                continue
            if "将生物HP降为0" in text and not target_was_zero_hp:
                continue

            results.extend(self._resource_restores_from_hit(actor, source, text))
            results.extend(self._extra_damage_from_hit(actor, target, source, text, is_critical, context))
            results.extend(self._spell_status_from_hit(actor, target, source, text, is_spell))
        return results

    def before_zero_hp(self, target_name: str) -> list[TriggerResult]:
        target = self.character_manager.get(target_name)
        results: list[TriggerResult] = []
        for source, effect_text in self._equipped_effects(target):
            text = self._normalize(effect_text)
            if "HP降为0" not in text and "生命值降为0" not in text:
                continue
            if "降至1HP" not in text and "1点生命值" not in text:
                continue
            cooldown_key = f"equipment:{source}:before_zero_hp"
            if cooldown_key in target.trigger_cooldowns:
                continue
            target.hp = 1
            target.trigger_cooldowns.add(cooldown_key)
            results.append(
                TriggerResult(
                    actor=target.name,
                    target=target.name,
                    source=source,
                    timing=TriggerTiming.BEFORE_ZERO_HP,
                    summary=f"{target.name} 触发【{source}】：HP 降为 0 时改为保留 1 HP。",
                    prevented_zero_hp=True,
                    details={"cooldown_key": cooldown_key},
                )
            )
            break
        return results

    def on_travel_discovery(self, party_names: list[str]) -> list[TriggerResult]:
        results: list[TriggerResult] = []
        for actor_name in party_names:
            if not self.character_manager.exists(actor_name):
                continue
            actor = self.character_manager.get(actor_name)
            for source, effect_text in self._equipped_effects(actor):
                text = self._normalize(effect_text)
                if "旅行获得发现" in text and "物语点" in text:
                    results.append(
                        self._modify_resource(
                            actor.name,
                            "fabula_points",
                            1,
                            source,
                            TriggerTiming.TRAVEL_DISCOVERY,
                            f"{actor.name} 触发【{source}】：旅行发现带来 1 点物语点。",
                        )
                    )
        return results

    def _resource_restores_from_hit(self, actor: Character, source: str, text: str) -> list[TriggerResult]:
        if "恢复" not in text:
            return []
        results: list[TriggerResult] = []
        for amount_text, resource_label in re.findall(r"恢复(\d+)(HP|MP|IP)", text):
            amount = int(amount_text)
            resource = RESOURCE_LABELS[resource_label]
            results.append(
                self._modify_resource(
                    actor.name,
                    resource,
                    amount,
                    source,
                    TriggerTiming.AFTER_HIT,
                    f"{actor.name} 触发【{source}】：命中后恢复 {amount} {resource_label}。",
                )
            )
        return results

    def _extra_damage_from_hit(
        self,
        actor: Character,
        target: Character,
        source: str,
        text: str,
        is_critical: bool,
        context: dict[str, Any],
    ) -> list[TriggerResult]:
        if "额外伤害" not in text:
            return []
        if "大成功" in text and (not is_critical or not context.get("auto_optional_triggers", True)):
            return []
        match = re.search(r"造成(\d+)点额外伤害", text)
        if not match:
            return []
        amount = int(match.group(1))
        before, after = self.character_manager.apply_damage(target.name, amount)
        return [
            TriggerResult(
                actor=actor.name,
                target=target.name,
                source=source,
                timing=TriggerTiming.AFTER_HIT,
                summary=f"{actor.name} 触发【{source}】：对 {target.name} 造成 {before - after} 点额外伤害。",
                extra_damage=before - after,
                details={"hp_before": before, "hp_after": after},
            )
        ]

    def _spell_status_from_hit(
        self,
        actor: Character,
        target: Character,
        source: str,
        text: str,
        is_spell: bool,
    ) -> list[TriggerResult]:
        if not is_spell or "施加" not in text:
            return []
        results: list[TriggerResult] = []
        for label, status in STATUS_LABELS.items():
            if label not in text:
                continue
            applied = self.character_manager.add_status(target.name, status)
            results.append(
                TriggerResult(
                    actor=actor.name,
                    target=target.name,
                    source=source,
                    timing=TriggerTiming.AFTER_HIT,
                    summary=f"{actor.name} 触发【{source}】：攻击性法术命中后使 {target.name} 陷入{label}。",
                    details={"status": status.value, "applied": applied},
                )
            )
            break
        return results

    def _modify_resource(
        self,
        actor_name: str,
        resource: str,
        amount: int,
        source: str,
        timing: TriggerTiming,
        summary: str,
    ) -> TriggerResult:
        before, after = self.character_manager.modify_resource(actor_name, resource, amount)
        change = ResourceChange(
            target=actor_name,
            resource=resource,
            amount=after - before,
            before=before,
            after=after,
            reason=f"触发【{source}】。",
        )
        return TriggerResult(
            actor=actor_name,
            source=source,
            timing=timing,
            summary=summary,
            resource_change=change,
        )

    def _equipped_effects(self, character: Character) -> list[tuple[str, str]]:
        names = [
            character.equipped_main_hand,
            character.equipped_off_hand,
            character.equipped_armor,
            character.equipped_shield,
            character.equipped_accessory,
        ]
        effects: list[tuple[str, str]] = []
        for item_name in names:
            if not item_name or item_name in {"徒手攻击", "无防具"}:
                continue
            template_name = character.equipment_templates.get(item_name, item_name)
            example = get_equipment_example(template_name)
            if example is None:
                continue
            for effect in example.effects:
                if effect and "无特殊效果" not in effect:
                    source_name = item_name if item_name != template_name else example.name
                    effects.append((source_name, effect))
        return effects

    def _is_hit_effect_relevant(self, text: str, *, is_spell: bool) -> bool:
        if is_spell:
            return "施放攻击性法术命中" in text or "法术" in text and "命中" in text
        if "施放攻击性法术" in text:
            return False
        return "命中" in text or "攻击" in text or "此武器" in text or "用此武器" in text

    def _has_bond_emotion(self, character: Character, emotion: str) -> bool:
        return any(emotion in bond.emotions for bond in character.bonds)

    def _normalize(self, text: str) -> str:
        return text.replace(" ", "").replace("生命值", "HP").replace("精神值", "MP").replace("物资点", "IP")
