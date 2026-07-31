from __future__ import annotations

import re

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Action,
    ActionType,
    DecisionWindow,
    EnemyRank,
    GamePanel,
    SpellEffectType,
    SpellTarget,
)
from fu_gm.skill_library import (
    SKILL_COVERAGE_HARD_RULE,
    get_skill_reference,
    normalize_skill_reference_name,
    skill_implementation_coverage,
    skill_rank,
)
from fu_gm.spellbook import (
    get_spell_definition,
    is_known_spell,
    normalize_spell_name,
)


_ACTIVE_TURN_SKILLS = {
    "契约与召唤",
    "暗影击",
    "摧心重击",
    "挑衅",
    "谴责",
    "鼓舞",
    "窃取时间",
    "窃取灵魂",
    "回见了您呐",
    "碎骨",
    "威慑射击",
    "破防打击",
    "快速评估",
    "意外盟友",
    "缴械雄辩",
    "不出所料！",
    "影逝",
    "重燃希望",
    "火山",
    "彗星",
    "弹幕射击",
    "利刃风暴",
}


def _decision_action(
    *,
    panel: GamePanel,
    actor_name: str,
    window: DecisionWindow,
    option_index: int,
    details: dict[str, object] | None = None,
) -> Action:
    """Translate a deterministic NPC window choice into a rule action."""

    if window.owner != actor_name:
        raise ValueError(
            f"待决窗口属于【{window.owner}】，不能由【{actor_name}】处理。"
        )
    if option_index < 0 or option_index >= len(window.options):
        raise ValueError("NPC选择的待决选项不在合法范围内。")

    selected = dict(window.options[option_index])
    supplied = dict(details or {})
    if window.kind in {
        "critical_opportunity",
        "fumble_opportunity",
        "opportunity_parameter",
    }:
        effect = str(
            selected.get("effect")
            or supplied.get("effect")
            or window.payload.get("effect")
            or ""
        ).strip()
        if not effect:
            raise ValueError("机会窗口缺少具体效果。")
        parameters: dict[str, object] = {
            "actor": actor_name,
            "window_id": window.window_id,
            "effect": effect,
            "opportunity": effect,
            **supplied,
        }
        if effect == "揭示":
            target = str(parameters.get("target") or "").strip()
            if not target:
                raise ValueError("机会【揭示】必须选择一个生物。")
            parameters["target_explicit"] = True
        elif effect == "进展":
            clock_name = str(parameters.get("clock_name") or "").strip(
                " ：:「」『』【】[]"
            )
            if not clock_name or not any(
                clock_name in str(raw) for raw in panel.active_clocks
            ):
                raise ValueError("机会【进展】必须选择当前场景中的命刻。")
            parameters["clock_name"] = clock_name
        elif effect == "纽带":
            if not str(parameters.get("target") or "").strip():
                raise ValueError("机会【纽带】必须选择羁绊对象。")
            parameters.setdefault("emotion", "信赖")
        elif effect == "优势":
            parameters.setdefault("target", actor_name)
        return Action(ActionType.TRIGGER_OPPORTUNITY, parameters)

    if window.kind == "zero_hp":
        raise ValueError("生命值归零选择不能由GM代替玩家处理。")
    if window.kind == "acceleration_benefit":
        selected.update(supplied)
        choice = str(selected.get("choice") or "").strip()
        if choice == "decline":
            return Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": actor_name,
                    "window_id": window.window_id,
                    "choice": "decline",
                    "selected_option": {"choice": "decline"},
                },
            )
        common = {
            "actor": actor_name,
            "_acceleration_window_id": window.window_id,
            "opportunity_action": True,
            "_enforce_turn_order": False,
        }
        if choice == "attack":
            target = str(selected.get("target") or "").strip()
            if not target:
                raise ValueError("NPC使用【加速术】顺势攻击时必须选择目标。")
            return Action(ActionType.ATTACK, {**common, "target": target})
        if choice == "cast_spell":
            spell_name = str(selected.get("spell_name") or "").strip()
            if not spell_name:
                raise ValueError("NPC使用【加速术】顺势施法时必须选择法术。")
            parameters = {**common, "spell_name": spell_name}
            target = str(selected.get("target") or "").strip()
            if target:
                parameters["target"] = target
                parameters["target_explicit"] = True
            return Action(ActionType.SPELL, parameters)
        raise ValueError("NPC的【加速术】选择不在合法选项中。")

    selected.update(supplied)
    return Action(
        ActionType.RESOLVE_DECISION,
        {
            "actor": actor_name,
            "window_id": window.window_id,
            "choice": str(selected.get("choice") or ""),
            "selected_option": selected,
        },
    )


