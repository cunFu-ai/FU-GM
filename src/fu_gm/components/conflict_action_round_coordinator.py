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

        next_actor = self.conflicts.next_turn()
        newly_opened_windows = self.decisions.pending(blocking_only=True)
        if newly_opened_windows:
            resolution.payload["turn_held_for_decision"] = True
            resolution.payload["decision_windows"] = self.decisions.public_summary()
            resolution.payload["previous_actor"] = previous_actor
            resolution.payload["next_actor"] = self.conflicts.state.current_actor()
            resolution.payload["action_round_completed"] = False
            return

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

        resolution.payload["previous_actor"] = previous_actor
        resolution.payload["next_actor"] = next_actor
        if auto_clock_changes:
            resolution.payload["auto_clock_changes"] = auto_clock_changes
            for change in auto_clock_changes:
                self.conflicts.record_log(
                    "system",
                    "auto_clock_advance",
                    f"命刻【{change.clock_name}】自动推进：{change.before}/{change.max_segments} -> {change.after}/{change.max_segments}。",
                )
        if self.clocks.all():
            highlighted_clock_names = set(changed_clock_names)
            highlighted_clock_names.update(change.clock_name for change in auto_clock_changes)
            resolution.payload["clock_progress"] = self.pacing.formatted_public_clocks(
                boss_scene=self.is_boss_scene(),
                highlight_names=highlighted_clock_names,
            )
            resolution.payload["clock_status_refresh"] = True
        resolution.payload["turn_board"] = self.conflicts.format_turn_board()
        resolution.payload["combat_log"] = self.conflicts.format_combat_log()
        if next_actor:
            notice = self.held_action_notice(next_actor)
            if notice:
                resolution.payload["held_action_notice"] = notice
