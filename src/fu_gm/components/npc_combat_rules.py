from __future__ import annotations

import re

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_ability_runtime import (
    npc_attack_adjustment,
    npc_check_bonus,
)
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Action,
    ActionType,
    DecisionWindow,
    EnemyRank,
    GamePanel,
    SpellEffectType,
    SpellTarget,
    StatusEffect,
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
                "spell_profiles": [
                    {
                        "name": profile.name,
                        "rules_name": profile.rules_name or profile.name,
                        "mp_cost": profile.mp_cost,
                        "target": profile.target,
                        "duration": profile.duration,
                        "effect": profile.effect,
                    }
                    for profile in actor.npc_spell_profiles
                ],
                "abilities": list(actor.abilities),
                "equipment": list(actor.equipment),
                "other_actions": list(actor.npc_other_actions),
                "trait_rules": list(actor.npc_trait_rules),
                "typed_abilities": [
                    {
                        "ability_id": profile.ability_id,
                        "name": profile.name,
                        "trigger": profile.trigger,
                        "effect_type": profile.effect_type,
                        "target_scope": profile.target_scope,
                        "amount": profile.amount,
                        "damage_type": profile.damage_type,
                        "attack_name": profile.attack_name,
                        "keywords": list(profile.keywords),
                        "expires_on": (
                            profile.expires_on.value
                            if profile.expires_on is not None
                            else ""
                        ),
                        "description": profile.description,
                    }
                    for profile in actor.npc_ability_profiles
                ],
                "source_template": actor.npc_source_template,
            },
            "tactical_pattern": dict(actor.npc_tactics),
            "legal_actions": self.build_legal_action_catalog(panel, actor_name),
        }

    def build_legal_action_catalog(
        self,
        panel: GamePanel,
        actor_name: str,
    ) -> list[dict[str, object]]:
        actor = self.character_manager.get(actor_name)
        conditional_check_bonus = npc_check_bonus(actor)
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
            for attack in self._attack_catalog(actor):
                actions.append(
                    {
                        "npc_action_type": "Attack",
                        **attack,
                        "targets": opponent_targets,
                        "max_targets": int(attack.get("multi_attack", 1) or 1),
                    }
                )
            actions.extend(
                [
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
        guard_action: dict[str, object] = {
            "npc_action_type": "Guard",
            "guarded_targets": [
                name for name in ally_targets if name != actor_name
            ],
        }
        terrain_options = sorted(
            {
                terrain
                for profile in actor.npc_ability_profiles
                if profile.trigger == "after_guard"
                and profile.effect_type == "terrain_guard"
                for terrain in profile.keywords
            }
        )
        if terrain_options:
            guard_action["terrain_options"] = terrain_options
        actions.append(guard_action)
        for raw_action in actor.npc_other_actions:
            name, _, description = str(raw_action or "").partition("：")
            name = name.strip()
            if not name:
                continue
            entry: dict[str, object] = {
                "npc_action_type": "OtherAction",
                "other_action_name": name,
                "description": description.strip(),
            }
            if name == "传递魔力":
                entry["target_options"] = [actor_name, *ally_targets]
                entry["mp_amount_max"] = min(10, actor.mp)
                if actor.mp <= 0:
                    continue
            elif name == "仙人掌汁液":
                followup = next(
                    (
                        attack
                        for attack in self._attack_catalog(actor)
                        if attack.get("attack_name") == "棘刺弹幕"
                    ),
                    None,
                )
                if followup is None or not opponent_targets:
                    continue
                entry["target_options"] = opponent_targets
                entry["followup_attack"] = followup
            actions.append(entry)
        for ability in actor.npc_ability_profiles:
            if ability.trigger != "skill_action":
                continue
            entry = {
                "npc_action_type": "OtherAction",
                "other_action_name": ability.name,
                "ability_id": ability.ability_id,
                "effect_type": ability.effect_type,
                "description": ability.description,
            }
            if ability.target_scope == "one_enemy":
                if not opponent_targets:
                    continue
                entry["target_options"] = opponent_targets
            actions.append(entry)
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
            profile = next(
                (
                    item
                    for item in actor.npc_spell_profiles
                    if normalize_spell_name(item.rules_name or item.name)
                    == canonical
                ),
                None,
            )
            display_name = profile.name if profile is not None else definition.name
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
                    "spell_name": display_name,
                    "rules_spell_name": canonical,
                    "mp_cost": definition.mp_cost,
                    "mp_cost_per_target": bool(
                        definition.mp_cost_per_target
                    ),
                    "minimum_total_mp_cost": minimum_cost,
                    "target_kind": definition.target.value,
                    "target_options": target_options,
                    "attributes": list(
                        actor.npc_spell_attributes.get(
                            canonical,
                            definition.attributes,
                        )
                    ),
                    "check_modifier": actor.npc_spell_check_bonus,
                    "modifier": conditional_check_bonus,
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
            submitted_spell_name = str(submitted.get("spell_name") or "").strip()
            spell_name = normalize_spell_name(submitted_spell_name)
            entry = next(
                (
                    item
                    for item in candidates
                    if str(item.get("spell_name") or "") == submitted_spell_name
                    or normalize_spell_name(
                        str(item.get("rules_spell_name") or "")
                    ) == spell_name
                ),
                None,
            )
            if entry is None:
                raise ValueError(f"未拥有或当前无法施放法术【{submitted_spell_name}】")
            rules_spell_name = normalize_spell_name(
                str(entry.get("rules_spell_name") or entry.get("spell_name") or "")
            )
            definition = get_spell_definition(rules_spell_name)
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
                    # Keep the authoritative rules key separate from the
                    # public stat-block name.  Some bestiary spells share a
                    # display name with a player spell but have different
                    # effects (for example the skeleton mage's 影袭).
                    "spell_name": rules_spell_name,
                    "npc_spell_name": str(entry.get("spell_name") or definition.name),
                    "target": submitted_targets[0],
                    "attributes": list(entry.get("attributes", [])),
                    "modifier": int(entry.get("modifier", 0) or 0),
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
        elif action_type == "OtherAction":
            other_action_name = str(
                submitted.get("other_action_name") or ""
            ).strip()
            entry = next(
                (
                    item
                    for item in candidates
                    if item.get("other_action_name") == other_action_name
                ),
                None,
            )
            if entry is None:
                raise ValueError(
                    f"未拥有或当前无法执行其他行动【{other_action_name}】"
                )
            common["other_action_name"] = other_action_name
            if other_action_name == "传递魔力":
                target = str(submitted.get("target") or "").strip()
                valid_targets = [
                    str(value)
                    for value in entry.get("target_options", [])
                ]
                try:
                    mp_amount = int(submitted.get("mp_amount") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("传递魔力的精神值数量必须是整数") from exc
                maximum = int(entry.get("mp_amount_max", 0) or 0)
                if target not in valid_targets:
                    raise ValueError("传递魔力的目标不在当前合法选项中")
                if not 1 <= mp_amount <= maximum:
                    raise ValueError(
                        f"传递魔力本次必须消耗1到{maximum}点精神值"
                    )
                common.update({"target": target, "mp_amount": mp_amount})
            elif other_action_name == "仙人掌汁液":
                target = str(submitted.get("target") or "").strip()
                valid_targets = [
                    str(value)
                    for value in entry.get("target_options", [])
                ]
                if target not in valid_targets:
                    raise ValueError("仙人掌汁液的顺势攻击目标不合法")
                followup = dict(entry.get("followup_attack") or {})
                common.update(
                    {
                        "target": target,
                        "attributes": list(followup.get("attributes", [])),
                        "damage_type": str(
                            followup.get("damage_type") or "physical"
                        ),
                        "is_melee": bool(followup.get("is_melee", True)),
                        "attack_id": str(followup.get("attack_id") or ""),
                        "attack_name": str(followup.get("attack_name") or ""),
                        "weapon_damage": int(
                            followup.get("weapon_damage", 0) or 0
                        ),
                        "accuracy_modifier": int(
                            followup.get("accuracy_modifier", 0) or 0
                        ),
                        "targets_magic_defense": bool(
                            followup.get("targets_magic_defense")
                        ),
                        "multi_attack": int(
                            followup.get("multi_attack", 1) or 1
                        ),
                    }
                )
            elif entry.get("ability_id"):
                common["ability_id"] = str(entry["ability_id"])
                valid_targets = [
                    str(value) for value in entry.get("target_options", [])
                ]
                submitted_target = str(submitted.get("target") or "").strip()
                if valid_targets:
                    if submitted_target not in valid_targets:
                        raise ValueError(
                            f"特殊行动【{other_action_name}】需要选择合法目标"
                        )
                    common["target"] = submitted_target
        elif action_type in {"Attack", "Hinder", "Investigate"}:
            if action_type == "Attack":
                attack_id = str(submitted.get("attack_id") or "").strip()
                attack_name = str(submitted.get("attack_name") or "").strip()
                entry = next(
                    (
                        item
                        for item in candidates
                        if (attack_id and item.get("attack_id") == attack_id)
                        or (attack_name and item.get("attack_name") == attack_name)
                    ),
                    candidates[0] if len(candidates) == 1 else None,
                )
                if entry is None:
                    raise ValueError("该NPC有多种基础攻击，必须选择合法的attack_id或attack_name")
            else:
                entry = candidates[0]
            valid_targets = [str(value) for value in entry.get("targets", [])]
            submitted_targets = self._submitted_targets(submitted)
            if not submitted_targets:
                target = str(submitted.get("target") or "").strip()
                submitted_targets = [target] if target else []
            max_targets = int(entry.get("max_targets", 1) or 1)
            if (
                not submitted_targets
                or len(submitted_targets) > max_targets
                or any(target not in valid_targets for target in submitted_targets)
            ):
                raise ValueError("攻击或检定目标不在当前可选目标中")
            common["target"] = submitted_targets[0]
            if len(submitted_targets) > 1:
                common["targets"] = submitted_targets
            if action_type == "Attack":
                common["attributes"] = list(entry.get("attributes", []))
                common["damage_type"] = str(
                    entry.get("damage_type") or "physical"
                )
                common["is_melee"] = bool(entry.get("is_melee", True))
                common["attack_id"] = str(entry.get("attack_id") or "")
                common["attack_name"] = str(entry.get("attack_name") or "")
                common["weapon_damage"] = int(entry.get("weapon_damage", 0) or 0)
                common["accuracy_modifier"] = int(entry.get("accuracy_modifier", 0) or 0)
                common["targets_magic_defense"] = bool(
                    entry.get("targets_magic_defense")
                )
                common["multi_attack"] = int(entry.get("multi_attack", 1) or 1)
                common["ignore_resist"] = bool(entry.get("ignore_resist"))
                if entry.get("consumes_combat_preparation"):
                    preparation = entry.get("consumes_combat_preparation")
                    common["consumes_combat_preparation"] = (
                        list(preparation)
                        if isinstance(preparation, list)
                        else str(preparation)
                    )
                status = entry.get("status_effect_on_hit")
                if status is not None:
                    common["status_effect_on_hit"] = status
                damage_type_options = [
                    str(value)
                    for value in entry.get("damage_type_options", [])
                ]
                if damage_type_options:
                    chosen_damage_type = str(
                        submitted.get("chosen_damage_type")
                        or submitted.get("damage_type")
                        or ""
                    ).strip()
                    if chosen_damage_type not in damage_type_options:
                        raise ValueError(
                            f"攻击【{entry.get('attack_name')}】需要选择合法的伤害类型"
                        )
                    common["damage_type"] = chosen_damage_type
                random_damage_types = [
                    str(value)
                    for value in entry.get("random_damage_types", [])
                ]
                if random_damage_types:
                    common["random_damage_types"] = random_damage_types
                status_options = [
                    value.value if isinstance(value, StatusEffect) else str(value)
                    for value in entry.get("status_options_on_hit", [])
                ]
                if status_options:
                    chosen_status = str(
                        submitted.get("chosen_status")
                        or submitted.get("status_effect_on_hit")
                        or ""
                    ).strip()
                    if chosen_status not in status_options:
                        raise ValueError(
                            f"攻击【{entry.get('attack_name')}】需要选择合法的异常状态"
                        )
                    common["status_effect_on_hit"] = chosen_status
                for key in (
                    "conditional_damage_bonus",
                    "recover_mp_on_hit",
                    "target_mp_loss",
                    "target_ip_loss",
                    "self_hp_loss_if_all_miss",
                ):
                    value = int(entry.get(key, 0) or 0)
                    if value:
                        common[key] = value
                recover_hp_fraction = float(
                    entry.get("recover_hp_fraction", 0.0) or 0.0
                )
                if recover_hp_fraction:
                    common["recover_hp_fraction"] = recover_hp_fraction
                conditional_statuses = [
                    value.value if isinstance(value, StatusEffect) else str(value)
                    for value in entry.get("conditional_target_statuses", [])
                ]
                if conditional_statuses:
                    common["conditional_target_statuses"] = conditional_statuses
                if entry.get("conditional_any_target_status"):
                    common["conditional_any_target_status"] = True
                attack_effects = list(entry.get("effects") or [])
                if attack_effects:
                    common["npc_attack_effects"] = attack_effects
                if entry.get("notes"):
                    common["attack_notes"] = list(entry.get("notes", []))
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
            submitted_terrain = str(submitted.get("terrain") or "").strip()
            terrain_options = [
                str(value)
                for value in candidates[0].get("terrain_options", [])
            ]
            if submitted_terrain:
                if submitted_terrain not in terrain_options:
                    raise ValueError(
                        f"防御行动不能凭空使用地形【{submitted_terrain}】"
                    )
                common["terrain"] = submitted_terrain
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
            if "clock_direction" not in entry:
                raise ValueError("NPC推进目标缺少明确的命刻方向。")
            common.update(
                {
                    "clock_name": clock_name,
                    "target": clock_name,
                    "attributes": list(entry.get("attributes", [])),
                    "target_number": target_number,
                    "clock_direction": int(entry["clock_direction"]),
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
    def _attack_catalog(actor) -> list[dict[str, object]]:
        if actor.npc_attacks:
            catalog = [
                {
                    "attack_id": attack.attack_id,
                    "attack_name": attack.name,
                    "attributes": list(attack.attributes),
                    "weapon_damage": int(attack.damage_bonus),
                    "accuracy_modifier": int(attack.accuracy_modifier),
                    "damage_type": attack.damage_type,
                    "range": attack.range,
                    "is_melee": str(attack.range or "melee").lower()
                    not in {"ranged", "远程"},
                    "targets_magic_defense": bool(attack.targets_magic_defense),
                    "multi_attack": max(1, int(attack.multi_attack or 1)),
                    "status_effect_on_hit": attack.status_effect_on_hit,
                    "damage_type_options": list(attack.damage_type_options),
                    "random_damage_types": list(attack.random_damage_types),
                    "status_options_on_hit": [
                        status.value for status in attack.status_options_on_hit
                    ],
                    "conditional_damage_bonus": int(
                        attack.conditional_damage_bonus
                    ),
                    "conditional_target_statuses": [
                        status.value
                        for status in attack.conditional_target_statuses
                    ],
                    "conditional_any_target_status": bool(
                        attack.conditional_any_target_status
                    ),
                    "bonus_if_previous_guard": int(
                        attack.bonus_if_previous_guard
                    ),
                    "recover_hp_fraction": float(attack.recover_hp_fraction),
                    "recover_mp_on_hit": int(attack.recover_mp_on_hit),
                    "target_mp_loss": int(attack.target_mp_loss),
                    "target_ip_loss": int(attack.target_ip_loss),
                    "self_hp_loss_if_all_miss": int(
                        attack.self_hp_loss_if_all_miss
                    ),
                    "effects": [
                        {
                            "effect_type": effect.effect_type,
                            "trigger": effect.trigger,
                            "target_scope": effect.target_scope,
                            "damage_type": effect.damage_type,
                            "damage_types": list(effect.damage_types),
                            "affinity": (
                                effect.affinity.value
                                if effect.affinity is not None
                                else ""
                            ),
                            "status": (
                                effect.status.value
                                if effect.status is not None
                                else ""
                            ),
                            "required_status": (
                                effect.required_status.value
                                if effect.required_status is not None
                                else ""
                            ),
                            "required_status_before_hit": bool(
                                effect.required_status_before_hit
                            ),
                            "amount": int(effect.amount),
                            "action_types": list(effect.action_types),
                            "trait": effect.trait,
                            "expires_on": (
                                effect.expires_on.value
                                if effect.expires_on is not None
                                else ""
                            ),
                            "check_attributes": list(effect.check_attributes),
                            "target_number": int(effect.target_number),
                            "clock_segments": int(effect.clock_segments),
                            "note": effect.note,
                        }
                        for effect in attack.effects
                    ],
                    "notes": list(attack.notes),
                }
                for attack in actor.npc_attacks
            ]
        else:
            catalog = [
                {
                    "attack_id": "attack-1",
                    "attack_name": actor.equipped_main_hand or "基础攻击",
                    "attributes": list(actor.weapon_accuracy_attributes),
                    "weapon_damage": int(actor.weapon_damage),
                    "accuracy_modifier": int(actor.weapon_accuracy_modifier),
                    "damage_type": actor.weapon_type,
                    "range": actor.weapon_range,
                    "is_melee": str(actor.weapon_range or "melee").lower()
                    not in {"ranged", "远程"},
                    "targets_magic_defense": bool(
                        actor.equipment_attack_targets_magic_defense
                    ),
                    "multi_attack": max(
                        1,
                        int(actor.equipment_multi_attack or 1),
                    ),
                    "status_effect_on_hit": actor.equipment_on_hit_status,
                    "notes": list(actor.equipment_notes),
                }
            ]
        forced_attack = str(
            actor.npc_skill_effects.get("forced_next_attack") or ""
        ).strip()
        if forced_attack:
            catalog = [
                entry
                for entry in catalog
                if entry.get("attack_name") == forced_attack
            ]
            for entry in catalog:
                entry["status_effect_on_hit"] = StatusEffect.SHAKEN
                entry["consumes_combat_preparation"] = "forced_next_attack"
        if actor.npc_skill_effects.get("charged_attack"):
            for entry in catalog:
                entry["multi_attack"] = max(
                    2,
                    int(entry.get("multi_attack", 1) or 1),
                )
                entry["ignore_resist"] = True
                entry["consumes_combat_preparation"] = "charged_attack"
        triggered_multiattack = dict(
            actor.npc_skill_effects.get("triggered_multiattack") or {}
        )
        ignored_resist_attacks = set(
            actor.npc_skill_effects.get("triggered_ignore_resist") or []
        )
        prepared_damage = dict(
            actor.npc_skill_effects.get("prepared_attack_damage") or {}
        )
        for entry in catalog:
            attack_name = str(entry.get("attack_name") or "")
            previous_guard_bonus = int(
                entry.get("bonus_if_previous_guard", 0) or 0
            )
            if (
                previous_guard_bonus
                and actor.npc_skill_effects.get("previous_action_guarded")
            ):
                entry["weapon_damage"] = int(
                    entry.get("weapon_damage", 0) or 0
                ) + previous_guard_bonus
            conditional_damage, conditional_type = npc_attack_adjustment(
                actor,
                attack_name,
            )
            entry["accuracy_modifier"] = int(
                entry.get("accuracy_modifier", 0) or 0
            ) + npc_check_bonus(actor)
            entry["weapon_damage"] = int(
                entry.get("weapon_damage", 0) or 0
            ) + conditional_damage
            if conditional_type:
                entry["damage_type"] = conditional_type
            multi_attack = triggered_multiattack.get(
                attack_name,
                triggered_multiattack.get("*", 1),
            )
            entry["multi_attack"] = max(
                int(entry.get("multi_attack", 1) or 1),
                int(multi_attack or 1),
            )
            if "*" in ignored_resist_attacks or attack_name in ignored_resist_attacks:
                entry["ignore_resist"] = True
            if prepared_damage:
                entry["weapon_damage"] = int(entry.get("weapon_damage", 0) or 0) + int(
                    prepared_damage.get("amount", 0) or 0
                )
                existing = entry.get("consumes_combat_preparation")
                existing_items = (
                    list(existing)
                    if isinstance(existing, list)
                    else [existing]
                    if existing
                    else []
                )
                entry["consumes_combat_preparation"] = [
                    *existing_items,
                    "prepared_attack_damage",
                ]
        return catalog

    @staticmethod
    def _specialty_bonus(actor, specialty: str) -> int:
        return int(actor.npc_specialty_bonuses.get(specialty, 0)) + npc_check_bonus(
            actor,
            specialty,
        )

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
