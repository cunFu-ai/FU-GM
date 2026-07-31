from __future__ import annotations

import re

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.models import Action, ActionResolution, ActionType


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
        common = {
            "actor": actor_name,
            "reasoning": action.parameters.get("reasoning", ""),
            "in_mind_reply": action.parameters.get("in_mind_reply", ""),
        }

        if subaction == "Attack":
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
                    "infusion_name": action.parameters.get("infusion_name"),
                    "weapon_damage": action.parameters.get(
                        "weapon_damage",
                        self.characters.get(actor_name).weapon_damage,
                    ),
                    "is_melee": action.parameters.get(
                        "is_melee",
                        str(
                            self.characters.get(actor_name).weapon_range
                            or "melee"
                        ).lower()
                        not in {"ranged", "远程"},
                    ),
                    "reactions": action.parameters.get("reactions", []),
                },
            )
        if subaction == "Spell":
            spell_parameters = {
                **common,
                "target": action.parameters.get("target", actor_name),
                "spell_name": action.parameters.get("spell_name"),
                "attributes": action.parameters.get("attributes", ["INS", "WLP"]),
            }
            for key in (
                "targets",
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
