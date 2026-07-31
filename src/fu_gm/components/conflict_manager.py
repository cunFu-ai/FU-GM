from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

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
from fu_gm.skill_library import has_skill_name

if TYPE_CHECKING:
    from fu_gm.components.decision_window_manager import DecisionWindowManager


class ConflictManager:
    def __init__(self, character_manager: CharacterManager) -> None:
        self.character_manager = character_manager
        self.state = ConflictState()
        self._ultima_spend_listeners: list[Callable[[str, int, str], None]] = []
        self._turn_start_listeners: list[Callable[[str, int], None]] = []
        self.decision_window_manager: DecisionWindowManager | None = None
        self.loyal_companion_manager: Any | None = None

    def bind_decision_window_manager(self, manager: "DecisionWindowManager") -> None:
        self.decision_window_manager = manager

    def bind_loyal_companion_manager(self, manager: Any) -> None:
        self.loyal_companion_manager = manager

    def register_ultima_spend_listener(self, listener: Callable[[str, int, str], None]) -> None:
        if listener not in self._ultima_spend_listeners:
            self._ultima_spend_listeners.append(listener)

    def register_turn_start_listener(self, listener: Callable[[str, int], None]) -> None:
        if listener not in self._turn_start_listeners:
            self._turn_start_listeners.append(listener)

    def _spend_ultima(self, name: str, amount: int, reason: str) -> None:
        if self.state.ultima_points.get(name, 0) < amount:
            raise ValueError(f"{name} 没有足够的终结点。")
        self.state.ultima_points[name] -= amount
        for listener in tuple(self._ultima_spend_listeners):
            listener(name, amount, reason)

    def start_scene(
        self,
        scene_name: str,
        turn_order: list[str],
        *,
        player_side: list[str] | None = None,
        enemy_side: list[str] | None = None,
        parent_scene_id: str = "",
        parent_scene_name: str = "",
        parent_scene_type: str = "",
        parent_scene_objective: str = "",
        parent_scene_summary: str = "",
    ) -> None:
        if self.loyal_companion_manager is not None:
            turn_order = [
                name
                for name in turn_order
                if not self.loyal_companion_manager.is_companion(name)
            ]
            if player_side is not None:
                player_side = [
                    name
                    for name in player_side
                    if not self.loyal_companion_manager.is_companion(name)
                ]
            if enemy_side is not None:
                enemy_side = [
                    name
                    for name in enemy_side
                    if not self.loyal_companion_manager.is_companion(name)
                ]
        carried_state = {
            "ultima_points": dict(self.state.ultima_points),
            "exalted_enemies": set(self.state.exalted_enemies),
            "enemy_ranks": dict(self.state.enemy_ranks),
            "villains": set(self.state.villains),
            "villain_appearance_awarded": set(),
            "enemy_action_counts": dict(self.state.enemy_action_counts),
            "escalation_stages": {name: list(stages) for name, stages in self.state.escalation_stages.items()},
            "current_escalation_stage": dict(self.state.current_escalation_stage),
            "fallen_pcs": dict(self.state.fallen_pcs),
            "sacrifices": set(self.state.sacrifices),
            "pc_defeat_consequences": {
                name: list(consequences)
                for name, consequences in self.state.pc_defeat_consequences.items()
            },
            "defeated_npc_fates": dict(self.state.defeated_npc_fates),
        }
        self._clear_all_timed_effects()
        resolved_player_side = list(
            dict.fromkeys(
                player_side
                if player_side is not None
                else [
                    name
                    for name in turn_order
                    if self._side_from_traits(name) != "enemy"
                ]
            )
        )
        resolved_enemy_side = list(
            dict.fromkeys(
                enemy_side
                if enemy_side is not None
                else [
                    name
                    for name in turn_order
                    if self._side_from_traits(name) == "enemy"
                ]
            )
        )
        self.state = ConflictState(
            active=True,
            scene_name=scene_name,
            parent_scene_id=parent_scene_id,
            parent_scene_name=parent_scene_name,
            parent_scene_type=parent_scene_type,
            parent_scene_objective=parent_scene_objective,
            parent_scene_summary=parent_scene_summary,
            round_number=1,
            turn_order=list(turn_order),
            player_side=resolved_player_side,
            enemy_side=resolved_enemy_side,
            current_turn_index=0,
            ultima_points=carried_state["ultima_points"],
            exalted_enemies=carried_state["exalted_enemies"],
            enemy_ranks=carried_state["enemy_ranks"],
            villains=carried_state["villains"],
            villain_appearance_awarded=carried_state["villain_appearance_awarded"],
            enemy_action_counts=carried_state["enemy_action_counts"],
            escalation_stages=carried_state["escalation_stages"],
            current_escalation_stage=carried_state["current_escalation_stage"],
            fallen_pcs=carried_state["fallen_pcs"],
            sacrifices=carried_state["sacrifices"],
            pc_defeat_consequences=carried_state["pc_defeat_consequences"],
            defeated_npc_fates=carried_state["defeated_npc_fates"],
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
        parent_scene_id: str = "",
        parent_scene_name: str = "",
        parent_scene_type: str = "",
        parent_scene_objective: str = "",
        parent_scene_summary: str = "",
    ) -> list[str]:
        turn_order = self.build_alternating_turn_order(
            player_side,
            enemy_side,
            players_first=players_first,
        )
        self.start_scene(
            scene_name,
            turn_order,
            player_side=player_side,
            enemy_side=enemy_side,
            parent_scene_id=parent_scene_id,
            parent_scene_name=parent_scene_name,
            parent_scene_type=parent_scene_type,
            parent_scene_objective=parent_scene_objective,
            parent_scene_summary=parent_scene_summary,
        )
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
        self.state.turn_serial += 1
        self.state.turn_started_actor = actor
        for listener in tuple(self._turn_start_listeners):
            listener(actor, self.state.turn_serial)
        return actor

    def end_current_turn(self) -> str | None:
        actor = self.state.turn_started_actor
        if actor is None:
            return None
        if self._pending_turn_end_window(actor) is not None:
            return actor
        if self._open_acceleration_window(actor):
            return actor
        return self._finalize_current_turn(actor)

    def _finalize_current_turn(self, actor: str) -> str:
        if self.state.current_bonus_actor is None:
            self._queue_rank_bonus_actions(actor)
            self._mark_acted(actor)
            self.state.pending_assists.pop(actor, None)
        if self.decision_window_manager is not None:
            self.decision_window_manager.cancel_nonblocking(
                kinds={"skill_parameter"},
                owner=actor,
                reason="turn_ended_without_using_optional_skill",
            )
        self._expire_effects(EffectTiming.OWNER_TURN_END, actor)
        self.state.turn_started_actor = None
        return actor

    def next_turn(self) -> str | None:
        if not self.state.turn_order and self.state.current_bonus_actor is None and not self.state.queued_turns:
            return None
        previous_bonus_actor = self.state.current_bonus_actor
        completed_actor = self.end_current_turn()

        # An end-of-turn choice is still part of the current actor's turn.  Do
        # not move initiative until that exact persisted window is resolved.
        if self.state.pending_turn_end_actor is not None:
            return self.state.current_actor()

        return self._advance_after_completed_turn(
            previous_bonus_actor,
            completed_actor=completed_actor,
        )

    def _advance_after_completed_turn(
        self,
        previous_bonus_actor: str | None,
        *,
        completed_actor: str | None,
    ) -> str | None:
        base_actor_removed = bool(self.state.current_base_actor_removed)
        removed_actor_ended_round = bool(
            self.state.current_base_actor_removed_ended_round
        )
        self.state.current_base_actor_removed = False
        self.state.current_base_actor_removed_ended_round = False
        if previous_bonus_actor is not None:
            self.state.current_bonus_actor = None

        queued = self._peek_next_queued_turn()
        next_base_actor = self._next_base_actor(
            base_actor_removed=base_actor_removed,
        )
        if self._rank_turn_must_yield(
            completed_actor=completed_actor,
            queued=queued,
            next_base_actor=next_base_actor,
        ):
            if not base_actor_removed:
                self._advance_base_turn()
            return self.begin_current_turn()

        queued_actor = self._pop_next_queued_turn()
        if queued_actor is not None:
            self.state.current_bonus_actor = queued_actor
        else:
            if not base_actor_removed:
                self._advance_base_turn()
            elif removed_actor_ended_round and self.state.turn_order:
                self._complete_round_boundary()
        return self.begin_current_turn()

    def complete_acceleration_turn_end(
        self,
        actor: str,
        *,
        benefit_used: bool,
        effect_key: str = "",
    ) -> dict[str, object]:
        """Commit one Acceleration choice and resume initiative atomically."""

        pending_actor = str(self.state.pending_turn_end_actor or "")
        if pending_actor != actor or self.state.turn_started_actor != actor:
            raise ValueError(f"【{actor}】当前没有等待处理的【加速术】回合末选择。")

        effect = self._acceleration_effect(actor, effect_key=effect_key)
        if effect is None:
            raise ValueError(f"【{actor}】身上的【加速术】效果已经结束。")

        benefits_used = int(effect.data.get("benefits_used", 0) or 0)
        max_benefits = max(1, int(effect.data.get("max_benefits", 2) or 2))
        expired = False
        if benefit_used:
            benefits_used += 1
            effect.data["benefits_used"] = benefits_used
            effect.data["remaining_bonus_turns"] = max(0, max_benefits - benefits_used)
            if benefits_used >= max_benefits:
                self.clear_effects(
                    effect.owner,
                    effect.effect_type,
                    effect.effect_key or None,
                    effect.target,
                )
                expired = True

        self.state.pending_turn_end_actor = None
        self._finalize_current_turn(actor)
        return {
            "actor": actor,
            "benefit_used": benefit_used,
            "benefits_used": benefits_used,
            "max_benefits": max_benefits,
            "effect_expired": expired,
            "turn_ready_to_advance": True,
        }

    def grant_bonus_turn(
        self,
        actor_name: str,
        count: int = 1,
        *,
        source: str = "bonus",
    ) -> None:
        if count <= 0:
            return
        for _ in range(count):
            if self._is_available_combatant(actor_name):
                self.state.queued_turns.append(actor_name)
                self.state.queued_turn_kinds.append(str(source or "bonus"))

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

    def register_held_action(
        self,
        actor_name: str,
        action_type: str,
        summary: str,
        *,
        speaker: str = "",
        action_parameters: dict | None = None,
    ) -> dict[str, object]:
        # A character has only one ordinary action to confirm when their turn
        # arrives. A newer declaration replaces the older draft instead of
        # building an ambiguous stack that would be repeated next round.
        self.state.held_actions = [
            item
            for item in self.state.held_actions
            if item.get("actor") != actor_name
        ]
        entry = {
            "round_number": self.state.round_number,
            "actor": actor_name,
            "action_type": action_type,
            "summary": summary,
            "speaker": speaker,
            "action_parameters": dict(action_parameters or {}),
        }
        self.state.held_actions.append(entry)
        self.state.held_actions = self.state.held_actions[-20:]
        if self.decision_window_manager is not None:
            window = self.decision_window_manager.create(
                kind="held_action",
                owner=actor_name,
                prompt=f"轮到你时确认、修改或放弃这条缓存行动：{summary}",
                options=[
                    {"choice": "confirm", "label": "按原意执行"},
                    {"choice": "revise", "label": "修改行动"},
                    {"choice": "discard", "label": "放弃缓存"},
                ],
                scope_kind="conflict",
                scope_id=self.state.scene_name,
                blocking=False,
                action_type=action_type,
                payload=dict(entry),
                dedupe_key=f"held_action:{self.state.scene_name}:{actor_name}",
            )
            entry["window_id"] = window.window_id
        self.record_log(actor_name, "held_action", f"{actor_name} 的回合外动作已暂缓：{summary}")
        return entry

    def held_actions_for_actor(self, actor_name: str) -> list[dict[str, object]]:
        return [entry for entry in self.state.held_actions if entry.get("actor") == actor_name]

    def consume_held_actions_for_actor(self, actor_name: str) -> list[dict[str, object]]:
        matched = self.held_actions_for_actor(actor_name)
        if not matched:
            return []
        self.state.held_actions = [entry for entry in self.state.held_actions if entry.get("actor") != actor_name]
        if self.decision_window_manager is not None:
            for window in self.decision_window_manager.pending(kind="held_action", owner=actor_name):
                self.decision_window_manager.resolve(
                    window_id=window.window_id,
                    responder=actor_name,
                    resolution={"choice": "acted_on_turn"},
                )
        return matched

    def withdraw_held_action(
        self,
        actor_name: str,
        *,
        window_id: str,
        choice: str,
    ) -> dict[str, object]:
        """Withdraw one cached draft without consuming the actor's turn."""

        if choice not in {"discard", "revise"}:
            raise ValueError("缓存行动只能选择放弃或修改；确认时应提交原本的实际行动。")
        matched = next(
            (
                entry
                for entry in self.state.held_actions
                if entry.get("actor") == actor_name
                and str(entry.get("window_id") or "") == str(window_id or "")
            ),
            None,
        )
        if matched is None:
            raise ValueError(f"【{actor_name}】没有与这个窗口对应的缓存行动。")
        self.state.held_actions = [
            entry
            for entry in self.state.held_actions
            if entry is not matched
        ]
        if self.decision_window_manager is not None:
            self.decision_window_manager.resolve(
                window_id=window_id,
                responder=actor_name,
                resolution={"choice": choice},
            )
        event_type = "held_action_discarded" if choice == "discard" else "held_action_revised"
        self.record_log(
            actor_name,
            event_type,
            f"{actor_name}放弃了先前缓存的行动。"
            if choice == "discard"
            else f"{actor_name}准备修改先前缓存的行动。",
        )
        return dict(matched)

    def remove_combatant_from_scene(self, target: str, *, as_escaped: bool = True) -> None:
        if as_escaped:
            self.state.escaped_combatants.add(target)
        else:
            self.state.defeated_combatants.add(target)
        self._remove_from_turn_order(target)

    def surrender_combatant(self, target: str) -> None:
        self.state.surrendered_combatants.add(target)
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
        self._spend_ultima(villain_name, 1, "援用反派特质")
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

    def next_escalation_stage(self, target: str) -> EscalationStage | None:
        stages = self.state.escalation_stages.get(target, [])
        next_index = self.state.current_escalation_stage.get(target, -1) + 1
        if next_index < 0 or next_index >= len(stages):
            return None
        return stages[next_index]

    def resolution_status(self) -> dict[str, object]:
        """Describe whether one side has naturally run out of combatants.

        This is advisory rather than an automatic scene ending. A conflict may
        also finish through surrender, withdrawal, negotiation or a completed
        objective while members of both sides are still present.
        """

        pending_zero_hp = {
            str(decision.get("target") or "")
            for decision in self.state.pending_decisions
            if decision.get("kind") == "zero_hp"
            and decision.get("status") == "pending"
            and str(decision.get("target") or "")
        }
        if self.decision_window_manager is not None:
            pending_zero_hp.update(
                window.owner
                for window in self.decision_window_manager.pending(
                    kind="zero_hp",
                    blocking_only=True,
                )
                if window.owner
            )
        active_names = [
            name
            for name in dict.fromkeys(
                [
                    *self.state.turn_order,
                    *self.state.queued_turns,
                    *(
                        [self.state.current_bonus_actor]
                        if self.state.current_bonus_actor
                        else []
                    ),
                ]
            )
            if self.character_manager.exists(name)
            and self.character_manager.get(name).hp > 0
            and name not in self.state.defeated_combatants
            and name not in self.state.escaped_combatants
            and name not in self.state.surrendered_combatants
        ]
        active_player_side = [
            name
            for name in active_names
            if self._combat_side(name) == "player"
        ]
        active_pcs = [
            name
            for name in active_player_side
            if "pc" in self.character_manager.get(name).traits
        ]
        active_allied_npcs = [
            name
            for name in active_player_side
            if "pc" not in self.character_manager.get(name).traits
        ]
        active_hostiles = [
            name
            for name in active_names
            if self._combat_side(name) == "enemy"
        ]
        if not self.state.active:
            natural_outcome = "inactive"
            ready = False
        elif pending_zero_hp:
            natural_outcome = "pending_player_defeat_choice"
            ready = False
        elif active_player_side and not active_hostiles:
            natural_outcome = "hostile_side_removed"
            ready = True
        elif active_hostiles and not active_player_side:
            natural_outcome = "player_side_removed"
            ready = True
        elif not active_player_side and not active_hostiles:
            natural_outcome = "no_active_sides"
            ready = True
        else:
            natural_outcome = "both_sides_active"
            ready = False
        return {
            "ready_for_natural_end": ready,
            "natural_outcome": natural_outcome,
            "active_player_side": active_player_side,
            "active_player_characters": active_pcs,
            "active_allied_npcs": active_allied_npcs,
            "active_hostiles": active_hostiles,
            "defeated_combatants": sorted(
                self.state.defeated_combatants
            ),
            "escaped_combatants": sorted(
                self.state.escaped_combatants
            ),
            "surrendered_combatants": sorted(
                self.state.surrendered_combatants
            ),
            "pending_zero_hp_characters": sorted(pending_zero_hp),
            "note": (
                "双方仍在场时，只有已经成立的谈判、撤离或目标结局"
                "才能由GM结束冲突。"
            ),
        }

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
        if has_skill_name(character.hero_skills, "坚不可摧") and target not in self.state.passive_survival_used:
            character.hp = 1
            self.state.passive_survival_used.add(target)
            return True
        return False

    def end_scene(self, participants: list[str] | None = None) -> None:
        self.clear_scene_effects(participants)
        self.state.active = False
        self.state.scene_name = ""
        self.state.parent_scene_id = ""
        self.state.parent_scene_name = ""
        self.state.parent_scene_type = ""
        self.state.parent_scene_objective = ""
        self.state.parent_scene_summary = ""
        self.state.round_number = 0
        self.state.turn_order = []
        self.state.player_side = []
        self.state.enemy_side = []
        self.state.current_turn_index = 0
        self.state.current_bonus_actor = None
        self.state.queued_turns = []
        self.state.queued_turn_kinds = []
        self.state.turn_started_actor = None
        self.state.current_base_actor_removed = False
        self.state.current_base_actor_removed_ended_round = False
        self.state.pending_turn_end_actor = None
        self.state.acted_this_round = []
        self.state.pending_assists = {}
        self.state.held_actions = []
        self.state.pending_decisions = []
        if self.decision_window_manager is not None:
            self.decision_window_manager.cancel_matching(
                scope_kind="conflict",
                reason="conflict_ended",
            )

    def clear_scene_effects(self, participants: list[str] | None = None) -> None:
        """Clear effects whose duration is the current scene, even outside combat."""

        if participants is None:
            self._clear_all_timed_effects()
            return
        names = {str(item or "").strip() for item in participants if str(item or "").strip()}
        if not names:
            self._clear_all_timed_effects()
            return
        remaining_effects: list[TimedEffect] = []
        for effect in self.state.active_effects:
            if effect.owner in names or str(effect.target or "") in names:
                self._cleanup_effect(effect)
                continue
            remaining_effects.append(effect)
        self.state.active_effects = remaining_effects

    def request_zero_hp_decision(
        self,
        target: str,
        *,
        consequence: str = "",
        source_actor: str = "",
    ) -> ConflictEvent:
        """Hold a PC at zero HP until their player chooses the rulebook outcome."""

        if self.decision_window_manager is not None:
            window = self.decision_window_manager.create(
                kind="zero_hp",
                owner=target,
                prompt="选择牺牲，或放弃抵抗并承受当前局势的严重后果。",
                options=[
                    {"choice": "sacrifice", "label": "牺牲"},
                    {"choice": "give_up_resistance", "label": "放弃抵抗"},
                ],
                scope_kind="conflict",
                scope_id=self.state.scene_name,
                blocking=True,
                action_type="ResolveZeroHP",
                payload={
                    "source_actor": source_actor,
                    "suggested_consequence": consequence,
                    "deferred_turn_actor": self.state.current_actor() or "",
                    "deferred_turn_serial": self.state.turn_serial,
                },
                dedupe_key=f"zero_hp:{self.state.scene_name}:{target}",
            )
        else:
            # Standalone ConflictManager users still get a functional local
            # choice, but the application path has one authoritative window.
            existing = next(
                (
                    decision
                    for decision in self.state.pending_decisions
                    if decision.get("kind") == "zero_hp" and decision.get("target") == target
                ),
                None,
            )
            if existing is None:
                self.state.pending_decisions.append(
                    {
                        "kind": "zero_hp",
                        "target": target,
                        "source_actor": source_actor,
                        "suggested_consequence": consequence,
                        "deferred_turn_actor": self.state.current_actor() or "",
                        "deferred_turn_serial": self.state.turn_serial,
                        "choices": ["sacrifice", "give_up_resistance"],
                        "status": "pending",
                    }
                )
        event = ConflictEvent(
            target=target,
            event_type="pc_zero_hp_choice_required",
            summary=f"{target} 的生命值降为 0；由玩家选择牺牲，或放弃抵抗并承受后果。",
            hp_after=self.character_manager.get(target).hp,
        )
        self.record_log(target, event.event_type, event.summary)
        return event

    def pending_zero_hp_decision(self, target: str = "") -> dict[str, object] | None:
        if self.decision_window_manager is not None:
            window = self.decision_window_manager.find_pending(kind="zero_hp", owner=target)
            if window is None and not target:
                pending = self.decision_window_manager.pending(kind="zero_hp")
                window = pending[0] if pending else None
            if window is not None:
                return {
                    "kind": "zero_hp",
                    "target": window.owner,
                    "source_actor": window.payload.get("source_actor", ""),
                    "suggested_consequence": window.payload.get("suggested_consequence", ""),
                    "deferred_turn_actor": window.payload.get(
                        "deferred_turn_actor", ""
                    ),
                    "deferred_turn_serial": window.payload.get(
                        "deferred_turn_serial", 0
                    ),
                    "choices": [option.get("choice") for option in window.options],
                    "status": "pending",
                    "window_id": window.window_id,
                }
        for decision in self.state.pending_decisions:
            if decision.get("kind") != "zero_hp" or decision.get("status") != "pending":
                continue
            if target and decision.get("target") != target:
                continue
            return decision
        return None

    def resolve_pending_zero_hp(
        self,
        target: str,
        *,
        choice: str,
        consequence: str = "",
        sacrifice_benefits_bond: bool | None = None,
        sacrifice_betters_world: bool | None = None,
        require_all_sacrifice_conditions: bool = False,
    ) -> ConflictEvent:
        decision = self.pending_zero_hp_decision(target)
        if decision is None:
            raise ValueError(f"{target} 当前没有等待处理的生命值归零选择。")
        resolved_consequence = consequence or str(
            decision.get("suggested_consequence") or ""
        )
        event = self.resolve_zero_hp(
            target,
            pc_choice=choice,
            pc_consequence=resolved_consequence,
            sacrifice_benefits_bond=sacrifice_benefits_bond,
            sacrifice_betters_world=sacrifice_betters_world,
            require_all_sacrifice_conditions=require_all_sacrifice_conditions,
        )
        if self.decision_window_manager is not None:
            window_id = str(decision.get("window_id") or "")
            window = self.decision_window_manager.find_pending(
                window_id=window_id,
                kind="zero_hp",
                owner=target,
            )
            if window is not None:
                self.decision_window_manager.resolve(
                    window_id=window.window_id,
                    responder=target,
                    resolution={"choice": choice, "consequence": consequence},
                )
        else:
            self.state.pending_decisions = [
                item
                for item in self.state.pending_decisions
                if not (
                    item.get("kind") == "zero_hp"
                    and item.get("target") == target
                    and item.get("status") == "pending"
                )
            ]
        return event

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
        self._spend_ultima(target, 1, "恢复精神值并解除异常状态")
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
        pc_choice: str = "",
        pc_consequence: str = "",
        source_actor: str = "",
        villain_mode: str = "auto",
        allow_escalation: bool = True,
        sacrifice_benefits_bond: bool | None = None,
        sacrifice_betters_world: bool | None = None,
        require_all_sacrifice_conditions: bool = False,
    ) -> ConflictEvent:
        character = self.character_manager.get(target)
        is_pc = "pc" in character.traits
        is_villain = self.is_villain(target)

        if is_pc:
            if target in self.state.sacrifices:
                raise ValueError(f"{target}已经牺牲，不能再次处理生命值归零。")
            if target in self.state.fallen_pcs:
                raise ValueError(f"{target}已经放弃抵抗，不能重复处理生命值归零。")
            if not pc_choice:
                return self.request_zero_hp_decision(target, consequence=pc_consequence)
            if (
                self.decision_window_manager is not None
                and self.pending_zero_hp_decision(target) is None
            ):
                raise ValueError("必须先建立并保留生命值归零待决窗口，再由该玩家提交选择。")
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
                required_conditions = 3 if require_all_sacrifice_conditions else 2
                if sacrifice_conditions < required_conditions:
                    if require_all_sacrifice_conditions:
                        raise ValueError(
                            "最终编年史组织化章节模式下，玩家角色牺牲必须同时满足：场景中存在反派、"
                            "牺牲会使羁绊对象受益、且角色相信牺牲会让世界更好。"
                        )
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

            clean_consequence = str(pc_consequence or "").strip()
            if not clean_consequence:
                raise ValueError("放弃抵抗时，GM必须依据当前场景给出一项明确后果。")
            before, after = self.character_manager.modify_resource(target, "fabula_points", 2)
            self.state.fallen_pcs[target] = clean_consequence
            consequences = self.state.pc_defeat_consequences.setdefault(target, [])
            if clean_consequence not in consequences:
                consequences.append(clean_consequence)
            self.state.defeated_combatants.add(target)
            self._remove_from_turn_order(target)
            event = ConflictEvent(
                target=target,
                event_type="pc_give_up_resistance",
                summary=f"{target} 选择放弃抵抗，活了下来，但必须承受沉重代价。",
                fabula_awarded=after - before,
                consequence=clean_consequence,
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        if (
            self.loyal_companion_manager is not None
            and self.loyal_companion_manager.is_companion(target)
        ):
            self.state.defeated_combatants.add(target)
            self._remove_from_turn_order(target)
            self.clear_effects(target)
            self.loyal_companion_manager.mark_retreated(target)
            event = ConflictEvent(
                target=target,
                event_type="loyal_companion_retreat",
                summary=(
                    f"{target}失去战斗力并离开当前场景；"
                    "它会在主人的下一个场景开始时归队。"
                ),
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        next_stage = self.next_escalation_stage(target)
        if (
            is_villain
            and allow_escalation
            and villain_mode != "surrender"
            and next_stage is not None
            and next_stage.transition_kind == "boss_phase"
        ):
            escalation = self.try_escalate(target)
            if escalation is not None:
                return escalation

        if is_villain and self.state.ultima_points.get(target, 0) > 0 and villain_mode != "surrender":
            self._spend_ultima(target, 1, "生命值归零时逃脱")
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
        if (
            self.decision_window_manager is not None
            and source_actor
            and self.character_manager.exists(source_actor)
            and "pc" in self.character_manager.get(source_actor).traits
        ):
            self.decision_window_manager.create(
                kind="npc_fate",
                owner=source_actor,
                prompt=f"【{target}】的生命值降为0；由造成最后一击的玩家决定其命运。",
                options=[
                    {"choice": "spare", "label": "饶恕"},
                    {"choice": "capture", "label": "俘虏"},
                    {"choice": "drive_off", "label": "驱逐"},
                    {"choice": "unconscious", "label": "击昏"},
                    {"choice": "kill", "label": "杀死"},
                    {"choice": "other", "label": "其他合适处置"},
                ],
                scope_kind="conflict",
                scope_id=self.state.scene_name,
                blocking=True,
                action_type="ResolveDecision",
                payload={
                    "target": target,
                    "source_actor": source_actor,
                    "deferred_turn_actor": self.state.current_actor() or "",
                    "deferred_turn_serial": self.state.turn_serial,
                },
                dedupe_key=f"npc_fate:{self.state.scene_name}:{target}",
            )
            event = ConflictEvent(
                target=target,
                event_type="npc_fate_choice_required",
                summary=f"{target}失去战斗力；由【{source_actor}】的玩家决定其命运。",
                hp_after=character.hp,
            )
            self.record_log(target, event.event_type, event.summary)
            return event

        fallback_fate = "失去战斗力，后续命运由GM依据局势决定"
        self.state.defeated_npc_fates[target] = fallback_fate
        event = ConflictEvent(
            target=target,
            event_type="enemy_defeated",
            summary=f"{target} 倒下并失去战斗力。",
            hp_after=character.hp,
        )
        self.record_log(target, event.event_type, event.summary)
        return event

    def resolve_pending_npc_fate(
        self,
        *,
        window_id: str,
        responder: str,
        choice: str,
        fate_description: str = "",
    ) -> ConflictEvent:
        if self.decision_window_manager is None:
            raise ValueError("当前没有可持久化的NPC命运待决窗口。")
        window = self.decision_window_manager.find_pending(
            window_id=window_id,
            kind="npc_fate",
            owner=responder,
        )
        if window is None:
            raise ValueError("这个NPC命运选择已经结束，或不属于当前玩家。")
        legal_choices = {
            str(option.get("choice") or "").strip()
            for option in window.options
        }
        if choice not in legal_choices:
            raise ValueError("所选处置不在这个NPC命运窗口的合法选项中。")
        target = str(window.payload.get("target") or "").strip()
        if not target:
            raise ValueError("NPC命运窗口缺少目标，无法提交。")
        labels = {
            "spare": "被饶恕",
            "capture": "被俘虏",
            "drive_off": "被驱逐",
            "unconscious": "被击昏并留在现场",
            "kill": "被杀死",
        }
        clean_description = str(fate_description or "").strip()
        if choice == "other" and not clean_description:
            raise ValueError("选择其他处置时，必须说明这个NPC具体遭遇了什么。")
        fate = clean_description or labels[choice]
        self.state.defeated_npc_fates[target] = fate
        self.decision_window_manager.resolve(
            window_id=window.window_id,
            responder=responder,
            resolution={
                "choice": choice,
                "target": target,
                "fate": fate,
            },
        )
        event = ConflictEvent(
            target=target,
            event_type="npc_fate_resolved",
            summary=f"【{responder}】决定了【{target}】的命运：{fate}。",
            consequence=fate,
            hp_after=self.character_manager.get(target).hp,
        )
        self.record_log(responder, event.event_type, event.summary)
        return event

    def try_escalate(self, target: str) -> ConflictEvent | None:
        stages = self.state.escalation_stages.get(target, [])
        current_index = self.state.current_escalation_stage.get(target, -1)
        next_index = current_index + 1
        if next_index >= len(stages):
            return None

        stage = stages[next_index]
        is_boss_phase = stage.transition_kind == "boss_phase"
        self.state.current_escalation_stage[target] = next_index
        if not is_boss_phase:
            self.state.exalted_enemies.add(target)
        self.state.villains.add(target)
        if not is_boss_phase:
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
        if is_boss_phase:
            if stage.preparation_round:
                for name in self.state.turn_order:
                    if (
                        self.character_manager.exists(name)
                        and "pc" in self.character_manager.get(name).traits
                    ):
                        self.grant_bonus_turn(
                            name,
                            source="phase_preparation",
                        )
            summary = (
                f"{target} 进入【{stage.name}】阶段，生命值与精神值恢复。"
            )
            if stage.preparation_round:
                summary += " 所有仍能行动的英雄各获得一次准备行动。"
        else:
            for character in self.character_manager.all():
                if "pc" not in character.traits:
                    continue
                before, after = self.character_manager.modify_resource(
                    character.name,
                    "fabula_points",
                    1,
                )
                fabula_awarded += after - before
            summary = (
                f"{target} 升格至【{stage.name}】阶段，终结点补满并变得更强。"
                "所有玩家角色获得 1 点物语点。"
            )
        if stage.public_cue:
            summary += f" {stage.public_cue}"
        event = ConflictEvent(
            target=target,
            event_type="boss_phase" if is_boss_phase else "escalation",
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
        self.state.held_actions = [
            item
            for item in self.state.held_actions
            if str(item.get("actor") or "") != target
        ]
        if self.decision_window_manager is not None:
            self.decision_window_manager.cancel_matching(
                kind="held_action",
                owner=target,
                reason="combatant_left_conflict",
            )
        queued = list(self.state.queued_turns)
        kinds = list(self.state.queued_turn_kinds)
        if len(kinds) < len(queued):
            kinds.extend(["bonus"] * (len(queued) - len(kinds)))
        kept = [
            (name, kinds[index])
            for index, name in enumerate(queued)
            if name != target
        ]
        self.state.queued_turns = [name for name, _ in kept]
        self.state.queued_turn_kinds = [kind for _, kind in kept]
        self.state.acted_this_round = [name for name in self.state.acted_this_round if name != target]
        self.state.pending_assists = {
            leader: [name for name in helpers if name != target]
            for leader, helpers in self.state.pending_assists.items()
            if leader != target
        }
        if (
            self.state.current_bonus_actor == target
            and self.state.turn_started_actor != target
        ):
            self.state.current_bonus_actor = None
        if target not in self.state.turn_order:
            return
        old_order = list(self.state.turn_order)
        old_index = self.state.current_turn_index % len(old_order)
        base_actor = old_order[old_index]
        target_index = old_order.index(target)
        removed_current_base = target == base_actor
        removed_last_slot = (
            removed_current_base and target_index == len(old_order) - 1
        )
        self.state.turn_order = [name for name in old_order if name != target]
        if not self.state.turn_order:
            self.state.current_turn_index = 0
            return
        if removed_current_base:
            self.state.current_base_actor_removed = True
            self.state.current_base_actor_removed_ended_round = removed_last_slot
            self.state.current_turn_index = min(
                target_index,
                len(self.state.turn_order) - 1,
            )
            return
        if target_index < old_index:
            self.state.current_turn_index = old_index - 1
        else:
            self.state.current_turn_index = old_index

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
            self._complete_round_boundary()

    def _complete_round_boundary(self) -> None:
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
            if self.state.queued_turn_kinds:
                self.state.queued_turn_kinds.pop(0)
            if self._is_available_combatant(actor_name):
                return actor_name
        return None

    def _peek_next_queued_turn(self) -> tuple[str, str] | None:
        while self.state.queued_turns:
            actor_name = self.state.queued_turns[0]
            kind = (
                self.state.queued_turn_kinds[0]
                if self.state.queued_turn_kinds
                else "bonus"
            )
            if self._is_available_combatant(actor_name):
                return actor_name, kind
            self.state.queued_turns.pop(0)
            if self.state.queued_turn_kinds:
                self.state.queued_turn_kinds.pop(0)
        return None

    def _next_base_actor(self, *, base_actor_removed: bool) -> str | None:
        if not self.state.turn_order:
            return None
        if base_actor_removed:
            index = self.state.current_turn_index % len(self.state.turn_order)
        else:
            index = (self.state.current_turn_index + 1) % len(self.state.turn_order)
        return self.state.turn_order[index]

    def _rank_turn_must_yield(
        self,
        *,
        completed_actor: str | None,
        queued: tuple[str, str] | None,
        next_base_actor: str | None,
    ) -> bool:
        if not completed_actor or queued is None or not next_base_actor:
            return False
        queued_actor, queued_kind = queued
        if queued_kind != "rank":
            return False
        # Crossing the round boundary is not a reason to postpone an enemy's
        # remaining rank actions: PCs already listed in ``acted_this_round``
        # have had their chance, so surplus champion actions may now be
        # consecutive as the rule permits.
        if next_base_actor in self.state.acted_this_round:
            return False
        return (
            self._combat_side(completed_actor) == self._combat_side(queued_actor)
            and self._combat_side(next_base_actor) != self._combat_side(queued_actor)
        )

    def _combat_side(self, actor_name: str) -> str:
        if actor_name in self.state.enemy_side:
            return "enemy"
        if actor_name in self.state.player_side:
            return "player"
        return self._side_from_traits(actor_name)

    def combat_side(self, actor_name: str) -> str:
        """Return the authoritative conflict side for one combatant."""

        return self._combat_side(actor_name)

    def _side_from_traits(self, actor_name: str) -> str:
        if not self.character_manager.exists(actor_name):
            return "unknown"
        traits = set(self.character_manager.get(actor_name).traits)
        return "enemy" if traits & {"enemy", "villain"} else "player"

    def _expire_effects(self, timing: EffectTiming, owner: str | None = None) -> None:
        remaining_effects = []
        for effect in self.state.active_effects:
            matches_timing = effect.expires_on == timing
            matches_owner = owner is None or effect.owner == owner
            earliest_serial = int(effect.data.get("expire_after_turn_serial", 0) or 0)
            if matches_timing and matches_owner and self.state.turn_serial < earliest_serial:
                remaining_effects.append(effect)
                continue
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

    def _queue_rank_bonus_actions(self, actor: str) -> None:
        action_count = self.state.enemy_action_counts.get(actor, 1)
        if action_count > 1:
            skipped_bonus_actions = self._consume_action_penalty(actor, action_count - 1)
            self.grant_bonus_turn(
                actor,
                action_count - 1 - skipped_bonus_actions,
                source="rank",
            )

    def _open_acceleration_window(self, actor: str) -> bool:
        effect = self._acceleration_effect(actor)
        if effect is None or self.decision_window_manager is None:
            return False

        max_spell_mp = max(0, int(effect.data.get("max_spell_mp", 10) or 10))
        effect.data.setdefault("benefits_used", 0)
        effect.data.setdefault("max_benefits", 2)
        effect.data.setdefault("max_spell_mp", max_spell_mp)
        # ``remaining_bonus_turns`` came from the legacy implementation. Keep
        # it synchronized only as save-file compatibility metadata; it no
        # longer queues a generic turn.
        effect.data["remaining_bonus_turns"] = max(
            0,
            int(effect.data["max_benefits"]) - int(effect.data["benefits_used"]),
        )
        window = self.decision_window_manager.create(
            kind="acceleration_benefit",
            owner=actor,
            prompt=(
                f"【加速术】在【{actor}】回合结束时触发：可以使用装备武器进行一次顺势攻击，"
                f"或顺势施放总精神值消耗不高于 {max_spell_mp} 点的法术；也可以本回合不发动。"
            ),
            options=[
                {"choice": "attack", "label": "使用装备武器顺势攻击"},
                {
                    "choice": "cast_spell",
                    "label": f"顺势施放总精神值消耗不高于{max_spell_mp}点的法术",
                    "max_mp_cost": max_spell_mp,
                },
                {"choice": "decline", "label": "本回合不发动"},
            ],
            scope_kind="conflict",
            scope_id=self.state.scene_name,
            blocking=True,
            action_type="ResolveDecision",
            resume_point="conflict_turn_end",
            payload={
                "spell": "加速术",
                "effect_key": effect.effect_key,
                "max_spell_mp": max_spell_mp,
                "benefits_used": int(effect.data.get("benefits_used", 0) or 0),
                "max_benefits": int(effect.data.get("max_benefits", 2) or 2),
            },
            dedupe_key=f"acceleration:{self.state.scene_name}:{actor}:{self.state.turn_serial}",
        )
        self.state.pending_turn_end_actor = actor
        self.record_log(
            actor,
            "acceleration_choice_opened",
            f"{actor} 的回合结束，等待处理【加速术】（窗口 {window.window_id}）。",
        )
        return True

    def _pending_turn_end_window(self, actor: str):
        if self.state.pending_turn_end_actor != actor:
            return None
        if self.decision_window_manager is None:
            self.state.pending_turn_end_actor = None
            return None
        window = self.decision_window_manager.find_pending(
            kind="acceleration_benefit",
            owner=actor,
        )
        if window is None:
            self.state.pending_turn_end_actor = None
        return window

    def _acceleration_effect(self, actor: str, *, effect_key: str = "") -> TimedEffect | None:
        for effect in self.state.active_effects:
            if effect.target != actor:
                continue
            if effect_key and effect.effect_key != effect_key:
                continue
            if effect.effect_type not in {"acceleration", "extra_action"}:
                continue
            if effect.effect_type == "extra_action" and effect.source not in {"加速", "加速术"}:
                continue
            # Upgrade old saves in place. The old counter represented generic
            # queued turns; the rule actually counts benefits already taken.
            if effect.effect_type == "extra_action":
                remaining = max(0, int(effect.data.get("remaining_bonus_turns", 2) or 0))
                effect.effect_type = "acceleration"
                effect.data.setdefault("max_benefits", 2)
                effect.data.setdefault("benefits_used", max(0, 2 - remaining))
                effect.data.setdefault("max_spell_mp", 10)
            return effect
        return None

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
