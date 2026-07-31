from __future__ import annotations

from dataclasses import dataclass, field, replace

from fu_gm.models import PressureBudget, SessionFeedbackSignals


@dataclass
class CampaignFeedbackControl:
    """Rolling pacing corrections derived from play that actually happened."""

    notes: list[str] = field(default_factory=list)
    villain_move_due: bool = False
    consolidate_threads: bool = False
    recovery_breath_due: bool = False
    clarify_reveal_due: bool = False
    stall_break_due: bool = False
    expected_turn_delta: int = 0
    reveal_quota_delta: int = 0
    auto_pressure_delta: int = 0


class CampaignFeedbackController:
    """Turn recent play evidence into restrained backstage corrections.

    This controller never chooses plot outcomes. It only notices drift such as
    villain inactivity, overloaded pressure, weak payoffs, or repeated stalls.
    """

    WINDOW = 3

    def evaluate(
        self,
        history: list[SessionFeedbackSignals],
        budget: PressureBudget,
    ) -> CampaignFeedbackControl:
        recent = history[-self.WINDOW :]
        if not recent:
            return CampaignFeedbackControl()

        latest = recent[-1]
        control = CampaignFeedbackControl()

        def average(attribute: str) -> float:
            return sum(float(getattr(item, attribute, 0) or 0) for item in recent) / len(recent)

        control.villain_move_due = latest.villain_drought_sessions >= 2
        if control.villain_move_due:
            control.notes.append(
                "连续两场没有反派推进；本场让反派、代理人或计划后果采取一次可见行动。"
            )

        control.consolidate_threads = latest.unresolved_thread_count >= 6
        if control.consolidate_threads:
            control.notes.append("未解决线索过多；优先兑现或合并旧线索，不再添加同等分量的新谜团。")

        control.clarify_reveal_due = average("reveal_uptake") < 0.5
        if control.clarify_reveal_due:
            control.reveal_quota_delta = -1
            control.notes.append("玩家尚未消化近期揭示；用人物反应和现实后果体现意义，不复述设定。")

        control.stall_break_due = average("stalled_beats") >= 1.5
        if control.stall_break_due:
            control.notes.append("近期场面停滞偏多；NPC要明确答复，环境或对手要在合适时机改变局面。")

        overloaded = any(
            item.foreground_pressure_count > budget.max_foreground_pressure_clocks
            for item in recent[-2:]
        )
        high_resource_pressure = average("resource_pressure_ratio") >= 0.45
        control.recovery_breath_due = overloaded or high_resource_pressure
        if control.recovery_breath_due:
            control.auto_pressure_delta = -1
            control.notes.append("近期压力或资源消耗偏高；本场先给恢复、准备或关系场景，再升级威胁。")
        elif average("resource_spend_events") < 0.5:
            control.notes.append("近期几乎没有资源代价；准备值得主动投入MP、物资点或物语点的真实选择。")

        shallow_sessions = sum(
            1 for item in recent if item.scene_count < 3 or item.meaningful_turns < 20
        )
        weak_payoffs = sum(
            1
            for item in recent
            if not item.local_question_resolved
            and not item.local_question_changed
            and not (item.deliberate_cliffhanger and item.reversal_reached)
        )
        latest_shallow = latest.scene_count < 3 or latest.meaningful_turns < 20
        if latest_shallow or shallow_sessions >= 2 or weak_payoffs >= 2:
            control.expected_turn_delta = 4
            control.notes.append("近期局部故事过早收束；本场必须经过升级、转折和可追踪后果后再收团。")

        if average("choice_count") < 1 or average("consequence_count") < 1:
            control.notes.append("近期缺少鲜明选择或后果；把两个都可行但代价不同的方向摆到现场。")
        if not latest.previous_consequence_recalled:
            control.notes.append("上一场选择后果没有进入本场现场；下一次开局先用人物反应、地点变化或代价回收它。")
        if not latest.session_identity_distinct or latest.memory_similarity_to_recent >= 0.72:
            control.expected_turn_delta = max(control.expected_turn_delta, 4)
            control.notes.append("近期场次记忆点过于相似；更换核心问题、标志物件、抉择代价与局部结果，而非只换地名。")
        if not latest.signature_image_evolved:
            control.notes.append("标志画面尚未因玩家选择发生可见变化；在收束前回到同一物件或景物兑现变化。")
        if not latest.local_payoff_present:
            control.notes.append("本场只铺线没有兑现；下一场优先完成一个救援、关系、地点或证据层面的局部结果。")
        return control

    @staticmethod
    def apply_budget(
        budget: PressureBudget,
        control: CampaignFeedbackControl,
    ) -> PressureBudget:
        if not control.auto_pressure_delta:
            return budget
        return replace(
            budget,
            max_auto_advance_clocks=max(
                0,
                budget.max_auto_advance_clocks + control.auto_pressure_delta,
            ),
        )
