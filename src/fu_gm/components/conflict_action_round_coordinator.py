from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fu_gm.models import Action, ActionResolution, ActionType, ClockChange


class ConflictActionRoundCoordinator:
    """Commit one conflict action and advance time at the complete-round boundary."""

    def __init__(
        self,
        *,
        conflicts: Any,
        decisions: Any,
        clocks: Any,
        pacing: Any,
        clock_changes: Any,
        is_turn_consuming: Callable[[Action], bool],
        is_boss_scene: Callable[[], bool],
        held_action_notice: Callable[[str], str],
    ) -> None:
        self.conflicts = conflicts
        self.decisions = decisions
        self.clocks = clocks
        self.pacing = pacing
        self.clock_changes = clock_changes
        self.is_turn_consuming = is_turn_consuming
        self.is_boss_scene = is_boss_scene
        self.held_action_notice = held_action_notice

    def advance(self, action: Action, resolution: ActionResolution) -> None:
        if resolution.payload.get("check_result_provisional") or resolution.payload.get("action_uncommitted"):
            return
        if (
            not self.conflicts.state.active
            or resolution.payload.get("out_of_turn")
            or resolution.payload.get("team_assist_registered")
        ):
            return
        blocking_windows = self.decisions.pending(blocking_only=True)
        if blocking_windows:
            resolution.payload["turn_held_for_decision"] = True
            resolution.payload["decision_windows"] = self.decisions.public_summary()
            return
        if action.action_type == ActionType.NEXT_TURN:
            return
        if resolution.payload.get("resumed_check_batch_roll"):
            return
        committed_source_action = resolution.payload.get("committed_source_action")
        if (
            isinstance(committed_source_action, Action)
            and committed_source_action.parameters.get("_check_batch_roll")
        ):
            # 团队先攻和玩家对抗的批次骰只是决定行动顺序或检定结果，
            # 即使其待决窗口以 resume_deferred_action 收尾，也不属于冲突中的一次行动。
            return
        resume_deferred = bool(resolution.payload.get("resume_deferred_action"))
        if not resume_deferred and not self.is_turn_consuming(action):
            return

        previous_actor = self.conflicts.state.current_actor()
        previous_round = int(self.conflicts.state.round_number or 0)
        changed_clock_names = self.clock_changes.changed_clock_names(resolution)
        current_skip_names = self.clock_changes.auto_advance_skip_names(resolution)
        round_skip_names = set(self.conflicts.state.auto_advance_skip_names_this_round)
        round_skip_names.update(current_skip_names)
        self.conflicts.state.auto_advance_skip_names_this_round = sorted(round_skip_names)

        if resolution.payload.get("remove_absent_actor_from_conflict"):
            absent_actor = str(resolution.payload.get("actor") or "").strip()
            if absent_actor:
                self.conflicts.remove_combatant_from_scene(
                    absent_actor,
                    as_escaped=True,
                )
                if absent_actor != previous_actor:
                    # 临时离席是桌面安排，不应因为玩家在别人回合宣布离席，
                    # 顺便把当前行动者的回合也推进掉。该角色只从后续回合表移除。
                    resolution.payload["previous_actor"] = previous_actor
                    resolution.payload["next_actor"] = self.conflicts.state.current_actor()
                    resolution.payload["action_round_completed"] = False
                    resolution.payload["turn_board"] = self.conflicts.format_turn_board()
                    resolution.payload["combat_log"] = self.conflicts.format_combat_log()
                    return

        next_actor = self.conflicts.next_turn()
        newly_opened_windows = self.decisions.pending(blocking_only=True)
        if newly_opened_windows:
            resolution.payload["turn_held_for_decision"] = True
            resolution.payload["decision_windows"] = self.decisions.public_summary()
            resolution.payload["previous_actor"] = previous_actor
            resolution.payload["next_actor"] = self.conflicts.state.current_actor()
            resolution.payload["action_round_completed"] = False
            return

        owner_turn_end_changes = list(
            self.clocks.emit_auto_advance_event(
                "owner_turn_end",
                actor=str(previous_actor or ""),
                skip_names=round_skip_names,
            )
        )
        if owner_turn_end_changes:
            resolution.payload.setdefault("timeline_phases", []).append(
                {
                    "kind": "automatic_clock",
                    "timing": "owner_turn_end",
                    "actor": str(previous_actor or ""),
                    "status": "completed",
                    "clock_names": [
                        change.clock_name for change in owner_turn_end_changes
                    ],
                }
            )

        current_round = int(self.conflicts.state.round_number or 0)
        action_round_completed = current_round > previous_round
        auto_clock_changes: list[ClockChange] = []
        if action_round_completed:
            auto_clock_changes = list(
                self.pacing.auto_advance_after_turn(
                    skip_names=round_skip_names,
                    boss_scene=self.is_boss_scene(),
                    event_timing="action_round_end",
                )
            )
            self.conflicts.state.auto_advance_skip_names_this_round = []
            resolution.payload["turn_auto_advanced"] = True
            resolution.payload["action_round_completed"] = True
            resolution.payload["completed_action_round"] = previous_round
        else:
            resolution.payload["action_round_completed"] = False

        round_clock_names = [
            clock.name
            for clock in self.clocks.subscribed_auto_clocks(
                "action_round_end"
            )
        ]
        if round_clock_names or auto_clock_changes:
            resolution.payload.setdefault("timeline_phases", []).append(
                {
                    "kind": "automatic_clock",
                    "timing": "action_round_end",
                    "round": previous_round,
                    "status": (
                        "completed" if action_round_completed else "pending"
                    ),
                    "clock_names": list(
                        dict.fromkeys(
                            [
                                *round_clock_names,
                                *[
                                    change.clock_name
                                    for change in auto_clock_changes
                                ],
                            ]
                        )
                    ),
                }
            )

        resolution.payload["previous_actor"] = previous_actor
        resolution.payload["next_actor"] = next_actor
        all_auto_clock_changes = [
            *list(resolution.payload.get("auto_clock_changes") or []),
            *owner_turn_end_changes,
            *auto_clock_changes,
        ]
        if all_auto_clock_changes:
            resolution.payload["auto_clock_changes"] = all_auto_clock_changes
            for change in all_auto_clock_changes:
                self.conflicts.record_log(
                    "system",
                    "auto_clock_advance",
                    f"命刻【{change.clock_name}】自动推进：{change.before}/{change.max_segments} -> {change.after}/{change.max_segments}。",
                )
        if self.clocks.all():
            highlighted_clock_names = set(changed_clock_names)
            highlighted_clock_names.update(
                change.clock_name for change in all_auto_clock_changes
            )
            resolution.payload["clock_progress"] = self.pacing.formatted_public_clocks(
                boss_scene=self.is_boss_scene(),
                highlight_names=highlighted_clock_names,
                only_highlighted=True,
            )
            resolution.payload["clock_status_refresh"] = bool(
                resolution.payload["clock_progress"] or all_auto_clock_changes
            )
        turn_board = self.conflicts.format_turn_board()
        turn_board["timeline_phases"] = list(
            resolution.payload.get("timeline_phases") or []
        )
        resolution.payload["turn_board"] = turn_board
        resolution.payload["combat_log"] = self.conflicts.format_combat_log()
        if next_actor:
            notice = self.held_action_notice(next_actor)
            if notice:
                resolution.payload["held_action_notice"] = notice
