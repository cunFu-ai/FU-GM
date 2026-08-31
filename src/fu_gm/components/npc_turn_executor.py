from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    DecisionWindow,
    GamePanel,
)


@dataclass
class NPCTurnExecution:
    """Audit-friendly result of one complete NPC turn transaction."""

    actor: str
    reply: str = ""
    actions: list[Action] = field(default_factory=list)
    resolutions: list[ActionResolution] = field(default_factory=list)
    stale_aborted: bool = False


class NPCTurnHost(Protocol):
    character_manager: Any
    conflict_manager: Any
    decision_window_manager: Any
    expressor: Any
    interceptor: Any
    npc_combat_rules: Any
    resolution_committer: Any
    safety_manager: Any
    session_episode_tracker: Any

    def build_panel(self, recent_chat: str) -> GamePanel: ...

    def _auto_advance_conflict_turn(
        self,
        action: Action,
        resolution: ActionResolution,
    ) -> None: ...

    def _attach_public_memory_to_resolution(
        self,
        resolution: ActionResolution,
        panel: GamePanel,
    ) -> None: ...

    def _audit_transparency(
        self,
        recent_chat: str,
        reply: str,
        resolution: ActionResolution,
    ) -> None: ...


class NPCTurnExecutor:
    """Resolve exactly one current NPC turn as a typed transaction.

    The core GM supplies one choice and its public action description from the
    legal-action snapshot. This module validates that choice and sends every
    mechanical effect through the same interceptor used by player actions.
    NPC-only follow-up windows use a deterministic, least-world-altering rule
    policy rather than another model.
    """

    def __init__(self, host: NPCTurnHost) -> None:
        self.host = host

    def execute(
        self,
        action_parameters: dict[str, object],
        scene_brief: str = "",
    ) -> str:
        return self.execute_result(
            action_parameters,
            scene_brief,
        ).reply

    def execute_result(
        self,
        action_parameters: dict[str, object],
        scene_brief: str = "",
        *,
        stale_guard: Any | None = None,
    ) -> NPCTurnExecution:
        host = self.host
        actor_name = host.conflict_manager.state.current_actor()
        if actor_name is None:
            raise ValueError("当前没有可行动的角色。")
        actor = host.character_manager.get(actor_name)
        if "pc" in actor.traits:
            raise ValueError(f"{actor_name} 是玩家角色，不能由GM代为执行回合。")
        combat_side = host.conflict_manager.combat_side(actor_name)
        if combat_side not in {"player", "enemy"}:
            raise ValueError(f"{actor_name} 没有明确的冲突阵营，不能执行NPC回合。")
        if host.npc_combat_rules is None:
            raise ValueError("当前场景未配置NPC战斗规则目录。")

        panel = host.build_panel(scene_brief or f"轮到 {actor_name} 行动。")
        if stale_guard is not None and stale_guard():
            return NPCTurnExecution(actor=actor_name, stale_aborted=True)
        existing_windows = host.decision_window_manager.pending(
            owner=actor_name,
            blocking_only=True,
        )
        if existing_windows:
            decisions = self._resolve_owned_windows(
                panel,
                actor_name,
                stale_guard=stale_guard,
            )
            if decisions is None:
                return NPCTurnExecution(actor=actor_name, stale_aborted=True)
            if not decisions:
                raise ValueError(f"【{actor_name}】仍有未处理的规则选择，不能继续其回合。")
            action, resolution = decisions[0]
            self._merge_decisions(resolution, decisions[1:])
            host._auto_advance_conflict_turn(action, resolution)
            for _, decision_resolution in decisions:
                host.resolution_committer.commit(decision_resolution)
            reply = self._render(panel, resolution)
            return NPCTurnExecution(
                actor=actor_name,
                reply=reply,
                actions=[item[0] for item in decisions],
                resolutions=[item[1] for item in decisions],
            )

        action = host.npc_combat_rules.validate_action(
            panel,
            actor_name,
            action_parameters,
        )
        if stale_guard is not None and stale_guard():
            return NPCTurnExecution(actor=actor_name, stale_aborted=True)
        resolution = host.interceptor.resolve(action)
        decisions = self._resolve_owned_windows(
            panel,
            actor_name,
            stale_guard=stale_guard,
        )
        if decisions is None:
            return NPCTurnExecution(actor=actor_name, stale_aborted=True)
        self._merge_decisions(resolution, decisions)
        host._auto_advance_conflict_turn(action, resolution)

        # Acceleration is created by end-of-turn processing, so it cannot be
        # present in ``decisions`` above. NPC-owned windows must be settled in
        # the same transaction; otherwise the next player message would make
        # the NPC choose a second ordinary action while initiative is paused.
        turn_end_decisions: list[tuple[Action, ActionResolution]] = []
        if host.conflict_manager.state.current_actor() == actor_name:
            pending_turn_end = host.decision_window_manager.pending(
                owner=actor_name,
                blocking_only=True,
            )
            if pending_turn_end:
                resolved_turn_end = self._resolve_owned_windows(
                    panel,
                    actor_name,
                    stale_guard=stale_guard,
                )
                if resolved_turn_end is None:
                    return NPCTurnExecution(actor=actor_name, stale_aborted=True)
                turn_end_decisions = resolved_turn_end
                self._merge_decisions(resolution, turn_end_decisions)
                advance_candidate = self._resume_candidate(turn_end_decisions)
                if advance_candidate is not None:
                    host._auto_advance_conflict_turn(*advance_candidate)
                    self._copy_turn_state(resolution, advance_candidate[1])

        for _, decision_resolution in decisions:
            host.resolution_committer.commit(decision_resolution)
        for _, decision_resolution in turn_end_decisions:
            host.resolution_committer.commit(decision_resolution)
        host.resolution_committer.commit(resolution)
        reply = self._render(panel, resolution)
        all_decisions = [*decisions, *turn_end_decisions]
        return NPCTurnExecution(
            actor=actor_name,
            reply=reply,
            actions=[action, *(item[0] for item in all_decisions)],
            resolutions=[resolution, *(item[1] for item in all_decisions)],
        )

    def _resolve_owned_windows(
        self,
        panel: GamePanel,
        actor_name: str,
        *,
        stale_guard: Any | None = None,
    ) -> list[tuple[Action, ActionResolution]] | None:
        host = self.host
        resolved: list[tuple[Action, ActionResolution]] = []
        for _ in range(8):
            windows = host.decision_window_manager.pending(
                owner=actor_name,
                blocking_only=True,
            )
            if not windows:
                return resolved
            window = windows[0]
            if window.kind == "zero_hp":
                raise ValueError("生命值归零的选择必须由对应玩家本人处理。")
            action = self._window_action(panel, actor_name, window)
            if stale_guard is not None and stale_guard():
                return None
            resolution = host.interceptor.resolve(action)
            resolved.append((action, resolution))

        remaining = host.decision_window_manager.pending(
            owner=actor_name,
            blocking_only=True,
        )
        if remaining:
            raise RuntimeError(f"NPC【{actor_name}】的规则选择超过安全处理上限。")
        return resolved

    def _window_action(
        self,
        panel: GamePanel,
        actor_name: str,
        window: DecisionWindow,
    ) -> Action:
        resolve_window = getattr(
            self.host.npc_combat_rules,
            "resolve_window",
            None,
        )
        if not callable(resolve_window):
            raise ValueError(
                f"NPC战斗规则尚未实现待决窗口【{window.kind}】；本次不猜测也不跳过。"
            )
        return resolve_window(panel, actor_name, window)

    @staticmethod
    def _resume_candidate(
        decisions: list[tuple[Action, ActionResolution]],
    ) -> tuple[Action, ActionResolution] | None:
        for action, resolution in decisions:
            if resolution.payload.get("action_uncommitted"):
                continue
            if resolution.payload.get("check_result_provisional"):
                continue
            if resolution.payload.get("resume_deferred_action"):
                return action, resolution
        return decisions[0] if decisions else None

    @staticmethod
    def _copy_turn_state(base: ActionResolution, source: ActionResolution) -> None:
        """Replace transient turn-hold metadata after a window resumes play."""

        if not source.payload.get("turn_held_for_decision"):
            base.payload.pop("turn_held_for_decision", None)
        for key in (
            "decision_windows",
            "previous_actor",
            "next_actor",
            "action_round_completed",
            "completed_action_round",
            "turn_auto_advanced",
            "auto_clock_changes",
            "clock_progress",
        ):
            if key in source.payload:
                base.payload[key] = source.payload[key]

    @staticmethod
    def _merge_decisions(
        base: ActionResolution,
        decisions: list[tuple[Action, ActionResolution]],
    ) -> None:
        if not decisions:
            return
        audit: list[dict[str, object]] = []
        extra_rules: list[str] = []
        for decision_action, decision_resolution in decisions:
            if decision_resolution.rules_text:
                extra_rules.append(decision_resolution.rules_text)
            audit.append(
                {
                    "action_type": decision_action.action_type.value,
                    "parameters": dict(decision_action.parameters),
                    "payload": dict(decision_resolution.payload),
                }
            )
            for key in (
                "decision_windows",
                "resume_deferred_action",
                "deferred_action_type",
                "deferred_action_owner",
                "post_check_decision_resolved",
                "previous_actor",
                "next_actor",
                "action_round_completed",
                "turn_held_for_decision",
            ):
                if key in decision_resolution.payload:
                    base.payload[key] = decision_resolution.payload[key]
        if extra_rules:
            base.rules_text = "\n".join([base.rules_text, *extra_rules]).strip()
        base.payload["npc_decision_resolutions"] = audit

    def _render(self, panel: GamePanel, resolution: ActionResolution) -> str:
        host = self.host
        resolution.payload["safety_guidance"] = host.safety_manager.render_guidance()
        host._attach_public_memory_to_resolution(resolution, panel)
        public_action = str(
            resolution.action.parameters.get("in_mind_reply") or ""
        ).strip()
        canonical_renderer = getattr(host.expressor, "fallback", host.expressor)
        canonical_text = str(canonical_renderer.render(resolution) or "").strip()
        parts = [public_action] if public_action else []
        if canonical_text and not any(
            canonical_text == item or canonical_text in item for item in parts
        ):
            parts.append(canonical_text)
        reply = "\n".join(parts).strip()
        host.session_episode_tracker.turn_resolved(
            resolution,
            public_reply=reply,
        )
        host._audit_transparency("", reply, resolution)
        return reply
