from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.models import (
    Affinity,
    CombatLogEntry,
    ConflictEvent,
    ConflictState,
    EffectTiming,
    EnemyRank,
    EscalationStage,
    StatusEffect,
    TimedEffect,
)


class ConflictManager:
    def __init__(self, character_manager: CharacterManager) -> None:
        self.character_manager = character_manager
        self.state = ConflictState()

    def start_scene(self, scene_name: str, turn_order: list[str]) -> None:
        carried_state = {
            "ultima_points": dict(self.state.ultima_points),
            "exalted_enemies": set(self.state.exalted_enemies),
            "enemy_ranks": dict(self.state.enemy_ranks),
            "villains": set(self.state.villains),
            "villain_appearance_awarded": set(),
            "enemy_action_counts": dict(self.state.enemy_action_counts),
            "escalation_stages": {name: list(stages) for name, stages in self.state.escalation_stages.items()},
            "current_escalation_stage": dict(self.state.current_escalation_stage),
        }
        self._clear_all_timed_effects()
        self.state = ConflictState(
            active=True,
            scene_name=scene_name,
            round_number=1,
            turn_order=list(turn_order),
            current_turn_index=0,
            ultima_points=carried_state["ultima_points"],
            exalted_enemies=carried_state["exalted_enemies"],
            enemy_ranks=carried_state["enemy_ranks"],
            villains=carried_state["villains"],
            villain_appearance_awarded=carried_state["villain_appearance_awarded"],
            enemy_action_counts=carried_state["enemy_action_counts"],
            escalation_stages=carried_state["escalation_stages"],
            current_escalation_stage=carried_state["current_escalation_stage"],
        )
        self.record_log("system", "scene_start", f"冲突场景【{scene_name}】开始。")
        self.begin_current_turn()

    def build_alternating_turn_order(
        self,
        player_side: list[str],
        enemy_side: list[str],
        *,
        players_first: bool,
    ) -> list[str]:
        first = list(player_side if players_first else enemy_side)
        second = list(enemy_side if players_first else player_side)
        order: list[str] = []
        max_len = max(len(first), len(second))
        for index in range(max_len):
            if index < len(first):
                order.append(first[index])
            if index < len(second):
                order.append(second[index])
        return [name for name in order if self.character_manager.exists(name)]

    def start_scene_from_initiative(
        self,
        scene_name: str,
        player_side: list[str],
        enemy_side: list[str],
        *,
        players_first: bool,
    ) -> list[str]:
        turn_order = self.build_alternating_turn_order(
            player_side,
            enemy_side,
            players_first=players_first,
        )
        self.start_scene(scene_name, turn_order)
        return turn_order

    def begin_current_turn(self) -> str | None:
        actor = self.state.current_actor()
        if actor is None:
            self.state.turn_started_actor = None
            return None
        if self.state.turn_started_actor == actor:
            return actor
        if self.state.current_bonus_actor is None and actor in self.state.acted_this_round:
            self._consume_action_penalty(actor, 1)
            self.record_log(actor, "turn_consumed_by_teamwork", f"{actor} 本轮已用行动协助同伴，跳过自己的回合。")
            self._advance_base_turn()
            return self.begin_current_turn()
        action_count = self.state.enemy_action_counts.get(actor, 1)
        if self.state.current_bonus_actor is None and self.state.action_penalties.get(actor, 0) >= action_count:
            self._consume_action_penalty(actor, action_count)
            self.record_log(actor, "turn_skipped", f"{actor} 的下个回合少执行 {action_count} 次行动，本回合无法行动。")
            self._advance_base_turn()
            return self.begin_current_turn()
        self._expire_effects(EffectTiming.OWNER_TURN_START, actor)
        self.state.turn_started_actor = actor
        return actor

    def end_current_turn(self) -> str | None:
        actor = self.state.turn_started_actor
        if actor is None:
            return None
        if self.state.current_bonus_actor is None:
            self._queue_extra_action_if_needed(actor)
            self._mark_acted(actor)
            self.state.pending_assists.pop(actor, None)
        self._expire_effects(EffectTiming.OWNER_TURN_END, actor)
        self.state.turn_started_actor = None
        return actor

    def next_turn(self) -> str | None:
        if not self.state.turn_order and self.state.current_bonus_actor is None and not self.state.queued_turns:
            return None
        previous_bonus_actor = self.state.current_bonus_actor
        self.end_current_turn()

        if previous_bonus_actor is not None:
            self.state.current_bonus_actor = None
            queued_actor = self._pop_next_queued_turn()
            if queued_actor is not None:
                self.state.current_bonus_actor = queued_actor
            else:
                self._advance_base_turn()
        else:
            queued_actor = self._pop_next_queued_turn()
            if queued_actor is not None:
                self.state.current_bonus_actor = queued_actor
            else:
                self._advance_base_turn()
        return self.begin_current_turn()

    def grant_bonus_turn(self, actor_name: str, count: int = 1) -> None:
        if count <= 0:
            return
        for _ in range(count):
            if self._is_available_combatant(actor_name):
                self.state.queued_turns.append(actor_name)

    def penalize_next_turn(self, actor_name: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.state.action_penalties[actor_name] = self.state.action_penalties.get(actor_name, 0) + count

    def can_assist_current_turn(self, supporter_name: str, leader_name: str | None = None) -> bool:
        if not self.state.active:
            return False
        leader_name = leader_name or self.state.current_actor()
        if not leader_name or leader_name == supporter_name:
            return False
        if not self._is_available_combatant(supporter_name) or not self._is_available_combatant(leader_name):
            return False
        supporter = self.character_manager.get(supporter_name)
        leader = self.character_manager.get(leader_name)
        if "pc" not in supporter.traits or "pc" not in leader.traits:
            return False
        if supporter_name in self.state.acted_this_round:
            return False
        if supporter_name not in self.state.turn_order:
            return False
        current_actor = self.state.current_actor()
        if leader_name != current_actor:
            return False
        if self.state.current_bonus_actor is not None:
            return supporter_name != self.state.current_bonus_actor
        current_index = self.state.current_turn_index % len(self.state.turn_order) if self.state.turn_order else 0
        try:
            supporter_index = self.state.turn_order.index(supporter_name)
        except ValueError:
            return False
        return supporter_index > current_index

    def register_team_assist(self, supporter_name: str, leader_name: str | None = None, *, reason: str = "") -> bool:
        leader_name = leader_name or self.state.current_actor()
        if not self.can_assist_current_turn(supporter_name, leader_name):
            return False
        helpers = self.state.pending_assists.setdefault(str(leader_name), [])
        if supporter_name not in helpers:
            helpers.append(supporter_name)
        self._mark_acted(supporter_name)
        self.penalize_next_turn(supporter_name, 1)
        detail = f" 原因：{reason}" if reason else ""
        self.record_log(
            supporter_name,
            "team_assist_registered",
            f"{supporter_name} 本轮消耗自己的回合，协助 {leader_name} 的下一次检定。{detail}",
        )
        return True

    def consume_pending_assists(self, leader_name: str) -> list[str]:
        helpers = self.state.pending_assists.pop(leader_name, [])
        return [name for name in helpers if self.character_manager.exists(name)]

    def register_held_action(self, actor_name: str, action_type: str, summary: str) -> dict[str, object]:
        entry = {
            "round_number": self.state.round_number,
            "actor": actor_name,
            "action_type": action_type,
            "summary": summary,
        }
        self.state.held_actions.append(entry)
        self.state.held_actions = self.state.held_actions[-20:]
        self.record_log(actor_name, "held_action", f"{actor_name} 的回合外动作已暂缓：{summary}")
        return entry

    def remove_combatant_from_scene(self, target: str, *, as_escaped: bool = True) -> None:
        if as_escaped:
            self.state.escaped_combatants.add(target)
        else:
            self.state.defeated_combatants.add(target)
        self._remove_from_turn_order(target)

    def set_ultima_points(self, enemy_name: str, value: int) -> None:
        self.state.ultima_points[enemy_name] = value

    def register_enemy(
        self,
        enemy_name: str,
        rank: EnemyRank,
        ultima_points: int = 0,
        escalation_stages: list[EscalationStage] | None = None,
        action_count: int | None = None,
        is_villain: bool | None = None,
    ) -> None:
        story_villain = rank == EnemyRank.VILLAIN or bool(is_villain) or ultima_points > 0 or bool(escalation_stages)
        combat_rank = EnemyRank.SOLDIER if rank == EnemyRank.VILLAIN else rank
        self.state.enemy_ranks[enemy_name] = combat_rank
        if story_villain:
            self.state.villains.add(enemy_name)
        self.state.ultima_points[enemy_name] = ultima_points
        self.state.escalation_stages[enemy_name] = list(escalation_stages or [])
        self.state.current_escalation_stage[enemy_name] = -1
        self.state.enemy_action_counts[enemy_name] = max(1, action_count or self._default_action_count(combat_rank))

    def award_villain_appearance_fabula(self, villain_name: str, *, max_per_pc: int = 1) -> ConflictEvent:
        if not self.is_villain(villain_name):
            raise ValueError(f"{villain_name} 不是反派，不能触发反派登场物语点奖励。")
        if villain_name in self.state.villain_appearance_awarded:
            return ConflictEvent(
                target=villain_name,
                event_type="villain_appearance_already_awarded",
                summary=f"反派【{villain_name}】本场景的登场物语点奖励已经结算过。",
            )
        awarded = 0
        for character in self.character_manager.all():
            if "pc" not in character.traits:
                continue
            before, after = self.character_manager.modify_resource(
                character.name,
                "fabula_points",
                min(1, max(1, max_per_pc)),
            )
            awarded += after - before
        self.state.villain_appearance_awarded.add(villain_name)
        event = ConflictEvent(
            target=villain_name,
            event_type="villain_appearance",
            summary=f"反派【{villain_name}】正式登场，所有玩家角色获得 1 点物语点。",
            fabula_awarded=awarded,
        )
        self.record_log(villain_name, event.event_type, event.summary)
        return event

    def spend_ultima_for_trait_invocation(self, villain_name: str) -> ConflictEvent:
        if self.state.ultima_points.get(villain_name, 0) < 1:
            raise ValueError(f"{villain_name} 没有足够的终结点。")
        if not self.is_villain(villain_name):
            raise ValueError(f"{villain_name} 不是反派，不能消耗终结点援用特质。")
        self.state.ultima_points[villain_name] -= 1
        event = ConflictEvent(
            target=villain_name,
            event_type="villain_trait_invocation",
            summary=f"{villain_name} 消耗 1 点终结点援用特质，重掷检定骰。",
            ultima_spent=1,
        )
        self.record_log(villain_name, event.event_type, event.summary)
        return event

    def is_villain(self, target: str) -> bool:
        if target in self.state.villains:
            return True
        if not self.character_manager.exists(target):
            return False
        character = self.character_manager.get(target)
        return "villain" in character.traits

    def current_stage(self, target: str) -> EscalationStage | None:
        stages = self.state.escalation_stages.get(target, [])
        index = self.state.current_escalation_stage.get(target, -1)
        if index < 0 or index >= len(stages):
            return None
        return stages[index]

    def can_escalate(self, target: str) -> bool:
        stages = self.state.escalation_stages.get(target, [])
        next_index = self.state.current_escalation_stage.get(target, -1) + 1
        return next_index < len(stages)

    def register_effect(self, effect: TimedEffect) -> None:
        self.clear_effects(effect.owner, effect.effect_type, effect.effect_key or None, effect.target)
        self._apply_effect(effect)
        self.state.active_effects.append(effect)

    def apply_guard(self, actor_name: str, guarded_target: str | None = None) -> None:
        self.register_effect(
            TimedEffect(
                owner=actor_name,
                effect_type="guard",
                expires_on=EffectTiming.OWNER_TURN_START,
                target=guarded_target,
                source="Guard",
                effect_key="guard",
                note="防御姿态持续到该角色下回合开始。",
            )
        )

    def clear_effects(
        self,
        owner: str,
        effect_type: str | None = None,
        effect_key: str | None = None,
        target: str | None = None,
    ) -> None:
        remaining_effects = []
        for effect in self.state.active_effects:
            matches_owner = effect.owner == owner
            matches_type = effect_type is None or effect.effect_type == effect_type
            matches_key = effect_key is None or effect.effect_key == effect_key
            matches_target = target is None or effect.target == target
            if matches_owner and matches_type and matches_key and matches_target:
                self._cleanup_effect(effect)
                continue
            remaining_effects.append(effect)
        self.state.active_effects = remaining_effects

    def clear_spell_effects_on_target(self, target: str) -> list[str]:
        removed_sources: list[str] = []
        remaining_effects = []
        for effect in self.state.active_effects:
            if effect.target == target and effect.effect_key.startswith("spell:"):
                self._cleanup_effect(effect)
                removed_sources.append(effect.source or effect.effect_key)
                continue
            remaining_effects.append(effect)
        self.state.active_effects = remaining_effects
        return removed_sources

    def prevent_zero_hp_once(self, target: str) -> bool:
        for effect in list(self.state.active_effects):
            if effect.effect_type != "survive_once":
                continue
            if effect.target != target:
                continue
            self.character_manager.get(target).hp = 1
            self.clear_effects(effect.owner, effect.effect_type, effect.effect_key, effect.target)
            return True
        character = self.character_manager.get(target)
        if "不破之人" in character.hero_skills and target not in self.state.passive_survival_used:
            character.hp = 1
            self.state.passive_survival_used.add(target)
            return True
        return False

    def end_scene(self) -> None:
        self._clear_all_timed_effects()
        self.state.active = False
        self.state.scene_name = ""
        self.state.round_number = 0
        self.state.turn_order = []
        self.state.current_turn_index = 0
        self.state.current_bonus_actor = None
        self.state.queued_turns = []
        self.state.turn_started_actor = None
        self.state.acted_this_round = []
        self.state.pending_assists = {}
        self.state.held_actions = []

    def apply_status(self, target: str, status: StatusEffect) -> bool:
        applied = self.character_manager.add_status(target, status)
        if applied:
            self.state.active_statuses.setdefault(target, []).append(status)
        return applied

    def remove_status(self, target: str, status: StatusEffect) -> bool:
        removed = self.character_manager.remove_status(target, status)
        if removed and target in self.state.active_statuses:
            self.state.active_statuses[target] = [
                active_status for active_status in self.state.active_statuses[target] if active_status != status
            ]
            if not self.state.active_statuses[target]:
                del self.state.active_statuses[target]
        return removed

    def clear_statuses(self, target: str) -> bool:
        had_statuses = bool(self.character_manager.get(target).statuses)
        self.character_manager.clear_statuses(target)
        self.state.active_statuses.pop(target, None)
        return had_statuses

    def spend_ultima_to_recover(self, target: str) -> ConflictEvent:
        if self.state.ultima_points.get(target, 0) < 1:
            raise ValueError(f"{target} 没有足够的终结点。")
        self.state.ultima_points[target] -= 1
        _, mp_after = self.character_manager.modify_resource(target, "mp", 50)
        cleared = self.clear_statuses(target)
        event = ConflictEvent(
            target=target,
            event_type="ultima_recovery",
            summary=f"{target} 消耗 1 点终结点，解除全部异常状态并恢复 50 MP。",
            ultima_spent=1,
            statuses_cleared=cleared,
            mp_after=mp_after,
        )
        self.record_log(target, event.event_type, event.summary)
        return event

    def resolve_zero_hp(
        self,
        target: str,
        pc_choice: str = "give_up_resistance",
        pc_consequence: str = "被俘虏并失去重要装备",
        villain_mode: str = "auto",
        allow_escalation: bool = True,
        sacrifice_benefits_bond: bool | None = None,
        sacrifice_betters_world: bool | None = None,
    ) -> ConflictEvent:
        character = self.character_manager.get(target)
        is_pc = "pc" in character.traits
        is_villain = self.is_villain(target)

        if is_pc:
            if pc_choice == "sacrifice":
                sacrifice_conditions = 0
                if any(self.is_villain(name) for name in self.state.turn_order):
                    sacrifice_conditions += 1
                if sacrifice_benefits_bond is None:
                    sacrifice_benefits_bond = bool(character.bonds)
                if sacrifice_betters_world is None:
                    sacrifice_betters_world = any(token in character.theme for token in ["希望", "正义", "使命", "慈悲"])
                if sacrifice_benefits_bond:
                    sacrifice_conditions += 1
                if sacrifice_betters_world:
                    sacrifice_conditions += 1
                if sacrifice_conditions < 2:
                    raise ValueError("玩家角色牺牲必须至少满足：场景中存在反派、牺牲会使羁绊对象受益、相信牺牲会让世界更好中的两项。")
                self.state.sacrifices.add(target)
                self.state.defeated_combatants.add(target)
                self._remove_from_turn_order(target)
                event = ConflictEvent(
                    target=target,
                    event_type="pc_sacrifice",
                    summary=f"{target} 牺牲自己，完成改变世界的史诗壮举。",
                    consequence="永久死亡",
                    hp_after=character.hp,
                )
                self.record_log(target, event.event_type, event.summary)
                return event

            before, after = self.character_manager.modify_resource(target, "fabula_points", 2)
            self.state.fallen_pcs[target] = pc_consequence
            self.state.defeated_combatants.add(target)
            self._remove_from_turn_order(target)
            event = ConflictEvent(
                target=target,
                event_type="pc_give_up_resistance",
                summary=f"{target} 选择放弃抵抗，活了下来，但必须承受沉重代价。",
                fabula_awarded=after - before,
                consequence=pc_consequence,
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        if is_villain and self.state.ultima_points.get(target, 0) > 0 and villain_mode != "surrender":
            self.state.ultima_points[target] -= 1
            self.state.escaped_combatants.add(target)
            self.state.defeated_combatants.discard(target)
            self._remove_from_turn_order(target)
            event = ConflictEvent(
                target=target,
                event_type="villain_escape",
                summary=f"{target} 消耗 1 点终结点，从必死绝境中逃脱。",
                ultima_spent=1,
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        if is_villain and allow_escalation and villain_mode != "surrender":
            escalation = self.try_escalate(target)
            if escalation is not None:
                return escalation

        if is_villain:
            self.state.surrendered_combatants.add(target)
            self.state.defeated_combatants.add(target)
            self._remove_from_turn_order(target)
            event = ConflictEvent(
                target=target,
                event_type="villain_surrender",
                summary=f"{target} 无力再战，选择投降。",
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        self.state.defeated_combatants.add(target)
        self._remove_from_turn_order(target)
        event = ConflictEvent(
            target=target,
            event_type="enemy_defeated",
            summary=f"{target} 倒下并失去战斗力。",
            hp_after=character.hp,
        )
        self.record_log(target, event.event_type, event.summary)
        return event

    def try_escalate(self, target: str) -> ConflictEvent | None:
        stages = self.state.escalation_stages.get(target, [])
        current_index = self.state.current_escalation_stage.get(target, -1)
        next_index = current_index + 1
        if next_index >= len(stages):
            return None

        stage = stages[next_index]
        self.state.current_escalation_stage[target] = next_index
        self.state.exalted_enemies.add(target)
        self.state.villains.add(target)
        self.state.ultima_points[target] = stage.ultima_points

        target_character = self.character_manager.get(target)
        target_character.hp = target_character.max_hp if stage.hp_restore is None else min(stage.hp_restore, target_character.max_hp)
        target_character.mp = target_character.max_mp if stage.mp_restore is None else min(stage.mp_restore, target_character.max_mp)
        self.clear_statuses(target)
        for status in stage.added_statuses:
            self.apply_status(target, status)
        for damage_type, affinity in stage.affinity_changes.items():
            self.character_manager.set_temporary_affinity(target, damage_type, Affinity(affinity))
        for ability in stage.added_abilities:
            if ability not in target_character.abilities:
                target_character.abilities.append(ability)
        for spell in stage.added_spells:
            if spell not in target_character.spells:
                target_character.spells.append(spell)
        if stage.action_count is not None:
            self.state.enemy_action_counts[target] = max(1, stage.action_count)

        fabula_awarded = 0
        for character in self.character_manager.all():
            if "pc" not in character.traits:
                continue
            before, after = self.character_manager.modify_resource(character.name, "fabula_points", 1)
            fabula_awarded += after - before

        summary = f"{target} 升格至【{stage.name}】阶段，终结点补满并变得更强。所有玩家角色获得 1 点物语点。"
        if stage.public_cue:
            summary += f" {stage.public_cue}"
        event = ConflictEvent(
            target=target,
            event_type="escalation",
            summary=summary,
            stage_name=stage.name,
            hp_after=target_character.hp,
            mp_after=target_character.mp,
            statuses_cleared=True,
            fabula_awarded=fabula_awarded,
        )
        self.record_log(target, event.event_type, event.summary)
        return event

    def _remove_from_turn_order(self, target: str) -> None:
        self.clear_effects(target)
        self.state.queued_turns = [name for name in self.state.queued_turns if name != target]
        self.state.acted_this_round = [name for name in self.state.acted_this_round if name != target]
        self.state.pending_assists = {
            leader: [name for name in helpers if name != target]
            for leader, helpers in self.state.pending_assists.items()
            if leader != target
        }
        if self.state.current_bonus_actor == target:
            self.state.current_bonus_actor = None
        if target not in self.state.turn_order:
            return
        current_actor = self.state.current_actor()
        self.state.turn_order = [name for name in self.state.turn_order if name != target]
        if not self.state.turn_order:
            self.state.current_turn_index = 0
            return
        if current_actor == target:
            self.state.current_turn_index %= len(self.state.turn_order)
            return
        self.state.current_turn_index = min(self.state.current_turn_index, len(self.state.turn_order) - 1)

    def format_phase(self) -> str:
        if not self.state.active:
            return "自由场景"
        actor = self.state.current_actor() or "未知"
        phase = f"冲突场景（{self.state.scene_name}，第 {self.state.round_number} 轮，当前行动者：{actor}）"
        if self.state.current_bonus_actor is not None:
            phase += " [奖励回合]"
        if actor in self.state.current_escalation_stage and self.state.current_escalation_stage[actor] >= 0:
            stage = self.state.escalation_stages[actor][self.state.current_escalation_stage[actor]]
            phase += f" [阶段：{stage.name}]"
        action_count = self.state.enemy_action_counts.get(actor, 1)
        if action_count > 1:
            phase += f" [每轮 {action_count} 次行动]"
        return phase

    def record_log(self, actor: str, event_type: str, summary: str) -> None:
        if not summary:
            return
        self.state.combat_log.append(
            CombatLogEntry(
                round_number=max(0, self.state.round_number),
                actor=actor,
                event_type=event_type,
                summary=summary,
            )
        )
        if len(self.state.combat_log) > 50:
            self.state.combat_log = self.state.combat_log[-50:]

    def format_combat_log(self, limit: int = 6) -> list[str]:
        entries = self.state.combat_log[-max(1, limit) :]
        return [
            f"R{entry.round_number} {entry.actor} [{entry.event_type}] {entry.summary}"
            for entry in entries
        ]

    def format_turn_board(self) -> dict[str, object]:
        current_actor = self.state.current_actor()
        acted: list[str] = []
        waiting: list[str] = []
        if self.state.turn_order:
            current_index = self.state.current_turn_index % len(self.state.turn_order)
            acted = self.state.turn_order[:current_index]
            waiting = self.state.turn_order[current_index:]
        return {
            "scene_name": self.state.scene_name,
            "round_number": self.state.round_number,
            "current_actor": current_actor,
            "acted": acted,
            "waiting": waiting,
            "bonus_actor": self.state.current_bonus_actor,
            "queued_turns": list(self.state.queued_turns),
            "acted_this_round": list(self.state.acted_this_round),
            "pending_assists": {leader: list(helpers) for leader, helpers in self.state.pending_assists.items()},
            "held_actions": list(self.state.held_actions),
            "phase": self.format_phase(),
            "recent_log": self.format_combat_log(),
        }

    def _advance_base_turn(self) -> None:
        if not self.state.turn_order:
            return
        self.state.current_turn_index += 1
        if self.state.current_turn_index >= len(self.state.turn_order):
            self.state.current_turn_index = 0
            self._expire_effects(EffectTiming.ROUND_END)
            self.state.round_number += 1
            self.state.acted_this_round = []
            self.state.pending_assists = {}

    def _mark_acted(self, actor: str) -> None:
        if actor and actor not in self.state.acted_this_round:
            self.state.acted_this_round.append(actor)

    def _pop_next_queued_turn(self) -> str | None:
        while self.state.queued_turns:
            actor_name = self.state.queued_turns.pop(0)
            if self._is_available_combatant(actor_name):
                return actor_name
        return None

    def _expire_effects(self, timing: EffectTiming, owner: str | None = None) -> None:
        remaining_effects = []
        for effect in self.state.active_effects:
            matches_timing = effect.expires_on == timing
            matches_owner = owner is None or effect.owner == owner
            if matches_timing and matches_owner:
                self._cleanup_effect(effect)
                continue
            remaining_effects.append(effect)
        self.state.active_effects = remaining_effects

    def _clear_all_timed_effects(self) -> None:
        for effect in list(self.state.active_effects):
            self._cleanup_effect(effect)
        self.state.active_effects = []

    def _cleanup_effect(self, effect: TimedEffect) -> None:
        if effect.effect_type == "arcanum_link" and self.character_manager.exists(effect.owner):
            self.character_manager.get(effect.owner).active_arcanum = ""
            return
        if effect.effect_type == "guard" and self.character_manager.exists(effect.owner):
            self.character_manager.set_guarding(effect.owner, False)
            return
        if effect.effect_type == "defense_bonus" and effect.target is not None and self.character_manager.exists(effect.target):
            for defense_type, amount in effect.data.get("defense_bonus", {}).items():
                self.character_manager.remove_defense_bonus(effect.target, defense_type, amount)
            return
        if effect.effect_type == "affinity_buff" and effect.target is not None and self.character_manager.exists(effect.target):
            for damage_type in effect.data.get("affinity_changes", {}):
                replacement = self._latest_affinity_from_other_effects(effect, damage_type)
                if replacement is None:
                    self.character_manager.clear_temporary_affinity(effect.target, damage_type)
                else:
                    self.character_manager.set_temporary_affinity(effect.target, damage_type, replacement)
            return
        if effect.effect_type == "defense_floor" and effect.target is not None and self.character_manager.exists(effect.target):
            for defense_type in effect.data.get("defense_floor", {}):
                replacement = self._max_defense_floor_from_other_effects(effect, defense_type)
                self.character_manager.set_defense_floor(effect.target, defense_type, replacement)
            return
        if effect.effect_type == "status_immunity" and effect.target is not None and self.character_manager.exists(effect.target):
            immunities = self._status_immunities_from_other_effects(effect)
            self.character_manager.clear_status_immunities(effect.target)
            for status in immunities:
                self.character_manager.add_status_immunity(effect.target, status)
            return
        if effect.effect_type == "attribute_buff" and effect.target is not None and self.character_manager.exists(effect.target):
            for attribute, steps in effect.data.get("attribute_bonus", {}).items():
                self.character_manager.remove_attribute_bonus(effect.target, attribute, steps)
            return
        if effect.effect_type == "weapon_enchant" and effect.target is not None and self.character_manager.exists(effect.target):
            replacement = self._latest_weapon_enchant_from_other_effects(effect)
            self.character_manager.set_weapon_damage_type_override(effect.target, replacement)
            return

    def _apply_effect(self, effect: TimedEffect) -> None:
        if effect.effect_type == "guard":
            self.character_manager.set_guarding(effect.owner, True, guarded_target=effect.target)
            return
        if effect.effect_type == "defense_bonus" and effect.target is not None:
            for defense_type, amount in effect.data.get("defense_bonus", {}).items():
                self.character_manager.add_defense_bonus(effect.target, defense_type, amount)
            return
        if effect.effect_type == "affinity_buff" and effect.target is not None:
            for damage_type, affinity in effect.data.get("affinity_changes", {}).items():
                self.character_manager.set_temporary_affinity(effect.target, damage_type, affinity)
            return
        if effect.effect_type == "defense_floor" and effect.target is not None:
            for defense_type, amount in effect.data.get("defense_floor", {}).items():
                self.character_manager.add_defense_floor(effect.target, defense_type, amount)
            return
        if effect.effect_type == "status_immunity" and effect.target is not None:
            for status in effect.data.get("status_immunities", ()):
                self.character_manager.add_status_immunity(effect.target, status)
                if effect.target in self.state.active_statuses:
                    self.state.active_statuses[effect.target] = [
                        active_status for active_status in self.state.active_statuses[effect.target] if active_status != status
                    ]
                    if not self.state.active_statuses[effect.target]:
                        del self.state.active_statuses[effect.target]
            return
        if effect.effect_type == "attribute_buff" and effect.target is not None:
            for attribute, steps in effect.data.get("attribute_bonus", {}).items():
                self.character_manager.add_attribute_bonus(effect.target, attribute, steps)
            return
        if effect.effect_type == "weapon_enchant" and effect.target is not None:
            self.character_manager.set_weapon_damage_type_override(effect.target, effect.data.get("weapon_damage_type"))

    def _latest_affinity_from_other_effects(self, current_effect: TimedEffect, damage_type: str):
        for effect in reversed(self.state.active_effects):
            if effect is current_effect:
                continue
            if effect.effect_type != "affinity_buff":
                continue
            if effect.target != current_effect.target:
                continue
            if damage_type in effect.data.get("affinity_changes", {}):
                return effect.data["affinity_changes"][damage_type]
        return None

    def _max_defense_floor_from_other_effects(self, current_effect: TimedEffect, defense_type: str) -> int:
        current_max = 0
        for effect in self.state.active_effects:
            if effect is current_effect:
                continue
            if effect.effect_type != "defense_floor":
                continue
            if effect.target != current_effect.target:
                continue
            current_max = max(current_max, effect.data.get("defense_floor", {}).get(defense_type, 0))
        return current_max

    def _status_immunities_from_other_effects(self, current_effect: TimedEffect) -> set[StatusEffect]:
        immunities: set[StatusEffect] = set()
        for effect in self.state.active_effects:
            if effect is current_effect:
                continue
            if effect.effect_type != "status_immunity":
                continue
            if effect.target != current_effect.target:
                continue
            immunities.update(effect.data.get("status_immunities", ()))
        return immunities

    def _latest_weapon_enchant_from_other_effects(self, current_effect: TimedEffect) -> str | None:
        for effect in reversed(self.state.active_effects):
            if effect is current_effect:
                continue
            if effect.effect_type != "weapon_enchant":
                continue
            if effect.target != current_effect.target:
                continue
            return effect.data.get("weapon_damage_type")
        return None

    def _queue_extra_action_if_needed(self, actor: str) -> None:
        action_count = self.state.enemy_action_counts.get(actor, 1)
        if action_count > 1:
            skipped_bonus_actions = self._consume_action_penalty(actor, action_count - 1)
            self.grant_bonus_turn(actor, action_count - 1 - skipped_bonus_actions)
        for effect in self.state.active_effects:
            if effect.effect_type != "extra_action":
                continue
            if effect.target != actor:
                continue
            remaining = effect.data.get("remaining_bonus_turns", 0)
            if remaining <= 0:
                continue
            self.grant_bonus_turn(actor, 1)
            effect.data["remaining_bonus_turns"] = remaining - 1

    def _consume_action_penalty(self, actor: str, maximum: int) -> int:
        if maximum <= 0:
            return 0
        current = self.state.action_penalties.get(actor, 0)
        consumed = min(current, maximum)
        if consumed <= 0:
            return 0
        remaining = current - consumed
        if remaining:
            self.state.action_penalties[actor] = remaining
        else:
            self.state.action_penalties.pop(actor, None)
        return consumed

    def _default_action_count(self, rank: EnemyRank) -> int:
        if rank == EnemyRank.ELITE:
            return 2
        if rank == EnemyRank.CHAMPION:
            raise ValueError("悍将必须显式提供 action_count，以表示它替代的小兵数量。")
        return 1

    def _is_available_combatant(self, actor_name: str) -> bool:
        return (
            self.character_manager.exists(actor_name)
            and actor_name not in self.state.defeated_combatants
            and actor_name not in self.state.escaped_combatants
            and actor_name not in self.state.surrendered_combatants
            and self.character_manager.get(actor_name).hp > 0
        )
