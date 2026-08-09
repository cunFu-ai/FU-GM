from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.check_transaction_manager import CheckTransactionManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.post_check_window_manager import PostCheckWindowManager
from fu_gm.components.post_check_state_journal import PostCheckStateJournal
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.skill_lifecycle_coordinator import (
    SkillLifecycleCoordinator,
    SkillLifecycleOutcome,
)
from fu_gm.components.skill_trigger_manager import SkillTriggerManager
from fu_gm.models import Action, ActionResolution, ActionType, DecisionWindowStatus


class PostCheckDecisionCoordinator:
    """Bridge one resolved check to persisted decisions and replay state.

    Rules components produce a roll; this coordinator opens the legal player
    choices, marks their shared transaction, and rebuilds a failed provisional
    check after save/load. It intentionally does not render player-facing text.
    """

    _REPLACED_KINDS = (
        "critical_opportunity",
        "fumble_opportunity",
        "trait_invocation",
        "bond_invocation",
        "skill_judgement",
    )
    _RESUMABLE_KINDS = {"trait_invocation", "bond_invocation"}
    FAILED_CHECK_GRACE_SECONDS = 15

    @staticmethod
    def _is_pre_final_window(window: dict[str, object]) -> bool:
        kind = str(window.get("kind") or "")
        if kind in {"trait_invocation", "bond_invocation"}:
            return True
        label = str(window.get("label") or window.get("skill") or "")
        if kind == "skill_judgement" and label == "幸运七":
            return True
        return kind == "skill_parameter" and label == "予以信任"

    @staticmethod
    def _is_pre_final_decision(window) -> bool:
        if window.kind in {"trait_invocation", "bond_invocation"}:
            return True
        label = str(window.payload.get("label") or window.payload.get("skill") or "")
        if window.kind == "skill_judgement" and label == "幸运七":
            return True
        return window.kind == "skill_parameter" and label == "予以信任"

    def __init__(
        self,
        *,
        characters: CharacterManager,
        conflict: ConflictManager,
        decisions: DecisionWindowManager,
        windows: PostCheckWindowManager,
        skill_triggers: SkillTriggerManager,
        skill_lifecycle: SkillLifecycleCoordinator,
        check_transactions: CheckTransactionManager,
        post_check_state: PostCheckStateJournal,
        capture_skill_lifecycle: Callable[[SkillLifecycleOutcome], None],
        scenes: SceneManager | None = None,
    ) -> None:
        self.characters = characters
        self.conflict = conflict
        self.decisions = decisions
        self.windows = windows
        self.skill_triggers = skill_triggers
        self.skill_lifecycle = skill_lifecycle
        self.check_transactions = check_transactions
        self.post_check_state = post_check_state
        self.capture_skill_lifecycle = capture_skill_lifecycle
        self.scenes = scenes

    def _decision_scope(self) -> tuple[str, str]:
        if self.conflict.state.active:
            return "conflict", str(self.conflict.state.scene_name or "current")
        scene = self.scenes.current_scene if self.scenes is not None else None
        if scene is not None:
            return "scene", str(scene.scene_id or scene.name or "current")
        return "scene", "current"

    def hydrate_for_action(self, action: Action) -> bool:
        """Hydrate the matching portable transaction before dispatch."""

        actor = str(action.parameters.get("actor") or "").strip()
        if not actor:
            return False
        window_id = str(action.parameters.get("window_id") or "").strip()
        window = self.decisions.find_pending(window_id=window_id) if window_id else None
        wanted_kind = ""
        if action.action_type == ActionType.INVOKE_TRAIT:
            wanted_kind = "trait_invocation"
        elif action.action_type == ActionType.INVOKE_BOND:
            wanted_kind = "bond_invocation"
        elif (
            action.action_type == ActionType.SKILL
            and str(action.parameters.get("skill_name") or "").strip() == "幸运七"
        ):
            wanted_kind = "skill_judgement"
        elif action.action_type == ActionType.RESOLVE_DECISION and window_id:
            candidate = self.decisions.find_pending(window_id=window_id)
            if candidate is not None and self._is_pre_final_decision(candidate):
                window = candidate
                wanted_kind = candidate.kind
        elif action.action_type in {ActionType.NARRATE, ActionType.RESOLVE_DECISION} and action.parameters.get(
            "post_check_acceptance"
        ):
            wanted_kind = ""
        else:
            return False

        if window is None and wanted_kind:
            window = self.decisions.find_pending(kind=wanted_kind, owner=actor)
            if window is not None and not self._is_pre_final_decision(window):
                window = None
        if window is None:
            window = next(
                (
                    item
                    for item in self.decisions.pending(owner=actor, blocking_only=True)
                    if self._is_pre_final_decision(item)
                ),
                None,
            )
        if window is None or window.owner != actor:
            return False
        return self.check_transactions.hydrate_from_window(window)

    def validate_opportunity_action(self, action: Action) -> None:
        """Require every opportunity effect to consume its actual rule window.

        The opportunity resolver deliberately knows only how to apply an
        effect. Admission belongs here, beside the persisted decision state.
        This prevents a malformed or stale action from inventing an
        opportunity, consuming an unrelated post-check choice, or applying the
        same opportunity twice.

        A small trusted escape hatch remains for rule components and focused
        unit tests that resolve a mechanically granted opportunity without a
        post-check roll. It is private to the hard-rules layer and may never
        overtake another blocking decision.
        """

        if action.action_type != ActionType.TRIGGER_OPPORTUNITY:
            return

        trusted_grant = bool(action.parameters.get("_trusted_opportunity_grant"))
        if trusted_grant:
            if self.decisions.has_blocking():
                owner = self.decisions.pending(blocking_only=True)[0].owner
                raise ValueError(
                    f"先由【{owner}】处理刚才的规则选择，不能用内部机会跳过待决窗口。"
                )
            return

        actor = str(action.parameters.get("actor") or "").strip()
        window_id = str(action.parameters.get("window_id") or "").strip()
        allowed_kinds = {
            "critical_opportunity",
            "fumble_opportunity",
            "opportunity_parameter",
        }
        window = self.decisions.find_pending(window_id=window_id) if window_id else None
        if window is not None and window.kind not in allowed_kinds:
            raise ValueError(
                f"待决窗口【{window.kind}】不能用机会动作处理。"
            )
        if window is None and window_id:
            raise ValueError("这个机会窗口已经结束或不存在，不能重复处理。")
        if window is None:
            candidates = [
                item
                for item in self.decisions.pending(owner=actor)
                if item.kind in allowed_kinds
            ]
            if len(candidates) != 1:
                raise ValueError("当前没有唯一可处理的机会窗口。")
            window = candidates[0]

        responder = actor
        if window.allowed_responders and responder not in window.allowed_responders:
            raise ValueError(
                f"{responder or '当前行动者'} 不能替【{window.owner}】处理这个机会。"
            )
        if window.owner != actor:
            raise ValueError(f"这个机会应由【{window.owner}】处理。")
        effect = str(
            action.parameters.get("effect")
            or action.parameters.get("opportunity")
            or ""
        ).strip()
        legal_effects = {
            str(option.get("effect") or "").strip()
            for option in window.options
            if str(option.get("effect") or "").strip()
        }
        if legal_effects and effect not in legal_effects:
            raise ValueError(f"【{effect or '未指定'}】不是这个机会窗口的合法效果。")
        selected_effect = str(window.payload.get("selected_effect") or "").strip()
        if selected_effect and effect != selected_effect:
            raise ValueError(
                f"这个补充窗口正在等待机会【{selected_effect}】的参数，不能改成【{effect or '未指定'}】。"
            )
        if effect == "法术附加效果":
            spell_opportunity = window.payload.get("spell_opportunity")
            if not isinstance(spell_opportunity, dict):
                raise ValueError("这次检定没有可结算的法术专属机会效果。")
            action.parameters["_spell_opportunity"] = dict(
                spell_opportunity
            )
        action.parameters["window_id"] = window.window_id

    def capture_source_windows(self, action: Action) -> dict[str, object]:
        actor = str(action.parameters.get("actor") or "").strip()
        if not actor:
            pending = self.decisions.pending(blocking_only=True)
            actor = pending[0].owner if pending else ""
        if not actor:
            return {}
        kinds = {
            "critical_opportunity",
            "fumble_opportunity",
            "trait_invocation",
            "bond_invocation",
            "skill_judgement",
            "opportunity_parameter",
        }
        pending = [
            window
            for window in self.decisions.pending(owner=actor)
            if window.kind in kinds
        ]
        selected_id = str(action.parameters.get("window_id") or "").strip()
        selected = next(
            (window for window in pending if window.window_id == selected_id),
            None,
        )
        if selected is None:
            wanted_kinds: tuple[str, ...] = ()
            if action.action_type == ActionType.INVOKE_TRAIT:
                wanted_kinds = ("trait_invocation",)
            elif action.action_type == ActionType.INVOKE_BOND:
                wanted_kinds = ("bond_invocation",)
            elif action.action_type == ActionType.TRIGGER_OPPORTUNITY:
                wanted_kinds = (
                    "opportunity_parameter",
                    "critical_opportunity",
                    "fumble_opportunity",
                )
            selected = next(
                (window for window in pending if window.kind in wanted_kinds),
                None,
            )
        if selected is None:
            selected = next((window for window in pending if window.blocking), None)
        if selected is None and pending:
            selected = pending[0]
        return {
            "actor": actor,
            "selected_id": selected.window_id if selected is not None else "",
            "window_ids": [window.window_id for window in pending],
            "payload": dict(selected.payload) if selected is not None else {},
            "transaction_id": selected.transaction_id if selected is not None else "",
        }

    def settle(
        self,
        action: Action,
        resolution: ActionResolution,
        *,
        source_windows: dict[str, object] | None = None,
    ) -> None:
        """Close a post-check selection and expire its sibling choices."""

        is_acceptance = bool(action.parameters.get("post_check_acceptance"))
        if action.action_type not in {
            ActionType.INVOKE_TRAIT,
            ActionType.INVOKE_BOND,
            ActionType.TRIGGER_OPPORTUNITY,
        } and not is_acceptance:
            return
        actor = str(action.parameters.get("actor") or "").strip()
        if not actor:
            pending = self.decisions.pending(blocking_only=True)
            actor = pending[0].owner if pending else ""
        if not actor:
            return
        captured = dict(source_windows or {})
        selected_id = str(
            captured.get("selected_id") or action.parameters.get("window_id") or ""
        ).strip()
        source_ids = [
            str(item)
            for item in captured.get("window_ids", [])
            if str(item).strip()
        ]
        source = dict(captured.get("payload") or {})
        source["owner"] = actor
        selection_resolution = {
            "action_type": action.action_type.value,
            "choice": str(
                action.parameters.get("effect")
                or action.parameters.get("trait_name")
                or action.parameters.get("bond_target")
                or "accepted"
            ),
        }

        if resolution.payload.get("opportunity_parameter_required"):
            if selected_id:
                self.decisions.settle_selection(
                    window_id=selected_id,
                    responder=actor,
                    resolution={**selection_resolution, "state": "awaiting_parameter"},
                    sibling_ids=source_ids,
                    sibling_reason="opportunity_parameter_selected",
                    allow_superseded=True,
                )
            scope_kind, scope_id = self._decision_scope()
            parameter_owner = str(
                action.parameters.get("_opportunity_parameter_owner") or actor
            ).strip()
            raw_responders = action.parameters.get(
                "_opportunity_parameter_allowed_responders"
            )
            parameter_responders = (
                [
                    str(item).strip()
                    for item in raw_responders
                    if str(item).strip()
                ]
                if isinstance(raw_responders, list)
                else [parameter_owner]
            )
            provided_parameters = resolution.payload.get("provided_parameters")
            parameter_window = self.decisions.create(
                kind="opportunity_parameter",
                owner=parameter_owner,
                prompt=str(
                    resolution.rules_text
                    or "请补充这个机会效果需要的目标。"
                ),
                options=[],
                scope_kind=scope_kind,
                scope_id=scope_id,
                blocking=True,
                allowed_responders=parameter_responders,
                action_type=ActionType.TRIGGER_OPPORTUNITY.value,
                transaction_id=str(
                    captured.get("transaction_id")
                    or source.get("check_batch_id")
                    or ""
                ),
                resume_point="opportunity_parameter",
                payload={
                    **source,
                    "opportunity_source_owner": actor,
                    "required_parameter": resolution.payload.get("required_parameter"),
                    "provided_parameters": (
                        dict(provided_parameters)
                        if isinstance(provided_parameters, dict)
                        else {}
                    ),
                    "selected_effect": str(
                        action.parameters.get("effect")
                        or action.parameters.get("opportunity")
                        or ""
                    ),
                },
                dedupe_key=f"opportunity_parameter:{parameter_owner}",
            )
            resolution.payload["decision_windows"] = [
                {
                    "window_id": parameter_window.window_id,
                    "kind": parameter_window.kind,
                    "actor": parameter_owner,
                    "blocking": True,
                    "guidance": parameter_window.prompt,
                }
            ]
            return

        if selected_id:
            self.decisions.settle_selection(
                window_id=selected_id,
                responder=actor,
                resolution=selection_resolution,
                sibling_ids=source_ids,
                sibling_reason="post_check_alternative_not_selected",
                allow_superseded=True,
            )
        elif source_ids:
            for window_id in source_ids:
                window = self.decisions.get(window_id)
                if window is not None and window.status == DecisionWindowStatus.PENDING:
                    window.status = DecisionWindowStatus.EXPIRED
                    window.resolution = {"reason": "post_check_result_accepted"}

        if (
            action.action_type in {ActionType.INVOKE_TRAIT, ActionType.INVOKE_BOND}
            or is_acceptance
        ) and not resolution.payload.get("check_result_provisional"):
            for kind in self._RESUMABLE_KINDS:
                self.decisions.cancel_matching(
                    kind=kind,
                    owner=actor,
                    reason="post_check_invocation_consumed",
                    status=DecisionWindowStatus.EXPIRED,
                )
        resolution.payload["decision_windows"] = self.decisions.public_summary()
        if source:
            resolution.payload["post_check_decision_resolved"] = True
            if not self.decisions.has_blocking():
                resolution.payload["resume_deferred_action"] = True
                resolution.payload["deferred_action_type"] = str(
                    source.get("source_action_type") or ""
                )
                resolution.payload["deferred_action_owner"] = str(
                    source.get("deferred_turn_actor") or actor
                )

    def attach(self, resolution: ActionResolution) -> None:
        outcome = resolution.payload.get("roll")
        if outcome is None or not hasattr(outcome, "actor"):
            return
        actor_name = str(getattr(outcome, "actor", "") or "")
        if not actor_name or not self.characters.exists(actor_name):
            return
        decision_owner = str(
            resolution.action.parameters.get("_decision_owner") or actor_name
        ).strip()
        if (
            not self.characters.exists(decision_owner)
            or "pc" not in self.characters.get(decision_owner).traits
        ):
            decision_owner = actor_name

        self.post_check_state.remember_roll(outcome)
        actor = self.characters.get(actor_name)
        # An opposed roll has no standalone success yet: target number 0 is a
        # storage detail and the result exists only after both totals are
        # compared.  Keep the normal invocation window for that participant,
        # while ordinary successful checks still settle immediately.
        windows = self.windows.build(
            actor,
            outcome,
            allow_success_invocation=bool(
                resolution.action.parameters.get("_opposed_check_roll")
            ),
        )
        spell_opportunity = resolution.payload.get("spell_opportunity")
        if isinstance(spell_opportunity, dict):
            for window in windows:
                if str(window.get("kind") or "") != "critical_opportunity":
                    continue
                options = [
                    dict(option)
                    for option in window.get("options", [])
                    if isinstance(option, dict)
                ]
                options.append(
                    {
                        "effect": "法术附加效果",
                        "summary": "结算该法术在大成功时列出的专属机会效果。",
                    }
                )
                window["options"] = options
        windows.extend(self.skill_triggers.emit("after_check", actor, outcome=outcome).windows)
        check_batch_id = str(
            resolution.action.parameters.get("_check_batch_id")
            or resolution.payload.get("_post_check_batch_id")
            or uuid4()
        )
        resolution.payload["_post_check_batch_id"] = check_batch_id
        if windows:
            self._persist_windows(
                resolution,
                actor_name,
                outcome,
                windows,
                decision_owner=decision_owner,
                check_batch_id=check_batch_id,
            )

        if "pc" not in actor.traits:
            return
        used_by = {
            str(name).strip()
            for name in resolution.action.parameters.get("_trust_assist_used_by", [])
            if str(name).strip()
        }
        for supporter in self.characters.all():
            if (
                supporter.name == actor.name
                or supporter.name in used_by
                or "pc" not in supporter.traits
            ):
                continue
            lifecycle = self.skill_lifecycle.trigger(
                "after_ally_check",
                supporter,
                target=actor,
                can_hear=True,
                outcome=outcome,
                transaction_available=self.check_transactions.candidate is not None,
                transaction_id=check_batch_id,
                source_actor=actor_name,
                source_action_type=resolution.action.action_type.value,
            )
            self.capture_skill_lifecycle(lifecycle)

    def _persist_windows(
        self,
        resolution: ActionResolution,
        actor_name: str,
        outcome,
        windows: list[dict[str, object]],
        *,
        decision_owner: str,
        check_batch_id: str,
    ) -> None:
        for kind in self._REPLACED_KINDS:
            self.decisions.cancel_matching(
                kind=kind,
                owner=decision_owner,
                reason="new_check_replaced_previous_window",
                status=DecisionWindowStatus.EXPIRED,
            )

        scope_kind, scope_id = self._decision_scope()
        persisted: list[dict[str, object]] = []
        gm_controlled: list[dict[str, object]] = []
        source_action_type = resolution.action.action_type.value
        candidate = self.check_transactions.candidate or {}
        if bool(candidate.get("bond_invoked")):
            windows = [
                window
                for window in windows
                if str(window.get("kind") or "") != "bond_invocation"
            ]
        pre_final_windows = [
            window for window in windows if self._is_pre_final_window(window)
        ]
        pre_final_phase = bool(pre_final_windows) and (
            not self.check_transactions.replaying
            or self.check_transactions.allow_restage
        )
        if pre_final_phase:
            windows = pre_final_windows
        elif self.check_transactions.replaying and not self.check_transactions.allow_restage:
            windows = [
                window for window in windows if not self._is_pre_final_window(window)
            ]
        portable_resume = (
            self.check_transactions.portable_resume_payload(
                action=resolution.action,
                outcome=outcome,
                roll_sequence=[
                    item
                    for item in resolution.payload.get("check_roll_sequence", [])
                    if hasattr(item, "actor")
                ],
                roll_index=int(
                    resolution.payload.get("check_roll_index", 0) or 0
                ),
            )
            if pre_final_phase and self.check_transactions.candidate is not None
            else {}
        )
        failure_grace_token = ""
        failure_grace_due_at = ""
        if pre_final_phase and not bool(outcome.success):
            failure_grace_token = str(uuid4())
            failure_grace_due_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.FAILED_CHECK_GRACE_SECONDS)
            ).isoformat()

        for window in windows:
            kind = str(window.get("kind") or "post_check")
            priority = str(window.get("priority") or "normal")
            if kind == "fumble_opportunity":
                payload = {
                    "source_action_type": source_action_type,
                    "source_actor": actor_name,
                    "roll_total": int(outcome.total),
                    "target_number": int(outcome.target_number),
                    "roll_success": False,
                    "priority": priority,
                    "check_batch_id": check_batch_id,
                    "controller": "gm",
                }
                decision = self.decisions.create(
                    kind=kind,
                    owner="__gm__",
                    prompt=str(window.get("guidance") or ""),
                    options=[
                        dict(item)
                        for item in window.get("options", [])
                        if isinstance(item, dict)
                    ],
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    blocking=True,
                    allowed_responders=["__gm__"],
                    action_type=ActionType.TRIGGER_OPPORTUNITY.value,
                    transaction_id=check_batch_id,
                    resume_point="post_check",
                    payload=payload,
                    dedupe_key=(
                        f"post_check:gm:fumble:{actor_name}:{outcome.total}:"
                        f"{outcome.target_number}"
                    ),
                )
                enriched = dict(window)
                enriched.update(
                    {
                        "window_id": decision.window_id,
                        "controller": "gm",
                        "owner": decision.owner,
                        "blocking": True,
                        "check_batch_id": check_batch_id,
                    }
                )
                gm_controlled.append(enriched)
                continue

            blocking = kind == "critical_opportunity" or self._is_pre_final_window(window)
            payload: dict[str, object] = {
                "label": str(window.get("label") or ""),
                "source_action_type": source_action_type,
                "roll_total": int(outcome.total),
                "target_number": int(outcome.target_number),
                "roll_success": bool(outcome.success),
                "priority": priority,
                "check_batch_id": check_batch_id,
                "transaction_available": actor_name in self.check_transactions.pending
                or self.check_transactions.candidate is not None,
                "source_actor": actor_name,
            }
            if self._is_pre_final_window(window) and not bool(outcome.success):
                payload.update(
                    {
                        "silent_failure_grace": True,
                        "failure_grace_seconds": self.FAILED_CHECK_GRACE_SECONDS,
                        "failure_grace_due_at": failure_grace_due_at,
                        "failure_grace_token": failure_grace_token,
                    }
                )
            if (
                kind == "critical_opportunity"
                and isinstance(
                    resolution.payload.get("spell_opportunity"),
                    dict,
                )
            ):
                payload["spell_opportunity"] = dict(
                    resolution.payload["spell_opportunity"]
                )
            if blocking and self._is_pre_final_window(window):
                payload.update(portable_resume)
            decision = self.decisions.create(
                kind=kind,
                owner=decision_owner,
                prompt=str(window.get("guidance") or ""),
                options=[
                    dict(item)
                    for item in window.get("options", [])
                    if isinstance(item, dict)
                ],
                scope_kind=scope_kind,
                scope_id=scope_id,
                blocking=blocking,
                action_type=str(window.get("action_type") or ""),
                transaction_id=check_batch_id,
                resume_point="post_check",
                payload=payload,
                dedupe_key=(
                    f"post_check:{decision_owner}:{kind}:{outcome.total}:"
                    f"{outcome.target_number}"
                ),
            )
            enriched = dict(window)
            enriched.update(
                {
                    "window_id": decision.window_id,
                    "actor": decision_owner,
                    "source_actor": actor_name,
                    "blocking": decision.blocking,
                    "check_batch_id": check_batch_id,
                }
            )
            persisted.append(enriched)

        resolution.payload["post_check_windows"] = [*persisted, *gm_controlled]
        resolution.payload["decision_windows"] = [*persisted, *gm_controlled]
        if gm_controlled:
            resolution.payload["gm_post_check_windows"] = gm_controlled
