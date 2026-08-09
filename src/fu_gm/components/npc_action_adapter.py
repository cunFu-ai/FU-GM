from __future__ import annotations

import re

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    ResourceChange,
    StatusEffect,
)


class NPCActionAdapter:
    """Translate one validated NPC turn into an ordinary rule action.

    NPCs use the same attack, spell, guard and objective handlers as everyone
    else. This adapter only normalizes the compact NPC subaction schema and
    owns the few terminal conflict moves that have no player-action analogue.
    """

    _ALIASES = {
        "attack": "Attack",
        "攻击": "Attack",
        "spell": "Spell",
        "施法": "Spell",
        "guard": "Guard",
        "防御": "Guard",
        "hinder": "Hinder",
        "妨碍": "Hinder",
        "investigate": "Investigate",
        "调查": "Investigate",
        "objective": "Objective",
        "推进目标": "Objective",
        "skill": "Skill",
        "技能": "Skill",
        "otheraction": "OtherAction",
        "other_action": "OtherAction",
        "其他行动": "OtherAction",
        "ultimarecover": "UltimaRecover",
        "ultima_recover": "UltimaRecover",
        "narrate": "Narrate",
        "叙事": "Narrate",
        "escape": "Escape",
        "flee": "Escape",
        "撤离": "Escape",
        "逃跑": "Escape",
        "surrender": "Surrender",
        "投降": "Surrender",
        "放弃抵抗": "Surrender",
    }

    def __init__(self, characters: CharacterManager, conflict: ConflictManager) -> None:
        self.characters = characters
        self.conflict = conflict

    def translate(self, action: Action) -> Action | ActionResolution:
        actor_name = str(action.parameters.get("actor") or "").strip()
        if not actor_name or not self.characters.exists(actor_name):
            raise ValueError("NPC 行动必须指定当前在场的行动者。")
        subaction = self._normalize_subaction(self._infer_subaction(action))
        actor = self.characters.get(actor_name)
        if subaction not in {"Attack", "Guard"}:
            actor.npc_skill_effects.pop("previous_action_guarded", None)
        common = {
            "actor": actor_name,
            "reasoning": action.parameters.get("reasoning", ""),
            "in_mind_reply": action.parameters.get("in_mind_reply", ""),
        }

        if subaction == "Attack":
            preparation = str(
                action.parameters.get("consumes_combat_preparation") or ""
            ).strip()
            raw_preparations = action.parameters.get("consumes_combat_preparation")
            preparations = (
                [str(item).strip() for item in raw_preparations if str(item).strip()]
                if isinstance(raw_preparations, list)
                else [preparation] if preparation else []
            )
            for preparation_key in preparations:
                actor.npc_skill_effects.pop(preparation_key, None)
            # This preparation belongs only to the immediately following NPC
            # action.  Its damage has already been compiled into the validated
            # catalog entry, so consuming it here cannot change the roll.
            actor.npc_skill_effects.pop("previous_action_guarded", None)
            selected_accuracy = int(
                action.parameters.get(
                    "accuracy_modifier",
                    actor.weapon_accuracy_modifier,
                )
                or 0
            )
            return Action(
                ActionType.ATTACK,
                {
                    **common,
                    "target": action.parameters.get("target")
                    or self._first_name(action.parameters.get("targets")),
                    "targets": action.parameters.get("targets"),
                    "attributes": action.parameters.get("attributes", ["DEX", "MIG"]),
                    "damage_type": action.parameters.get(
                        "damage_type",
                        self.characters.effective_weapon_damage_type(actor_name),
                    ),
                    "non_damage": bool(action.parameters.get("non_damage"))
                    or str(action.parameters.get("damage_type") or "").strip()
                    in {"none", "无"},
                    "infusion_name": action.parameters.get("infusion_name"),
                    "weapon_damage": action.parameters.get(
                        "weapon_damage",
                        actor.weapon_damage,
                    ),
                    # The shared attack resolver adds the legacy primary
                    # accuracy field. Offset it so a selected secondary attack
                    # uses exactly its own authoritative modifier.
                    "modifier": selected_accuracy - actor.weapon_accuracy_modifier,
                    "is_melee": action.parameters.get(
                        "is_melee",
                        str(
                            actor.weapon_range
                            or "melee"
                        ).lower()
                        not in {"ranged", "远程"},
                    ),
                    "defense_type": (
                        "magic"
                        if action.parameters.get("targets_magic_defense")
                        else "physical"
                    ),
                    "multi_attack": int(
                        action.parameters.get("multi_attack", 1) or 1
                    ),
                    "status_effect_on_hit": action.parameters.get(
                        "status_effect_on_hit"
                    ),
                    "random_damage_types": action.parameters.get(
                        "random_damage_types", []
                    ),
                    "attack_id": action.parameters.get("attack_id"),
                    "attack_name": action.parameters.get("attack_name"),
                    "reactions": action.parameters.get("reactions", []),
                    "ignore_resist": bool(
                        action.parameters.get("ignore_resist")
                    ),
                    "conditional_damage_bonus": int(
                        action.parameters.get("conditional_damage_bonus", 0) or 0
                    ),
                    "conditional_target_statuses": list(
                        action.parameters.get("conditional_target_statuses", [])
                        or []
                    ),
                    "conditional_any_target_status": bool(
                        action.parameters.get("conditional_any_target_status")
                    ),
                    "recover_hp_fraction": float(
                        action.parameters.get("recover_hp_fraction", 0.0) or 0.0
                    ),
                    "recover_mp_on_hit": int(
                        action.parameters.get("recover_mp_on_hit", 0) or 0
                    ),
                    "target_mp_loss": int(
                        action.parameters.get("target_mp_loss", 0) or 0
                    ),
                    "target_ip_loss": int(
                        action.parameters.get("target_ip_loss", 0) or 0
                    ),
                    "self_hp_loss_if_all_miss": int(
                        action.parameters.get("self_hp_loss_if_all_miss", 0) or 0
                    ),
                    "npc_attack_effects": list(
                        action.parameters.get("npc_attack_effects", []) or []
                    ),
                },
            )
        if subaction == "Spell":
            spell_parameters = {
                **common,
                "target": action.parameters.get("target", actor_name),
                "spell_name": action.parameters.get("spell_name"),
                "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                "modifier": action.parameters.get("modifier", 0),
            }
            for key in (
                "targets",
                "npc_spell_name",
                "chosen_damage_type",
                "chosen_status",
                "chosen_statuses",
                "chosen_attribute",
                "attack_target",
            ):
                if action.parameters.get(key) not in (None, "", []):
                    spell_parameters[key] = action.parameters[key]
            return Action(ActionType.SPELL, spell_parameters)
        if subaction == "Guard":
            return Action(
                ActionType.GUARD,
                {
                    "actor": actor_name,
                    "guarded_target": action.parameters.get("guarded_target"),
                    "terrain": action.parameters.get("terrain"),
                    "in_mind_reply": action.parameters.get("in_mind_reply", ""),
                },
            )
        if subaction == "Hinder":
            return Action(
                ActionType.HINDER,
                {
                    **common,
                    "target": self._target_name(action, "当前威胁"),
                    "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
                    "status_effect": action.parameters.get("status_effect", "shaken"),
                    "target_number": action.parameters.get("target_number", 10),
                    "modifier": action.parameters.get("modifier", 0),
                },
            )
        if subaction == "Investigate":
            return Action(
                ActionType.INVESTIGATE,
                {
                    **common,
                    "target": self._target_name(action, "当前线索"),
                    "attributes": action.parameters.get("attributes", ["INS", "INS"]),
                    "modifier": action.parameters.get("modifier", 0),
                },
            )
        if subaction == "Objective":
            clock_name = (
                action.parameters.get("clock_name")
                or action.parameters.get("target")
                or "当前目标命刻"
            )
            return Action(
                ActionType.OBJECTIVE,
                {
                    **common,
                    "target": action.parameters.get("target", clock_name),
                    "attributes": action.parameters.get("attributes", ["DEX", "INS"]),
                    "clock_name": clock_name,
                    "clock_direction": action.parameters.get("clock_direction", 1),
                    "target_number": action.parameters.get("target_number", 10),
                    "modifier": action.parameters.get("modifier", 0),
                    "threat_clock_name": action.parameters.get("threat_clock_name"),
                },
            )
        if subaction == "Skill":
            return Action(ActionType.SKILL, {**action.parameters, "actor": actor_name})
        if subaction == "OtherAction":
            action_name = str(
                action.parameters.get("other_action_name") or ""
            ).strip()
            if action_name == "传递魔力":
                target_name = str(action.parameters.get("target") or "").strip()
                amount = int(action.parameters.get("mp_amount") or 0)
                before_actor, after_actor = self.characters.modify_resource(
                    actor_name,
                    "mp",
                    -amount,
                )
                before_target, after_target = self.characters.modify_resource(
                    target_name,
                    "mp",
                    amount,
                )
                return ActionResolution(
                    action=action,
                    rules_text=(
                        f"{actor_name}消耗{before_actor - after_actor}点精神值，"
                        f"{target_name}恢复{after_target - before_target}点精神值。"
                    ),
                    payload={
                        "resource_changes": [
                            ResourceChange(
                                actor_name,
                                "mp",
                                after_actor - before_actor,
                                before_actor,
                                after_actor,
                                "传递魔力消耗。",
                            ),
                            ResourceChange(
                                target_name,
                                "mp",
                                after_target - before_target,
                                before_target,
                                after_target,
                                "传递魔力恢复。",
                            ),
                        ],
                        "npc_other_action": action_name,
                    },
                )
            if action_name == "愤怒鼻息":
                actor.npc_skill_effects["forced_next_attack"] = "巨岩暴冲"
                return ActionResolution(
                    action=action,
                    rules_text=f"{actor_name}蓄势；下个回合必须发动巨岩暴冲。",
                    payload={"npc_other_action": action_name},
                )
            if action_name == "攻击蓄力":
                actor.npc_skill_effects["charged_attack"] = True
                return ActionResolution(
                    action=action,
                    rules_text=(
                        f"{actor_name}完成蓄力；下一次攻击获得多重攻击(2)"
                        "并无视抵抗相性。"
                    ),
                    payload={"npc_other_action": action_name},
                )
            if action_name == "仙人掌汁液":
                cleared = []
                for status in (StatusEffect.SLOW, StatusEffect.WEAKENED):
                    if self.conflict.remove_status(actor_name, status):
                        cleared.append(status.value)
                return Action(
                    ActionType.ATTACK,
                    {
                        **common,
                        "target": action.parameters.get("target"),
                        "attributes": action.parameters.get("attributes"),
                        "damage_type": action.parameters.get("damage_type"),
                        "weapon_damage": action.parameters.get("weapon_damage"),
                        "modifier": int(
                            action.parameters.get("accuracy_modifier", 0) or 0
                        )
                        - actor.weapon_accuracy_modifier,
                        "is_melee": action.parameters.get("is_melee", False),
                        "defense_type": (
                            "magic"
                            if action.parameters.get("targets_magic_defense")
                            else "physical"
                        ),
                        "multi_attack": int(
                            action.parameters.get("multi_attack", 1) or 1
                        ),
                        "attack_id": action.parameters.get("attack_id"),
                        "attack_name": action.parameters.get("attack_name"),
                        "npc_pre_action_statuses_cleared": cleared,
                    },
                )
            ability_id = str(action.parameters.get("ability_id") or "").strip()
            ability = next(
                (
                    profile
                    for profile in actor.npc_ability_profiles
                    if profile.ability_id == ability_id
                    or (not ability_id and profile.name == action_name)
                ),
                None,
            )
            if ability is not None and ability.trigger == "skill_action":
                details = {
                    "npc_other_action": action_name,
                    "ability_id": ability.ability_id,
                    "effect_type": ability.effect_type,
                }
                if ability.effect_type == "prepare_attack_damage":
                    actor.npc_skill_effects["prepared_attack_damage"] = {
                        "amount": ability.amount,
                        "source": ability.name,
                    }
                    rules_text = (
                        f"{actor_name}准备完成；下一次攻击造成"
                        f"{ability.amount}点额外伤害。"
                    )
                elif ability.effect_type == "affinity_change":
                    actor.temporary_affinities.update(ability.affinity_changes)
                    details["affinity_changes"] = {
                        key: value.value
                        for key, value in ability.affinity_changes.items()
                    }
                    rules_text = f"{actor_name}改变姿态，伤害相性已经更新。"
                elif ability.effect_type == "recover_mp":
                    before, after = self.characters.modify_resource(
                        actor_name,
                        "mp",
                        ability.amount,
                    )
                    details["mp_before"] = before
                    details["mp_after"] = after
                    rules_text = f"{actor_name}恢复{after - before}点精神值。"
                elif ability.effect_type == "status_apply":
                    target_name = (
                        actor_name
                        if ability.target_scope == "self"
                        else str(action.parameters.get("target") or "").strip()
                    )
                    applied = [
                        status.value
                        for status in ability.statuses
                        if self.characters.add_status(target_name, status)
                    ]
                    details["target"] = target_name
                    details["applied_statuses"] = applied
                    rules_text = f"{target_name}受到【{ability.name}】的异常状态效果。"
                else:
                    raise ValueError(
                        f"特殊行动【{ability.name}】的效果尚不能主动结算。"
                    )
                return ActionResolution(
                    action=action,
                    rules_text=rules_text,
                    payload=details,
                )
            raise ValueError(f"其他行动【{action_name}】尚无可执行规则。")
        if subaction == "UltimaRecover":
            event = self.conflict.spend_ultima_to_recover(actor_name)
            return ActionResolution(
                action=action,
                rules_text=event.summary,
                payload={"conflict_event": event},
            )
        if subaction == "Narrate":
            return ActionResolution(
                action=action,
                rules_text=str(
                    action.parameters.get("summary")
                    or f"{actor_name} 暂时没有执行明确动作。"
                ),
                payload={},
            )
        if subaction == "Escape":
            self.conflict.remove_combatant_from_scene(actor_name, as_escaped=True)
            return ActionResolution(
                action=action,
                rules_text=str(
                    action.parameters.get("summary")
                    or f"{actor_name} 撤离了当前冲突场景。"
                ),
                payload={"npc_escaped": True, "actor": actor_name},
            )
        if subaction == "Surrender":
            self.conflict.surrender_combatant(actor_name)
            return ActionResolution(
                action=action,
                rules_text=str(
                    action.parameters.get("summary")
                    or f"{actor_name} 放弃抵抗，退出了当前轮转。"
                ),
                payload={"npc_surrendered": True, "actor": actor_name},
            )
        return ActionResolution(
            action=action,
            rules_text=(
                f"{actor_name} 的意图还需要 GM 重新描述成本轮可结算的行动；"
                "本次没有执行硬数值结算。"
            ),
            payload={
                "npc_action_unresolved": True,
                "actor": actor_name,
                "requested_subaction": subaction,
            },
        )

    @classmethod
    def _infer_subaction(cls, action: Action) -> str:
        explicit = (
            action.parameters.get("npc_action_type")
            or action.parameters.get("subaction")
            or action.parameters.get("action")
        )
        if explicit:
            return str(explicit)
        if action.parameters.get("clock_name"):
            return "Objective"
        if action.parameters.get("spell_name"):
            return "Spell"
        if action.parameters.get("status_effect"):
            return "Hinder"
        if action.parameters.get("guarded_target"):
            return "Guard"
        if action.parameters.get("target") or action.parameters.get("targets"):
            return "Attack"
        return "Narrate"

    @classmethod
    def _normalize_subaction(cls, value: str) -> str:
        raw = str(value or "").strip()
        return cls._ALIASES.get(raw.lower(), cls._ALIASES.get(raw, raw))

    @classmethod
    def _target_name(cls, action: Action, fallback: str) -> str:
        value = action.parameters.get("target") or cls._first_name(
            action.parameters.get("targets")
        )
        return str(value or fallback).strip()

    @staticmethod
    def _first_name(value: object) -> str:
        if isinstance(value, str):
            return next(
                (part.strip() for part in re.split(r"[、,，/]+", value) if part.strip()),
                "",
            )
        if isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, dict):
                for key in ("name", "target", "id"):
                    text = str(first.get(key) or "").strip()
                    if text:
                        return text
                return ""
            return str(first or "").strip()
        return ""
