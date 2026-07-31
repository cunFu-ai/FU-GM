from __future__ import annotations

from dataclasses import dataclass

from fu_gm.models import Affinity, Character, EffectTiming, TimedEffect
from fu_gm.skill_library import has_skill_name


@dataclass(frozen=True)
class CombatTraitEvent:
    actor: str
    event_type: str
    summary: str
    effect: TimedEffect | None = None


class CombatTraitManager:
    """Small rules hooks for high-frequency NPC traits.

    These events are intentionally audit-first: the GM expression layer can
    turn them into fiction, but the hard-rule state stays deterministic.
    """

    def after_damage(
        self,
        target: Character,
        *,
        affinity: Affinity,
        damage: int,
        hp_before: int | None = None,
    ) -> list[CombatTraitEvent]:
        events: list[CombatTraitEvent] = []
        if damage > 0 and hp_before is not None and self._just_entered_crisis(target, hp_before):
            summary = f"{target.name} 进入危机状态。"
            if self._has_token(target, "危机效果"):
                summary += " 已记录危机效果窗口，GM 应公开提示可见变化。"
            if self.has_flight(target):
                summary += " 飞行优势在危机状态下失效。"
            events.append(
                CombatTraitEvent(
                    actor=target.name,
                    event_type="crisis_entered",
                    summary=summary,
                )
            )
        if damage > 0 and affinity == Affinity.WEAK and self.has_flight(target):
            events.append(
                CombatTraitEvent(
                    actor=target.name,
                    event_type="flight_suppressed",
                    summary=f"{target.name} 被弱点伤害击中，飞行优势暂时失效直到本轮结束。",
                    effect=TimedEffect(
                        owner=target.name,
                        effect_type="trait_suppression",
                        expires_on=EffectTiming.ROUND_END,
                        target=target.name,
                        source="飞行",
                        effect_key="flight_suppressed",
                        data={"suppressed_trait": "飞行"},
                        note="受到弱点伤害后暂时落地。",
                    ),
                )
            )
        return events

    def suppress_flight_by_opportunity(self, target: Character) -> CombatTraitEvent | None:
        if not self.has_flight(target):
            return None
        return CombatTraitEvent(
            actor=target.name,
            event_type="flight_suppressed",
            summary=f"{target.name} 被机会效果迫使落地，飞行优势暂时失效直到本轮结束。",
            effect=TimedEffect(
                owner=target.name,
                effect_type="trait_suppression",
                expires_on=EffectTiming.ROUND_END,
                target=target.name,
                source="机会效果",
                effect_key="flight_suppressed",
                data={"suppressed_trait": "飞行"},
                note="机会效果迫使飞行目标暂时落地。",
            ),
        )

    def before_zero_hp(self, target: Character) -> list[CombatTraitEvent]:
        if not self._has_token(target, "最后一搏"):
            return []
        return [
            CombatTraitEvent(
                actor=target.name,
                event_type="last_stand_window",
                summary=f"{target.name} 的 HP 归零，已打开最后一搏窗口；若其设计包含遗言、自爆、召唤或大招，应先结算再移出战斗。",
            )
        ]

    def has_flight(self, character: Character) -> bool:
        return self._has_token(character, "飞行") or self._has_token(character, "浮空")

    def _just_entered_crisis(self, character: Character, hp_before: int) -> bool:
        threshold = character.crisis_threshold if character.crisis_threshold > 0 else character.max_hp // 2
        return hp_before > threshold >= character.hp

    def _has_token(self, character: Character, token: str) -> bool:
        if has_skill_name(character.skills.keys(), token):
            return True
        if has_skill_name(character.hero_skills, token):
            return True
        text_sources = [
            *character.traits,
            *character.abilities,
            *character.equipment_notes,
            character.identity,
        ]
        return any(token in str(text) for text in text_sources)
