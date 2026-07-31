from __future__ import annotations

from collections.abc import Callable

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Bond,
    DecisionWindow,
    EffectTiming,
    ResourceChange,
    StatusEffect,
    TimedEffect,
)


SpellParameterResolver = Callable[
    [Action, DecisionWindow, dict[str, object]],
    ActionResolution,
]
InvestigationResolver = Callable[[Action], ActionResolution]


class DecisionActionResolver:
    """Commit persisted player choices outside the general rule interceptor.

    The decision window owns who may answer and which options are legal. This
    service owns the concrete effects of the selected option. Keeping both
    responsibilities outside ``ActionInterceptor`` prevents every new skill
    choice from adding another branch to the central action transaction.
    """

    _STATUS_LABELS = {
        StatusEffect.SLOW: "迟缓",
        StatusEffect.DAZED: "眩晕",
        StatusEffect.WEAKENED: "虚弱",
        StatusEffect.SHAKEN: "动摇",
        StatusEffect.ENRAGED: "激怒",
        StatusEffect.POISONED: "中毒",
    }

    def __init__(
        self,
        characters: CharacterManager,
        conflict: ConflictManager,
        decisions: DecisionWindowManager,
    ) -> None:
        self.characters = characters
        self.conflict = conflict
        self.decisions = decisions

    def resolve_zero_hp(
        self,
        action: Action,
        *,
        require_all_sacrifice_conditions: bool = False,
    ) -> ActionResolution:
        target = str(action.parameters.get("target") or action.parameters.get("actor") or "").strip()
        if not target:
            pending = self.conflict.pending_zero_hp_decision()
            target = str((pending or {}).get("target") or "")
        raw_choice = str(action.parameters.get("choice") or "").strip().lower()
        choice = {
            "sacrifice": "sacrifice",
            "牺牲": "sacrifice",
            "give_up_resistance": "give_up_resistance",
            "give-up": "give_up_resistance",
            "放弃抵抗": "give_up_resistance",
            "活下去": "give_up_resistance",
        }.get(raw_choice, raw_choice)
        if choice not in {"sacrifice", "give_up_resistance"}:
            raise ValueError("生命值归零时必须由玩家选择‘牺牲’或‘放弃抵抗’。")
        pending_decision = self.conflict.pending_zero_hp_decision(target)
        if pending_decision is None:
            raise ValueError(f"{target} 当前没有等待处理的生命值归零选择。")
        heroic_outcome = ""
        consequence_type = ""
        consequence = ""
        new_theme = ""
        remove_target = ""
        new_target = ""
        new_emotion = ""
        if choice == "sacrifice":
            heroic_outcome = str(action.parameters.get("heroic_outcome") or "").strip()
            if not heroic_outcome:
                raise ValueError("牺牲必须说明角色以生命实际成就了什么。")
        else:
            consequence_type = str(
                action.parameters.get("consequence_type") or ""
            ).strip()
            consequence = str(action.parameters.get("consequence") or "").strip()
            if consequence_type not in {"黑暗", "绝望", "损失", "怨恨", "分离"}:
                raise ValueError("放弃抵抗必须选择黑暗、绝望、损失、怨恨或分离中的一项后果。")
            if not consequence:
                raise ValueError("放弃抵抗必须由GM给出一项具体后果。")
            character = self.characters.get(target)
            if consequence_type == "黑暗":
                new_theme = str(action.parameters.get("new_theme") or "").strip()
                if new_theme not in {"愤怒", "疑虑", "愧疚", "复仇"}:
                    raise ValueError("【黑暗】后果必须把主题改为愤怒、疑虑、愧疚或复仇之一。")
            elif consequence_type == "怨恨":
                remove_target = str(
                    action.parameters.get("remove_bond_target") or ""
                ).strip()
                new_target = str(
                    action.parameters.get("new_bond_target") or ""
                ).strip()
                new_emotion = str(
                    action.parameters.get("new_emotion") or ""
                ).strip()
                if not any(bond.target == remove_target for bond in character.bonds):
                    raise ValueError("【怨恨】后果必须抹除一段现有羁绊。")
                if not new_target or new_emotion not in {"憎恨", "自卑", "猜忌"}:
                    raise ValueError("【怨恨】后果必须建立一段带憎恨、自卑或猜忌的替代羁绊。")
        event = self.conflict.resolve_pending_zero_hp(
            target,
            choice=choice,
            consequence=str(action.parameters.get("consequence") or ""),
            sacrifice_benefits_bond=action.parameters.get("sacrifice_benefits_bond"),
            sacrifice_betters_world=action.parameters.get("sacrifice_betters_world"),
            require_all_sacrifice_conditions=require_all_sacrifice_conditions,
        )
        if choice == "sacrifice":
            event.summary = f"{event.summary} {heroic_outcome}"
        else:
            character = self.characters.get(target)
            if consequence_type == "黑暗":
                character.theme = new_theme
            elif consequence_type == "怨恨":
                self.characters.manage_bond(target, remove_target, mode="erase")
                self.characters.manage_bond(
                    target,
                    new_target,
                    [new_emotion],
                    replace=True,
                )
            event.summary = f"{event.summary} 后果是：{consequence}"
        payload: dict[str, object] = {
            "conflict_event": event,
            "zero_hp_choice_resolved": True,
            "target_status": self.characters.format_status(
                self.characters.get(target)
            ),
        }
        self._attach_deferred_turn_resume(
            payload,
            pending_decision,
            source_action_type="zero_hp",
        )
        return ActionResolution(
            action=action,
            rules_text=event.summary,
            payload=payload,
        )

    def resolve(
        self,
        action: Action,
        *,
        resolve_spell_parameter: SpellParameterResolver,
        resolve_investigation: InvestigationResolver,
    ) -> ActionResolution:
        actor_name = str(action.parameters.get("actor") or "").strip()
        window_id = str(action.parameters.get("window_id") or "").strip()
        window = self.decisions.find_pending(window_id=window_id)
        if window is None:
            raise ValueError("这个选择已经结束，或对应的待决窗口不存在。")
        if actor_name != window.owner:
            raise ValueError(f"只有【{window.owner}】的玩家可以处理这个选择。")
        selected = action.parameters.get("selected_option")
        selected = dict(selected) if isinstance(selected, dict) else {
            "choice": action.parameters.get("choice", "")
        }
        if window.kind == "spell_parameter":
            return resolve_spell_parameter(action, window, selected)
        if window.kind == "npc_fate":
            choice = str(
                selected.get("choice") or action.parameters.get("choice") or ""
            ).strip()
            fate_description = str(
                selected.get("fate_description")
                or action.parameters.get("fate_description")
                or ""
            ).strip()
            event = self.conflict.resolve_pending_npc_fate(
                window_id=window.window_id,
                responder=actor_name,
                choice=choice,
                fate_description=fate_description,
            )
            payload: dict[str, object] = {
                "decision_window_id": window.window_id,
                "decision_kind": window.kind,
                "conflict_event": event,
                "npc_fate_resolved": True,
            }
            self._attach_deferred_turn_resume(
                payload,
                window.payload,
                source_action_type="npc_fate",
            )
            return ActionResolution(
                action=action,
                rules_text=event.summary,
                payload=payload,
            )
        if window.kind == "held_action":
            choice = str(
                selected.get("choice") or action.parameters.get("choice") or ""
            ).strip()
            if choice == "confirm":
                raise ValueError(
                    "确认缓存行动时必须提交原本的实际行动，不能只关闭待决窗口。"
                )
            held = self.conflict.withdraw_held_action(
                actor_name,
                window_id=window.window_id,
                choice=choice,
            )
            return ActionResolution(
                action=action,
                rules_text=(
                    f"{actor_name}放弃了先前缓存的行动。"
                    if choice == "discard"
                    else f"{actor_name}可以重新声明本回合要做的行动。"
                ),
                payload={
                    "decision_window_id": window.window_id,
                    "decision_kind": window.kind,
                    "held_action_withdrawn": held,
                    "held_action_choice": choice,
                },
            )
        choice = str(selected.get("choice") or action.parameters.get("choice") or "").strip()
        if window.options and not any(
            all(option.get(key) == value for key, value in selected.items())
            for option in window.options
        ):
            raise ValueError("所选内容不在这个待决窗口的合法选项中。")

        skill = str(window.payload.get("skill") or "")
        payload: dict[str, object] = {
            "decision_window_id": window.window_id,
            "decision_kind": window.kind,
            "skill_name": skill,
            "selected_option": selected,
        }
        rules_text = ""

        if choice == "decline":
            rules_text = f"{actor_name}暂不发动【{skill}】。"
        elif skill == "不屈意志":
            rules_text = self._resolve_unyielding_will(actor_name, choice, selected, payload)
        elif skill == "死战不退" and choice == "attribute":
            attribute = str(selected.get("attribute") or "WLP").upper()
            effect = TimedEffect(
                owner=actor_name,
                effect_type="attribute_buff",
                expires_on=EffectTiming.OWNER_TURN_END,
                target=actor_name,
                source=skill,
                effect_key=f"skill:{skill}:{actor_name}",
                data={
                    "attribute_bonus": {attribute: 1},
                    "expire_after_turn_serial": int(
                        getattr(self.conflict.state, "turn_serial", 0) or 0
                    )
                    + 1,
                },
                note="所选属性骰提升一级，持续到下个回合结束。",
            )
            self.conflict.register_effect(effect)
            payload["skill_effect"] = effect
            attribute_label = {
                "DEX": "敏捷",
                "INS": "洞察",
                "MIG": "力量",
                "WLP": "意志",
            }.get(attribute, attribute)
            rules_text = f"【死战不退】使{actor_name}的{attribute_label}骰提升一级。"
        elif skill == "鹰眼":
            rules_text = self._resolve_eagle_eye(actor_name, choice, selected, payload)
        elif skill == "黑暗之心" and choice == "hate_bond":
            target = str(selected.get("target") or "").strip()
            if not target or not self.characters.exists(target):
                raise ValueError("【黑暗之心】需要选择一个仍在场的生物。")
            actor = self.characters.get(actor_name)
            if actor.bond_strength_with(target) > 0:
                raise ValueError(f"{actor_name}已经与【{target}】建立羁绊。")
            actor.bonds.append(Bond(target=target, emotions=["憎恨"]))
            actor.trigger_cooldowns.add("scene:skill:黑暗之心")
            rules_text = f"【黑暗之心】使{actor_name}对【{target}】建立了憎恨羁绊。"
            payload["bond_target"] = target
        elif skill == "法术支援" and choice == "support":
            target = str(selected.get("target") or "").strip()
            modifier = int(selected.get("modifier", 0) or 0)
            effect = TimedEffect(
                owner=actor_name,
                effect_type="next_check_bonus",
                expires_on=EffectTiming.SCENE_END,
                target=target,
                source=skill,
                effect_key=f"skill:{skill}:{target}",
                data={"check_bonus": modifier},
                note="本场景下一次检定获得羁绊强度修正。",
            )
            self.conflict.register_effect(effect)
            payload["skill_effect"] = effect
            rules_text = f"【法术支援】将为【{target}】的下一次检定提供 +{modifier} 修正。"
        elif skill == "苦痛教训" and choice == "investigate":
            target = str(selected.get("target") or "").strip()
            investigation = resolve_investigation(
                Action(
                    ActionType.INVESTIGATE,
                    {
                        "actor": actor_name,
                        "target": target,
                        "attributes": ["INS", "INS"],
                        "target_number": 7,
                        "modifier": int(selected.get("modifier", 0) or 0),
                        "opportunity_action": True,
                    },
                )
            )
            payload["followup_resolution"] = investigation.payload
            rules_text = f"【苦痛教训】触发。{investigation.rules_text}"
        elif skill == "应急用品" and choice == "use_inventory_action":
            raise ValueError(
                "【应急用品】必须连同实际消耗物资行动一起提交，不能先关闭待决窗口。"
            )
        elif skill in {"疾速身法", "奥灵回响"}:
            raise ValueError(
                f"【{skill}】必须连同实际顺势行动一起提交，不能先关闭待决窗口。"
            )
        elif skill == "药剂雨" and choice == "select_targets":
            rules_text = self._resolve_potion_rain(actor_name, selected, window.payload, payload)
        else:
            raise ValueError(
                f"【{skill}】的选择【{choice}】尚未接入可提交的规则效果；窗口仍保持待决。"
            )

        resolved = self.decisions.resolve(
            window_id=window.window_id,
            responder=actor_name,
            resolution={"choice": choice, "selected_option": selected},
        )
        payload["decision_resolution"] = dict(resolved.resolution)
        self._attach_deferred_turn_resume(
            payload,
            window.payload,
            source_action_type=str(
                window.payload.get("source_action_type") or "skill"
            ),
        )
        return ActionResolution(action=action, rules_text=rules_text, payload=payload)

    def _attach_deferred_turn_resume(
        self,
        payload: dict[str, object],
        window_payload: dict[str, object],
        *,
        source_action_type: str,
    ) -> None:
        """Resume only the conflict action paused by this blocking choice."""

        try:
            deferred_serial = int(
                window_payload.get("deferred_turn_serial") or 0
            )
        except (TypeError, ValueError):
            deferred_serial = 0
        if (
            not self.conflict.state.active
            or deferred_serial <= 0
            or deferred_serial != int(self.conflict.state.turn_serial or 0)
            or self.decisions.has_blocking()
        ):
            return
        payload["resume_deferred_action"] = True
        payload["deferred_action_type"] = source_action_type
        payload["deferred_action_owner"] = str(
            window_payload.get("deferred_turn_actor") or ""
        )

    def _resolve_unyielding_will(
        self,
        actor_name: str,
        choice: str,
        selected: dict[str, object],
        payload: dict[str, object],
    ) -> str:
        if choice in {"recover_hp", "recover_mp"}:
            resource = "hp" if choice == "recover_hp" else "mp"
            before, after = self.characters.modify_resource(
                actor_name,
                resource,
                int(selected.get("amount", 0) or 0),
            )
            payload["resource_change"] = ResourceChange(
                actor_name,
                resource,
                after - before,
                before,
                after,
                "不屈意志。",
            )
            return f"【不屈意志】使{actor_name}恢复 {after - before} 点 {resource.upper()}。"
        if choice == "clear_status":
            status = StatusEffect(str(selected.get("status") or ""))
            removed = self.conflict.remove_status(actor_name, status)
            payload["status_removed"] = removed
            return f"【不屈意志】使{actor_name}解除{self._status_label(status)}。"
        return ""

    def _resolve_eagle_eye(
        self,
        actor_name: str,
        choice: str,
        selected: dict[str, object],
        payload: dict[str, object],
    ) -> str:
        if choice == "next_ranged_damage":
            effect = TimedEffect(
                owner=actor_name,
                effect_type="outgoing_ranged_damage_bonus",
                expires_on=EffectTiming.SCENE_END,
                target=actor_name,
                source="鹰眼",
                effect_key=f"skill:鹰眼:{actor_name}:damage",
                data={"damage_bonus": int(selected.get("amount", 0) or 0)},
                note="本场景下一次远程攻击获得额外伤害。",
            )
            self.conflict.register_effect(effect)
            payload["skill_effect"] = effect
            return f"【鹰眼】蓄势完成，{actor_name}的下一次远程攻击将造成额外伤害。"
        raise ValueError(
            "【鹰眼】的立即攻击必须连同目标与攻击行动一起提交，不能先关闭待决窗口。"
        )

    def _resolve_potion_rain(
        self,
        actor_name: str,
        selected: dict[str, object],
        window_payload: dict[str, object],
        payload: dict[str, object],
    ) -> str:
        context = dict(window_payload.get("trigger_context") or {})
        primary_target = str(context.get("primary_target") or "").strip()
        extra_targets = [str(name) for name in selected.get("targets", []) if str(name).strip()]
        max_targets = int(selected.get("max_extra_targets", 0) or 0)
        if not primary_target or not self.characters.exists(primary_target):
            raise ValueError("【药剂雨】原本的药剂目标已经不在场。")
        if not extra_targets or len(extra_targets) > max_targets:
            raise ValueError("【药剂雨】需要选择合法数量的额外目标。")
        changes: list[ResourceChange] = []
        for healing in context.get("healing_changes", []):
            if not isinstance(healing, dict):
                continue
            resource = str(healing.get("resource") or "")
            base_amount = int(healing.get("base_amount", 0) or 0)
            before = int(healing.get("before", 0) or 0)
            half_amount = base_amount // 2
            if resource not in {"hp", "mp"} or half_amount <= 0:
                continue
            primary = self.characters.get(primary_target)
            maximum = primary.max_hp if resource == "hp" else primary.max_mp
            desired = min(maximum, before + half_amount)
            current = primary.hp if resource == "hp" else primary.mp
            primary_before, primary_after = self.characters.modify_resource(
                primary_target,
                resource,
                desired - current,
            )
            changes.append(
                ResourceChange(
                    primary_target,
                    resource,
                    primary_after - primary_before,
                    primary_before,
                    primary_after,
                    "药剂雨将原药剂恢复量减半。",
                )
            )
            for target_name in extra_targets:
                if not self.characters.exists(target_name):
                    raise ValueError(f"【药剂雨】的额外目标【{target_name}】已经不在场。")
                extra_before, extra_after = self.characters.modify_resource(
                    target_name,
                    resource,
                    half_amount,
                )
                changes.append(
                    ResourceChange(
                        target_name,
                        resource,
                        extra_after - extra_before,
                        extra_before,
                        extra_after,
                        "药剂雨恢复。",
                    )
                )
        payload["resource_changes"] = changes
        return (
            f"【药剂雨】让药剂同时影响{primary_target}与{'、'.join(extra_targets)}，"
            "每名目标的恢复量减半。"
        )

    @classmethod
    def _status_label(cls, status: StatusEffect) -> str:
        return cls._STATUS_LABELS[status]
