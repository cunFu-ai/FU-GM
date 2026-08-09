from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fu_gm.models import Affinity, Character, EffectTiming, TimedEffect
from fu_gm.skill_library import has_skill_name


@dataclass(frozen=True)
class CombatTraitEvent:
    actor: str
    event_type: str
    summary: str
    effect: TimedEffect | None = None
    data: dict[str, Any] | None = None


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
        triggering_actor: str = "",
        is_spell: bool = False,
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
            events.extend(
                self._ability_events(
                    target,
                    "enter_crisis",
                    triggering_actor=triggering_actor,
                )
            )
        if damage > 0:
            events.extend(
                self._ability_events(
                    target,
                    "after_damage",
                    triggering_actor=triggering_actor,
                )
            )
        if damage > 0 and affinity == Affinity.WEAK:
            events.extend(
                self._ability_events(
                    target,
                    "hit_by_weakness",
                    triggering_actor=triggering_actor,
                )
            )
        if is_spell:
            events.extend(
                self._ability_events(
                    target,
                    "hit_by_spell",
                    triggering_actor=triggering_actor,
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

    def after_attack_missed(
        self,
        target: Character,
        *,
        triggering_actor: str,
    ) -> list[CombatTraitEvent]:
        return self._ability_events(
            target,
            "attack_missed",
            triggering_actor=triggering_actor,
        )

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

    def before_zero_hp(
        self,
        target: Character,
        *,
        triggering_actor: str = "",
        damage_type: str = "",
    ) -> list[CombatTraitEvent]:
        typed = self._ability_events(
            target,
            "zero_hp",
            triggering_actor=triggering_actor,
            damage_type=damage_type,
        )
        if typed:
            return typed
        if not self._has_token(target, "最后一搏"):
            return []
        return [
            CombatTraitEvent(
                actor=target.name,
                event_type="last_stand_window",
                summary=f"{target.name} 的 HP 归零，已打开最后一搏窗口；若其设计包含遗言、自爆、召唤或大招，应先结算再移出战斗。",
            )
        ]

    def after_guard(
        self,
        actor: Character,
        *,
        guarded_target: str = "",
        terrain: str = "",
    ) -> list[CombatTraitEvent]:
        return self._ability_events(
            actor,
            "after_guard",
            triggering_actor=guarded_target,
            context_keyword=terrain,
        )

    def _ability_events(
        self,
        target: Character,
        trigger: str,
        *,
        triggering_actor: str = "",
        damage_type: str = "",
        context_keyword: str = "",
    ) -> list[CombatTraitEvent]:
        events: list[CombatTraitEvent] = []
        for profile in target.npc_ability_profiles:
            if profile.trigger != trigger:
                continue
            if (
                profile.effect_type == "terrain_guard"
                and (
                    not context_keyword
                    or context_keyword not in profile.keywords
                )
            ):
                continue
            if damage_type and damage_type in profile.blocked_by_damage_types:
                continue
            cooldown_key = f"scene:npc_ability:{profile.ability_id}:{trigger}"
            if cooldown_key in target.trigger_cooldowns:
                continue
            if profile.once_per_scene or trigger in {"enter_crisis", "zero_hp"}:
                target.trigger_cooldowns.add(cooldown_key)

            details: dict[str, Any] = {
                "ability_id": profile.ability_id,
                "ability_name": profile.name,
                "source_skill": profile.source_skill,
                "trigger": profile.trigger,
                "effect_type": profile.effect_type,
                "target_scope": profile.target_scope,
                "amount": profile.amount,
                "damage_type": profile.damage_type,
                "statuses": [status.value for status in profile.statuses],
                "triggering_actor": triggering_actor,
                "ignore_resist": profile.ignore_resist,
                "damage_type_that_triggered": damage_type,
                "expires_on": (
                    profile.expires_on.value if profile.expires_on else ""
                ),
                "context_keyword": context_keyword,
            }
            if profile.affinity_changes:
                details["affinity_changes"] = {
                    key: value.value
                    for key, value in profile.affinity_changes.items()
                }
            summary = f"{target.name} 触发【{profile.name}】。"
            if (
                profile.effect_type == "affinity_change"
                and profile.target_scope == "self"
                and profile.expires_on is None
            ):
                target.temporary_affinities.update(profile.affinity_changes)
                summary += " 伤害相性发生变化。"
            elif profile.effect_type == "grant_multiattack":
                target.npc_skill_effects.setdefault("triggered_multiattack", {})[
                    profile.attack_name or "*"
                ] = profile.multi_attack
                details.update(
                    {
                        "attack_name": profile.attack_name,
                        "multi_attack": profile.multi_attack,
                    }
                )
                summary += f" 攻击获得多重攻击({profile.multi_attack})。"
            elif profile.effect_type == "ignore_resist":
                target.npc_skill_effects.setdefault("triggered_ignore_resist", []).append(
                    profile.attack_name or "*"
                )
                details["attack_name"] = profile.attack_name
                summary += " 指定攻击将无视抵抗相性。"
            elif profile.effect_type == "recover_mp":
                before = target.mp
                target.mp = min(target.max_mp, target.mp + profile.amount)
                details["resource_change"] = {
                    "resource": "mp",
                    "before": before,
                    "after": target.mp,
                }
                summary += f" 恢复{target.mp - before}点精神值。"
            elif profile.effect_type == "status_apply" and profile.target_scope == "self":
                applied = []
                for status in profile.statuses:
                    if (
                        status not in target.permanent_status_immunities
                        and status not in target.temporary_status_immunities
                        and status not in target.statuses
                    ):
                        target.statuses.append(status)
                        applied.append(status.value)
                details["applied_statuses"] = applied
                summary += " 异常状态效果已结算。"
            elif profile.effect_type == "clear_statuses":
                details["pending_external_resolution"] = True
                summary += " 已建立异常状态清除效果。"
            else:
                details["pending_external_resolution"] = True
                summary += " 已建立可执行的触发效果。"
            events.append(
                CombatTraitEvent(
                    actor=target.name,
                    event_type=f"npc_ability_{trigger}",
                    summary=summary,
                    data=details,
                )
            )
        return events

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
            *character.npc_trait_rules,
            *character.equipment_notes,
            character.identity,
        ]
        return any(token in str(text) for text in text_sources)
