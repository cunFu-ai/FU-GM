from __future__ import annotations

from typing import Any, Protocol

from fu_gm.models import Action, ActionResolution, ActionType


class ActionTransactionHost(Protocol):
    TURN_CONSUMING_ACTIONS: set[ActionType]
    character_manager: Any
    decision_window_manager: Any
    conflict_manager: Any
    post_check_state: Any
    pending_check_transactions: Any
    post_check_decisions: Any
    action_dispatcher: Any
    _active_rule_action: Any

    @property
    def _replaying_check_transaction(self) -> bool: ...

    @_replaying_check_transaction.setter
    def _replaying_check_transaction(self, value: bool) -> None: ...

    @property
    def _check_transaction_candidate(self) -> dict[str, object] | None: ...

    @_check_transaction_candidate.setter
    def _check_transaction_candidate(self, value: dict[str, object] | None) -> None: ...

    def _build_check_transaction_candidate(self, action: Action) -> dict[str, object] | None: ...

    def _action_actor_name(self, action: Action) -> str: ...

    def _resolve_out_of_turn_action(self, action: Action) -> ActionResolution | None: ...

    def _finalize_resolution(self, resolution: ActionResolution) -> ActionResolution: ...

    def _normalized_skill_name(self, skill_name: str) -> str: ...

    def _validate_skill_action_followup(
        self,
        action: Action,
        *,
        after_commit: bool = False,
    ) -> None: ...