class NPCCombatRules:
    """Expose legal NPC combat options and validate the core GM's choice."""

    _CLOCK_PATTERN = re.compile(
        r"\[(?P<name>.+?)\]\s+(?P<current>\d+)\s*/\s*(?P<max>\d+)"
    )

    def __init__(
        self,
        character_manager: CharacterManager,
        conflict_manager: ConflictManager,
        world_state: WorldState,
    ) -> None:
        self.character_manager = character_manager
        self.conflict_manager = conflict_manager
        self.world_state = world_state

    def build_tactical_snapshot(
        self,
        panel: GamePanel,
        actor_name: str,
    ) -> dict[str, object]:
        actor = self.character_manager.get(actor_name)
        active_combatants = self._active_combatants()
        actor_side = self.conflict_manager.combat_side(actor_name)
        opponent_targets = [
            self.character_manager.get(name)
            for name in active_combatants
            if self.character_manager.exists(name)
            for character in [self.character_manager.get(name)]
            if (
                name != actor_name
                and self.conflict_manager.combat_side(name) != actor_side
                and character.hp > 0
            )
        ]
        preferred_target = self._pick_target(opponent_targets)
        preferred_clock = self._pick_preferred_clock(panel.active_clocks)
        stage = self.conflict_manager.current_stage(actor_name)
        return {
            "actor": actor_name,
            "actor_in_crisis": actor.in_crisis,
            "actor_statuses": [status.value for status in actor.statuses],
            "ultima_points": self.conflict_manager.state.ultima_points.get(
                actor_name, 0
            ),
            "current_stage": stage.name if stage is not None else "",
            "stage_public_cue": stage.public_cue if stage is not None else "",
            "stage_preferred_actions": (
                list(stage.preferred_actions) if stage is not None else []
            ),
            "stage_tactic_hints": (
                list(stage.tactic_hints) if stage is not None else []
            ),
            "stage_affinity_changes": (
                {
                    damage_type: affinity.value
                    for damage_type, affinity in stage.affinity_changes.items()
                }
                if stage is not None
                else {}
            ),
            "stage_hint_policy": "soft_suggestions_not_forced",
            "can_escalate": self.conflict_manager.can_escalate(actor_name),
            "is_exalted": (
                actor_name in self.conflict_manager.state.exalted_enemies
            ),
            "action_count_per_round": (
                self.conflict_manager.state.enemy_action_counts.get(
                    actor_name, 1
                )
            ),
            "preferred_target": (
                preferred_target.name if preferred_target is not None else ""
            ),
            "preferred_clock": (
                preferred_clock["name"] if preferred_clock is not None else ""
            ),
            "preferred_clock_progress": (
                f"{preferred_clock['current']}/{preferred_clock['max_segments']}"
                if preferred_clock is not None
                else ""
            ),
            "crisis_targets": [
                character.name
                for character in opponent_targets
                if character.in_crisis
            ],
            "actor_rules_profile": {
                "attributes": dict(actor.attributes),
                "weapon": {
                    "name": actor.equipped_main_hand,
                    "accuracy_attributes": list(
                        actor.weapon_accuracy_attributes
                    ),
                    "accuracy_modifier": actor.weapon_accuracy_modifier,
                    "damage_bonus": actor.weapon_damage,
                    "damage_type": actor.weapon_type,
                    "range": actor.weapon_range,
                },
                "skills": dict(actor.skills),
                "hero_skills": list(actor.hero_skills),
                "spells": list(actor.spells),
                "abilities": list(actor.abilities),
                "equipment": list(actor.equipment),
            },
            "legal_actions": self.build_legal_action_catalog(panel, actor_name),
        }

    def build_legal_action_catalog(
        self,
        panel: GamePanel,
        actor_name: str,
    ) -> list[dict[str, object]]:
        actor = self.character_manager.get(actor_name)
        active_combatants = self._active_combatants()
        actor_side = self.conflict_manager.combat_side(actor_name)
        opponent_targets = [
            name
            for name in active_combatants
            if self.character_manager.exists(name)
            for character in [self.character_manager.get(name)]
            if (
                name != actor_name
                and self.conflict_manager.combat_side(name) != actor_side
                and character.hp > 0
            )
        ]
        ally_targets = [
            name
            for name in active_combatants
            if self.character_manager.exists(name)
            for character in [self.character_manager.get(name)]
            if (
                name != actor_name
                and self.conflict_manager.combat_side(name) == actor_side
                and character.hp > 0
            )
        ]
        actions: list[dict[str, object]] = []
        if opponent_targets:
            actions.extend(
                [
                    {
                        "npc_action_type": "Attack",
                        "targets": opponent_targets,
                        "attributes": list(
                            actor.weapon_accuracy_attributes
                        ),
                        "damage_type": actor.weapon_type,
                        "range": actor.weapon_range,
                        "is_melee": (
                            str(actor.weapon_range or "melee").lower()
                            not in {"ranged", "远程"}
                        ),
                    },
                    {
                        "npc_action_type": "Hinder",
                        "targets": opponent_targets,
                        "attributes": self._objective_attributes(actor),
                        "modifier": self._specialty_bonus(actor, "妨碍检定"),
                        "status_options": [
                            "slow",
                            "dazed",
                            "weakened",
                            "shaken",
                        ],
                    },
                    {
                        "npc_action_type": "Investigate",
                        "targets": opponent_targets,
                        "attributes": ["INS", "INS"],
                        "modifier": self._specialty_bonus(actor, "调查检定"),
                    },
                ]
            )
        actions.append(
            {
                "npc_action_type": "Guard",
                "guarded_targets": [
                    name for name in ally_targets if name != actor_name
                ],
            }
        )
        for raw in panel.active_clocks:
            match = self._CLOCK_PATTERN.match(str(raw))
            if not match:
                continue
            clock_name = match.group("name")
            clock_type = self._clock_type_from_panel_text(str(raw))
            actions.append(
                {
                    "npc_action_type": "Objective",
                    "clock_name": clock_name,
                    "clock_direction": (
                        -1 if clock_type in {"objective", "ritual"} else 1
                    ),
                    "attributes": self._objective_attributes(actor),
                    "target_number": 10,
                    "modifier": self._specialty_bonus(actor, "推进目标检定"),
                }
            )
        for raw_spell in actor.spells:
            canonical = normalize_spell_name(raw_spell)
            if not is_known_spell(canonical):
                continue
            definition = get_spell_definition(canonical)
            if actor.level < int(definition.minimum_level or 0):
                continue
            rank = self.conflict_manager.state.enemy_ranks.get(
                actor_name,
                EnemyRank.SOLDIER,
            )
            if (
                definition.allowed_npc_ranks
                and rank.value not in definition.allowed_npc_ranks
            ):
                continue
            if (
                definition.npc_last_turn_only
                and not self._is_last_rank_turn(actor_name)
            ):
                continue
            target_options = self._spell_target_options(
                definition.target.value,
                definition.effect_type.value,
                actor_name,
                opponent_targets,
                ally_targets,
            )
            rule_max_targets = self._spell_max_targets(
                definition.target,
                len(target_options),
            )
            affordable_max_targets = self._max_affordable_spell_targets(
                actor,
                definition,
                rule_max_targets,
            )
            required_target_count = (
                len(target_options)
                if definition.target == SpellTarget.ALL_ENEMIES
                else 1
            )
            if (
                not target_options
                or affordable_max_targets < required_target_count
            ):
                continue
            max_targets = (
                required_target_count
                if definition.target == SpellTarget.ALL_ENEMIES
                else min(rule_max_targets, affordable_max_targets)
            )
            minimum_cost = self._spell_cost(
                definition,
                required_target_count,
            )
            actions.append(
                {
                    "npc_action_type": "Spell",
                    "spell_name": definition.name,
                    "mp_cost": definition.mp_cost,
                    "mp_cost_per_target": bool(
                        definition.mp_cost_per_target
                    ),
                    "minimum_total_mp_cost": minimum_cost,
                    "target_kind": definition.target.value,
                    "target_options": target_options,
                    "attributes": list(
                        actor.npc_spell_attributes.get(
                            definition.name,
                            definition.attributes,
                        )
                    ),
                    "check_modifier": actor.npc_spell_check_bonus,
                    "effect_type": definition.effect_type.value,
                    "damage_type": definition.damage_type,
                    "selectable_damage_types": list(
                        definition.selectable_damage_types
                    ),
                    "selectable_statuses": [
                        status.value
                        for status in definition.selectable_statuses
                    ],
                    "selectable_status_count": int(
                        definition.selectable_status_count or 1
                    ),
                    "max_targets": max_targets,
                    "attack_target_options": (
                        opponent_targets
                        if definition.effect_type
                        == SpellEffectType.IMMEDIATE_ATTACK
                        else []
                    ),
                    "description": definition.description,
                    "payment": (
                        "mp"
                        if actor.mp >= minimum_cost
                        else "生命秘法"
                    ),
                }
            )
        owned_skills: dict[str, int] = {}
        for raw_name, rank in actor.skills.items():
            canonical = normalize_skill_reference_name(raw_name)
            owned_skills[canonical] = max(
                owned_skills.get(canonical, 0), int(rank)
            )
        for raw_name in actor.hero_skills:
            canonical = normalize_skill_reference_name(raw_name)
            owned_skills[canonical] = max(
                owned_skills.get(canonical, 0), 1
            )
        for skill_name, rank in owned_skills.items():
            coverage = skill_implementation_coverage(skill_name)
            reference = get_skill_reference(skill_name)
            if (
                skill_name not in _ACTIVE_TURN_SKILLS
                or coverage is None
                or coverage.category != SKILL_COVERAGE_HARD_RULE
                or reference is None
            ):
                continue
            actions.append(
                {
                    "npc_action_type": "Skill",
                    "skill_name": skill_name,
                    "skill_rank": rank,
                    "targets": [*opponent_targets, *ally_targets],
                    "summary": reference.summary,
                }
            )
        if self.conflict_manager.state.ultima_points.get(actor_name, 0) > 0:
            actions.append({"npc_action_type": "UltimaRecover"})
        actions.extend(
            [
                {"npc_action_type": "Escape"},
                {"npc_action_type": "Surrender"},
            ]
        )
        return actions

    def validate_action(
        self,
        panel: GamePanel,
        actor_name: str,
        parameters: dict[str, object],
    ) -> Action:
        if not actor_name or not self.character_manager.exists(actor_name):
            raise ValueError("当前行动者不存在")
        submitted = dict(parameters)
        action_type = str(
            submitted.get("npc_action_type") or ""
        ).strip()
        legal_catalog = self.build_legal_action_catalog(panel, actor_name)
        candidates = [
            entry
            for entry in legal_catalog
            if entry.get("npc_action_type") == action_type
        ]
        if not candidates:
            raise ValueError(f"动作 {action_type or '空'} 不在合法行动清单")
        action_description = str(
            submitted.get("action_description") or ""
        ).strip()
        if not action_description:
            raise ValueError("NPC行动必须附带核心GM写好的公开行动描述")
        common: dict[str, object] = {
            "actor": actor_name,
            "npc_action_type": action_type,
            "reasoning": str(submitted.get("reasoning") or "").strip(),
            "in_mind_reply": action_description,
        }

        if action_type == "Spell":
            spell_name = normalize_spell_name(
                str(submitted.get("spell_name") or "")
            )
            entry = next(
                (
                    item
                    for item in candidates
                    if item.get("spell_name") == spell_name
                ),
                None,
            )
            if entry is None:
                raise ValueError(f"未拥有或当前无法施放法术【{spell_name}】")
            definition = get_spell_definition(spell_name)
            legal_targets = [
                str(value)
                for value in entry.get("target_options", [])
            ]
            submitted_targets = self._submitted_targets(submitted)
            if definition.target == SpellTarget.SELF:
                submitted_targets = [actor_name]
            elif definition.target == SpellTarget.ALL_ENEMIES:
                submitted_targets = list(legal_targets)
            if not submitted_targets:
                target = str(submitted.get("target") or actor_name).strip()
                submitted_targets = [target] if target else []
            max_targets = int(entry.get("max_targets", 1) or 1)
            if (
                not submitted_targets
                or (max_targets > 0 and len(submitted_targets) > max_targets)
                or any(target not in legal_targets for target in submitted_targets)
            ):
                raise ValueError(
                    f"法术【{spell_name}】的目标不在当前合法选项中"
                )
            total_mp_cost = self._spell_cost(
                definition,
                len(submitted_targets),
            )
            actor = self.character_manager.get(actor_name)
            can_use_life = (
                skill_rank(actor.skills, "生命秘法") > 0
                and actor.hp > total_mp_cost + 10
            )
            if actor.mp < total_mp_cost and not can_use_life:
                raise ValueError(
                    f"法术【{spell_name}】选择"
                    f"{len(submitted_targets)}个目标时需要"
                    f"{total_mp_cost}点精神值，当前资源不足"
                )

            selectable_damage_types = {
                str(value)
                for value in entry.get("selectable_damage_types", [])
            }
            chosen_damage_type = str(
                submitted.get("chosen_damage_type") or ""
            ).strip()
            if (
                selectable_damage_types
                and chosen_damage_type not in selectable_damage_types
            ):
                raise ValueError(
                    f"法术【{spell_name}】需要选择合法的伤害类型"
                )

            selectable_statuses = {
                str(value)
                for value in entry.get("selectable_statuses", [])
            }
            required_status_count = int(
                entry.get("selectable_status_count", 1) or 1
            )
            chosen_statuses = self._submitted_statuses(submitted)
            if selectable_statuses and (
                len(chosen_statuses) != required_status_count
                or any(
                    status not in selectable_statuses
                    for status in chosen_statuses
                )
            ):
                raise ValueError(
                    f"法术【{spell_name}】需要选择"
                    f"{required_status_count}种不同且合法的异常状态"
                )

            common.update(
                {
                    "spell_name": spell_name,
                    "target": submitted_targets[0],
                    "attributes": list(entry.get("attributes", [])),
                }
            )
            if len(submitted_targets) > 1 or definition.target in {
                SpellTarget.UP_TO_THREE_CREATURES,
                SpellTarget.ANY_VISIBLE_CREATURES,
                SpellTarget.ALL_ENEMIES,
            }:
                common["targets"] = submitted_targets
            if chosen_damage_type:
                common["chosen_damage_type"] = chosen_damage_type
            if chosen_statuses:
                common["chosen_status"] = chosen_statuses[0]
                if len(chosen_statuses) > 1:
                    common["chosen_statuses"] = chosen_statuses
            if definition.effect_type == SpellEffectType.IMMEDIATE_ATTACK:
                attack_target = str(
                    submitted.get("attack_target") or ""
                ).strip()
                legal_attack_targets = {
                    str(value)
                    for value in entry.get(
                        "attack_target_options",
                        [],
                    )
                }
                if attack_target not in legal_attack_targets:
                    raise ValueError(
                        f"法术【{spell_name}】需要选择合法的顺势攻击目标"
                    )
                common["attack_target"] = attack_target
        elif action_type == "Skill":
            skill_name = normalize_skill_reference_name(
                str(submitted.get("skill_name") or "")
            )
            entry = next(
                (
                    item
                    for item in candidates
                    if item.get("skill_name") == skill_name
                ),
                None,
            )
            if entry is None:
                raise ValueError(f"未拥有或不能主动结算技能【{skill_name}】")
            common["skill_name"] = skill_name
            target = str(submitted.get("target") or "").strip()
            valid_targets = [str(value) for value in entry.get("targets", [])]
            if target:
                if valid_targets and target not in valid_targets:
                    raise ValueError(f"目标【{target}】不在当前可选目标中")
                common["target"] = target
        elif action_type in {"Attack", "Hinder", "Investigate"}:
            entry = candidates[0]
            valid_targets = [str(value) for value in entry.get("targets", [])]
            target = str(submitted.get("target") or "").strip()
            if target not in valid_targets:
                raise ValueError(f"目标【{target}】不在当前可选目标中")
            common["target"] = target
            if action_type == "Attack":
                common["attributes"] = list(entry.get("attributes", []))
                common["damage_type"] = str(
                    entry.get("damage_type") or "physical"
                )
                common["is_melee"] = bool(entry.get("is_melee", True))
            elif action_type == "Hinder":
                status = str(
                    submitted.get("status_effect") or ""
                ).strip()
                if status not in entry.get("status_options", []):
                    raise ValueError(f"妨碍状态【{status}】不合法")
                common.update(
                    {
                        "status_effect": status,
                        "attributes": list(entry.get("attributes", [])),
                        "target_number": 10,
                        "modifier": int(entry.get("modifier", 0)),
                    }
                )
            else:
                common["attributes"] = ["INS", "INS"]
                common["modifier"] = int(entry.get("modifier", 0))
        elif action_type == "Guard":
            guarded_target = str(
                submitted.get("guarded_target") or ""
            ).strip()
            valid_targets = [
                str(value)
                for value in candidates[0].get("guarded_targets", [])
            ]
            if guarded_target and guarded_target not in valid_targets:
                raise ValueError(f"不能掩护【{guarded_target}】")
            if guarded_target:
                common["guarded_target"] = guarded_target
        elif action_type == "Objective":
            clock_name = str(
                submitted.get("clock_name")
                or submitted.get("target")
                or ""
            ).strip(" ：:「」『』【】[]")
            entry = next(
                (
                    item
                    for item in candidates
                    if item.get("clock_name") == clock_name
                ),
                None,
            )
            if entry is None:
                raise ValueError(f"命刻【{clock_name}】不在当前场景")
            try:
                target_number = int(
                    submitted.get(
                        "target_number",
                        entry.get("target_number", 10),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("推进目标的难度等级必须是整数。") from exc
            if target_number < 7:
                raise ValueError("推进目标的难度等级至少为 7。")
            common.update(
                {
                    "clock_name": clock_name,
                    "target": clock_name,
                    "attributes": list(entry.get("attributes", [])),
                    "target_number": target_number,
                    "clock_direction": int(
                        entry.get("clock_direction", 1)
                    ),
                    "modifier": int(entry.get("modifier", 0)),
                }
            )
        elif action_type == "UltimaRecover":
            if (
                self.conflict_manager.state.ultima_points.get(actor_name, 0)
                <= 0
            ):
                raise ValueError("没有可用终结点")

        return Action(ActionType.NPCACT, common)

    @staticmethod
    def _specialty_bonus(actor, specialty: str) -> int:
        return int(actor.npc_specialty_bonuses.get(specialty, 0))

    def resolve_window(
        self,
        panel: GamePanel,
        actor_name: str,
        window: DecisionWindow,
    ) -> Action:
        """Choose the least world-altering legal NPC-only follow-up."""

        if not window.options:
            raise ValueError(
                f"NPC待决窗口【{window.kind}】没有可执行的合法选项。"
            )
        option_index = 0
        details: dict[str, object] = {}
        if window.kind == "critical_opportunity":
            option_index = next(
                (
                    index
                    for index, option in enumerate(window.options)
                    if str(option.get("effect") or "") == "优势"
                ),
                0,
            )
            details["target"] = actor_name
        else:
            option_index = next(
                (
                    index
                    for index, option in enumerate(window.options)
                    if str(option.get("choice") or "") == "decline"
                ),
                0,
            )
        return _decision_action(
            panel=panel,
            actor_name=actor_name,
            window=window,
            option_index=option_index,
            details=details,
        )

    @staticmethod
    def _pick_target(characters: list[object]) -> object | None:
        if not characters:
            return None
        return sorted(characters, key=lambda character: character.hp)[0]

    def _active_combatants(self) -> list[str]:
        """Return only actors that still belong to the current conflict.

        ``CharacterManager`` is campaign-wide. Reading every character here
        allowed an NPC to attack a living PC from another scene or guard an
        off-screen villain. The conflict turn order is the authoritative set
        of active, non-defeated combatants; queued turns do not introduce new
        participants and therefore need no separate target source.
        """

        return list(dict.fromkeys(self.conflict_manager.state.turn_order))

    def _pick_preferred_clock(
        self,
        active_clocks: list[str],
    ) -> dict[str, object] | None:
        parsed: list[dict[str, object]] = []
        for raw in active_clocks:
            match = self._CLOCK_PATTERN.match(str(raw))
            if not match:
                continue
            parsed.append(
                {
                    "name": match.group("name"),
                    "current": int(match.group("current")),
                    "max_segments": int(match.group("max")),
                    "type": self._clock_type_from_panel_text(str(raw)),
                }
            )
        incomplete = [
            clock
            for clock in parsed
            if int(clock["current"]) < int(clock["max_segments"])
        ]
        if not incomplete:
            return None
        return max(
            incomplete,
            key=lambda clock: (
                1
                if clock.get("type")
                in {"threat", "villain", "dungeon", "boss"}
                else 0,
                int(clock["current"]) / int(clock["max_segments"]),
            ),
        )

    @staticmethod
    def _clock_type_from_panel_text(raw: str) -> str:
        for label, clock_type in (
            ("威胁命刻", "threat"),
            ("反派命刻", "villain"),
            ("地下城危机命刻", "dungeon"),
            ("首领机制命刻", "boss"),
            ("仪式命刻", "ritual"),
            ("目标命刻", "objective"),
        ):
            if label in raw:
                return clock_type
        return ""

    @staticmethod
    def _objective_attributes(actor: object) -> list[str]:
        if actor.attributes.get("INS", 6) >= actor.attributes.get("DEX", 6):
            return ["INS", "WLP"]
        return ["DEX", "INS"]

    @staticmethod
    def _spell_target_options(
        target_kind: str,
        effect_type: str,
        actor_name: str,
        player_targets: list[str],
        ally_targets: list[str],
    ) -> list[str]:
        if target_kind == "self":
            return [actor_name]
        if target_kind == "one_ally":
            return ally_targets
        if target_kind == "one_enemy":
            return player_targets
        if target_kind == "all_enemies":
            return player_targets
        if target_kind == "any_visible_creatures":
            return list(
                dict.fromkeys(
                    [*player_targets, *ally_targets]
                )
            )
        if effect_type in {"damage", "mp_damage", "status_apply"}:
            return player_targets
        return ally_targets

    def _is_last_rank_turn(self, actor_name: str) -> bool:
        action_count = int(
            self.conflict_manager.state.enemy_action_counts.get(
                actor_name,
                1,
            )
            or 1
        )
        if action_count <= 1:
            return True
        if self.conflict_manager.state.current_bonus_actor != actor_name:
            return False
        return not any(
            queued_actor == actor_name and queued_kind == "rank"
            for queued_actor, queued_kind in zip(
                self.conflict_manager.state.queued_turns,
                self.conflict_manager.state.queued_turn_kinds,
            )
        )

    @staticmethod
    def _spell_max_targets(
        target_kind: SpellTarget,
        candidate_count: int,
    ) -> int:
        if target_kind == SpellTarget.UP_TO_THREE_CREATURES:
            return 3
        if target_kind in {
            SpellTarget.ANY_VISIBLE_CREATURES,
            SpellTarget.ALL_ENEMIES,
        }:
            return max(0, candidate_count)
        return 1

    @staticmethod
    def _spell_cost(definition, target_count: int) -> int:
        count = max(1, int(target_count or 1))
        if (
            definition.mp_cost_per_target
            and definition.target
            in {
                SpellTarget.UP_TO_THREE_CREATURES,
                SpellTarget.ANY_VISIBLE_CREATURES,
                SpellTarget.ALL_ENEMIES,
            }
        ):
            return int(definition.mp_cost) * count
        return int(definition.mp_cost)

    @classmethod
    def _max_affordable_spell_targets(
        cls,
        actor,
        definition,
        rule_max_targets: int,
    ) -> int:
        max_targets = max(0, int(rule_max_targets or 0))
        if max_targets == 0:
            return 0
        can_pay_with_life = skill_rank(actor.skills, "生命秘法") > 0
        affordable = 0
        for target_count in range(1, max_targets + 1):
            cost = cls._spell_cost(definition, target_count)
            if actor.mp >= cost or (
                can_pay_with_life
                and actor.hp > cost + 10
            ):
                affordable = target_count
        return affordable

    @staticmethod
    def _submitted_targets(
        parameters: dict[str, object],
    ) -> list[str]:
        raw = parameters.get("targets")
        if isinstance(raw, str):
            values = [
                item.strip()
                for item in re.split(r"[、,，/]+", raw)
                if item.strip()
            ]
        elif isinstance(raw, (list, tuple, set)):
            values = [
                str(item).strip()
                for item in raw
                if str(item).strip()
            ]
        else:
            target = str(parameters.get("target") or "").strip()
            values = [target] if target else []
        return list(dict.fromkeys(values))

    @staticmethod
    def _submitted_statuses(
        parameters: dict[str, object],
    ) -> list[str]:
        raw = (
            parameters.get("chosen_statuses")
            or parameters.get("chosen_status")
        )
        if isinstance(raw, str):
            values = [
                item.strip()
                for item in re.split(r"[、,，/；;\s]+", raw)
                if item.strip()
            ]
        elif isinstance(raw, (list, tuple, set)):
            values = [
                str(item).strip()
                for item in raw
                if str(item).strip()
            ]
        else:
            values = []
        return list(dict.fromkeys(values))