class ActionTransactionCoordinator:
    """Own the lifecycle around one hard-rules action transaction.

    Individual action handlers remain in the rules interceptor. This component
    is the single place that blocks unresolved choices, manages post-check
    rewind state, handles out-of-turn admission and commits the final result.
    """

    def __init__(self, host: ActionTransactionHost) -> None:
        self.host = host

    def resolve(self, action: Action) -> ActionResolution:
        host = self.host
        actor = host._action_actor_name(action)
        batch_roll_id = str(action.parameters.get("_check_batch_id") or "").strip()
        batch_roll = bool(action.parameters.get("_check_batch_roll") and batch_roll_id)
        internal_reaction = bool(action.parameters.get("_reaction_followup"))
        selected_window = None
        window_id = str(action.parameters.get("window_id") or "").strip()
        if window_id:
            selected_window = host.decision_window_manager.find_pending(
                window_id=window_id
            )
        post_check_modifier = bool(
            selected_window is not None
            and selected_window.kind == "skill_parameter"
            and str(
                selected_window.payload.get("label")
                or selected_window.payload.get("skill")
                or ""
            )
            == "予以信任"
        )
        if actor and actor in host.conflict_manager.state.sacrifices:
            raise ValueError(f"【{actor}】已经牺牲，不能再执行角色行动。")
        if (
            actor
            and host.character_manager.exists(actor)
            and "petrified"
            in host.character_manager.get(actor).special_conditions
        ):
            raise ValueError(f"【{actor}】已经石化，当前无法行动。")
        if (
            actor
            and actor in host.conflict_manager.state.fallen_pcs
            and action.action_type != ActionType.RESOLVE_ZERO_HP
        ):
            raise ValueError(
                f"【{actor}】已经放弃抵抗并失去意识；直到其参与的下一个场景开始前不能行动。"
            )
        blocking_windows = host.decision_window_manager.pending(
            blocking_only=True
        )
        batch_roll_may_continue = bool(
            batch_roll
            and blocking_windows
            and all(window.transaction_id == batch_roll_id for window in blocking_windows)
        )
        if (
            not host._replaying_check_transaction
            and blocking_windows
            and not batch_roll_may_continue
            and not internal_reaction
            and not self.resolves_blocking_decision(action)
        ):
            owner = blocking_windows[0].owner
            raise ValueError(f"先由【{owner}】处理刚才的规则选择，再结算新的场景行动。")
        host._validate_skill_action_followup(action)

        post_check_skill = bool(
            action.action_type == ActionType.SKILL
            and host._normalized_skill_name(
                str(action.parameters.get("skill_name") or "")
            )
            == "幸运七"
        )
        if (
            not host._replaying_check_transaction
            and action.action_type == ActionType.TRIGGER_OPPORTUNITY
        ):
            host.post_check_decisions.validate_opportunity_action(action)
        if (
            not host._replaying_check_transaction
            and action.action_type == ActionType.TRIGGER_OPPORTUNITY
            and not action.parameters.get("_preserve_compound_check_transaction")
        ):
            host.pending_check_transactions.clear()
            host.post_check_state.clear_roll_context()

        post_check_acceptance = bool(action.parameters.get("post_check_acceptance"))
        if (
            not host._replaying_check_transaction
            and action.action_type
            not in {
                ActionType.INVOKE_TRAIT,
                ActionType.INVOKE_BOND,
                ActionType.TRIGGER_OPPORTUNITY,
            }
            and not post_check_skill
            and not post_check_acceptance
            and not post_check_modifier
            and not batch_roll
            and not internal_reaction
        ):
            host.decision_window_manager.cancel_nonblocking(
                kinds={"trait_invocation", "bond_invocation"},
                reason="new_action_committed",
            )
            host.post_check_state.clear_roll_context()
            host.pending_check_transactions.clear()

        if not host._replaying_check_transaction:
            host.post_check_decisions.hydrate_for_action(action)
            host._check_transaction_candidate = host._build_check_transaction_candidate(action)

        out_of_turn = host._resolve_out_of_turn_action(action)
        if out_of_turn is not None:
            return host._finalize_resolution(out_of_turn)

        if (
            actor
            and host.conflict_manager.state.active
            and action.action_type in host.TURN_CONSUMING_ACTIONS
            and host.conflict_manager.state.current_actor() == actor
        ):
            started_actor = host.conflict_manager.begin_current_turn()
            if started_actor != actor:
                raise ValueError(
                    f"当前阵营行动槽已由【{started_actor or '其他角色'}】认领，"
                    f"不能结算【{actor}】的行动。"
                )
            if host.character_manager.get(actor).hp <= 0:
                return host._finalize_resolution(
                    ActionResolution(
                        action=action,
                        rules_text=f"【{actor}】在回合开始时失去战斗能力，原行动尚未执行。",
                        payload={
                            "action_uncommitted": True,
                            "turn_held_for_decision": host.decision_window_manager.has_blocking(),
                            "decision_windows": host.decision_window_manager.public_summary(),
                        },
                    )
                )

        active_action_token = host._active_rule_action.set(action)
        try:
            resolution = host.action_dispatcher.dispatch(action)
        finally:
            host._active_rule_action.reset(active_action_token)
        if resolution is None:
            resolution = ActionResolution(
                action=action,
                rules_text="该动作不需要执行硬规则。",
                payload={},
            )
        if (
            actor
            and host.conflict_manager.state.active
            and host.conflict_manager.state.current_actor() == actor
            and action.action_type in host.TURN_CONSUMING_ACTIONS
        ):
            consumed = host.conflict_manager.consume_held_actions_for_actor(actor)
            if consumed:
                resolution.payload["held_action_consumed"] = consumed
        return host._finalize_resolution(resolution)

    @staticmethod
    def resolves_blocking_decision(action: Action) -> bool:
        if (
            action.parameters.get("_acceleration_window_id")
            or action.parameters.get("_immediate_attack_window_id")
            or action.parameters.get("_skill_followup_window_id")
            or action.parameters.get("_reactive_check_window_id")
        ):
            return True
        if action.action_type in {
            ActionType.INVOKE_TRAIT,
            ActionType.INVOKE_BOND,
            ActionType.TRIGGER_OPPORTUNITY,
            ActionType.RESOLVE_ZERO_HP,
            ActionType.RESOLVE_DECISION,
        }:
            return True
        if action.action_type == ActionType.SKILL and action.parameters.get("window_id"):
            return True
        if action.action_type == ActionType.NARRATE and any(
            action.parameters.get(flag)
            for flag in (
                "post_check_acceptance",
                "pending_zero_hp_decision",
                "pending_skill_decision",
                "pending_spell_decision",
                "decision_window_guard",
            )
        ):
            return True
        return False
