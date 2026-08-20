from __future__ import annotations

import math
from dataclasses import dataclass, field

from fu_gm.models import SessionFeedbackSignals


_STAGE_RANK = {
    "opening": 0,
    "development": 1,
    "reversal": 2,
    "climax": 3,
    "closure": 4,
}


@dataclass(frozen=True)
class SessionActEvidence:
    """Public evidence used to move between scenes inside one table session."""

    stage: str = "opening"
    scene_change_recommended: bool = False
    local_question_changed: bool = False
    local_question_resolved: bool = False
    deliberate_cliffhanger: bool = False
    reversal_reached: bool = False
    concrete_consequence: bool = False
    npc_answer_complete: bool = True
    opposition_move_present: bool = False
    local_payoff_present: bool = False
    repeated_loop_detected: bool = False
    unresolved_scene_condition: bool = False
    scene_evidence_available: bool = False
    current_scene_player_actions: int = 0
    current_scene_material_change: bool = False
    current_scene_local_outcome: bool = False
    current_scene_opposition_move: bool = False
    current_scene_reveal: bool = False
    current_scene_reversal: bool = False
    current_scene_core_resolution: bool = False


@dataclass(frozen=True)
class SessionActDecision:
    current_act: int
    next_act: int
    advance: bool
    reason: str = ""


@dataclass(frozen=True)
class SessionClosureDecision:
    can_end: bool
    reasons: list[str] = field(default_factory=list)


class SessionClosurePolicy:
    """Evidence-first lifecycle for a complete session-sized story.

    The policy never chooses an outcome. It only prevents a scheduler from
    mistaking elapsed turns, an outline beat, or a newly found clue for a
    completed evening of play.
    """

    FINAL_ACT = 4

    @staticmethod
    def dense_two_scene_resolution(
        feedback: SessionFeedbackSignals,
        *,
        minimum_turns: int,
    ) -> bool:
        """允许真正完成的高密度两场景单元收束。

        三段式场景通常更容易形成完整的一场，但它不应成为脱离桌面
        证据的硬门槛。两段场景只有在行动量显著更高、本场问题已经
        解决、对立方确实行动、因果与记忆锚点完整，且没有待决事务时
        才能替代第三段场景。悬念本身不能触发此例外。
        """

        dense_turn_floor = max(
            30,
            int(math.ceil(max(20, int(minimum_turns)) * 1.5)),
        )
        return bool(
            feedback.scene_count >= 2
            and feedback.meaningful_turns >= dense_turn_floor
            and feedback.local_question_resolved
            and feedback.choice_count >= 1
            and feedback.consequence_count >= 1
            and feedback.villain_move_observed
            and feedback.memory_anchor_complete
            and feedback.session_identity_distinct
            and feedback.signature_image_evolved
            and feedback.local_payoff_present
            and feedback.cause_effect_linked
            and feedback.gm_control_present
            and feedback.npc_answer_complete
            and feedback.player_agency_preserved
            and feedback.pending_blocking_decision_count == 0
            and feedback.pending_scene_commitment_count == 0
        )

    def recommend_act(
        self,
        *,
        current_act: int,
        evidence: SessionActEvidence,
        has_blocking_decision: bool = False,
    ) -> SessionActDecision:
        if has_blocking_decision:
            return SessionActDecision(
                current_act=current_act,
                next_act=current_act,
                advance=False,
                reason="仍有必须由玩家处理的待决选择。",
            )
        if current_act >= self.FINAL_ACT:
            return SessionActDecision(current_act, current_act, False)

        rank = _STAGE_RANK.get(str(evidence.stage or "opening"), 0)
        material_change = bool(
            evidence.local_question_changed
            or evidence.concrete_consequence
            or evidence.reversal_reached
            or evidence.opposition_move_present
        )
        if evidence.scene_evidence_available:
            # Cumulative session evidence is useful for closure, but it must
            # not let an earlier scene's reversal or payoff auto-complete a new
            # camera that has not yet produced anything at the table.
            if evidence.current_scene_player_actions < 1:
                return SessionActDecision(
                    current_act,
                    current_act,
                    False,
                    "当前镜头还没有英雄实际介入，不能只靠场景开场继续转场。",
                )
            if not evidence.current_scene_material_change:
                return SessionActDecision(
                    current_act,
                    current_act,
                    False,
                    "当前镜头尚未产生公开变化，不能把上一幕的进展重复计算。",
                )

        scene_change_earned = bool(
            evidence.scene_change_recommended
            or evidence.repeated_loop_detected
            or (
                current_act == 1
                and evidence.local_payoff_present
                and evidence.local_question_changed
                and evidence.concrete_consequence
                and not evidence.unresolved_scene_condition
            )
            or (
                current_act == 1
                and evidence.concrete_consequence
                and evidence.opposition_move_present
            )
        )
        if current_act < 3 and not scene_change_earned:
            return SessionActDecision(current_act, current_act, False)

        if current_act == 1:
            forced_exit = bool(
                evidence.scene_change_recommended
                and evidence.concrete_consequence
                and evidence.opposition_move_present
            )
            if evidence.unresolved_scene_condition and not forced_exit:
                return SessionActDecision(
                    current_act,
                    current_act,
                    False,
                    "NPC已经提出有限条件，但承诺尚未兑现；当前镜头仍有必须落地的局部结果。",
                )
            local_outcome = bool(
                evidence.local_payoff_present
                or evidence.local_question_changed
                or evidence.local_question_resolved
                or forced_exit
            )
            if evidence.scene_evidence_available:
                local_outcome = bool(
                    local_outcome
                    and (
                        evidence.current_scene_local_outcome
                        or (
                            forced_exit
                            and evidence.current_scene_material_change
                            and evidence.current_scene_opposition_move
                        )
                    )
                )
            ready = bool(
                rank >= 1
                and material_change
                and local_outcome
                and evidence.npc_answer_complete
            )
            return SessionActDecision(
                current_act,
                2 if ready else current_act,
                ready,
                "开场局面已经因行动或对立方动作发生实质变化。" if ready else "",
            )

        if current_act == 2:
            revealed_reversal = bool(
                rank >= 2
                and evidence.reversal_reached
                and material_change
                and evidence.npc_answer_complete
            )
            if evidence.scene_evidence_available:
                revealed_reversal = bool(
                    revealed_reversal
                    and (
                        evidence.current_scene_reversal
                        or evidence.current_scene_reveal
                    )
                )
            # A situation can earn its reversal through play before the
            # semantic evaluator labels a prepared secret as "the reversal".
            # When the table is demonstrably looping and the opposition has
            # already changed the situation, cutting to the confrontation is
            # the GM cashing in that pressure, not a turn-count shortcut.
            forced_confrontation = bool(
                evidence.repeated_loop_detected
                and evidence.opposition_move_present
                and material_change
                and evidence.npc_answer_complete
            )
            if evidence.scene_evidence_available:
                forced_confrontation = bool(
                    forced_confrontation
                    and evidence.current_scene_opposition_move
                )
            ready = revealed_reversal or forced_confrontation
            return SessionActDecision(
                current_act,
                3 if ready else current_act,
                ready,
                (
                    "转折已经公开，局面可以进入高潮。"
                    if revealed_reversal
                    else "当前做法已经穷尽，对立方的行动迫使局面进入正面对决。"
                    if forced_confrontation
                    else ""
                ),
            )

        resolved_ending_earned = bool(
            evidence.local_question_resolved
            and evidence.local_payoff_present
        )
        cliffhanger_earned = bool(
            evidence.deliberate_cliffhanger
            and evidence.local_question_changed
            and evidence.local_payoff_present
            and evidence.reversal_reached
            and evidence.concrete_consequence
        )
        ready = bool(
            evidence.npc_answer_complete
            and not evidence.unresolved_scene_condition
            and (resolved_ending_earned or cliffhanger_earned)
        )
        if evidence.scene_evidence_available:
            ready = bool(
                ready
                and evidence.current_scene_local_outcome
                and evidence.current_scene_core_resolution
            )
        return SessionActDecision(
            current_act,
            self.FINAL_ACT if ready else current_act,
            ready,
            (
                "转折后的选择已经兑现局部结果，并形成了明确的悬念收束。"
                if cliffhanger_earned and not evidence.local_question_resolved
                else "高潮已经兑现结果，进入余波与收束。"
                if ready
                else "当前高潮仍有公开条件或核心问题未结算，不能提前进入余波。"
                if evidence.unresolved_scene_condition
                else ""
            ),
        )

    def assess_completion(
        self,
        feedback: SessionFeedbackSignals,
        *,
        minimum_scenes: int,
        minimum_turns: int,
    ) -> SessionClosureDecision:
        reasons: list[str] = []
        required_scenes = max(3, int(minimum_scenes))
        dense_two_scene_resolution = self.dense_two_scene_resolution(
            feedback,
            minimum_turns=minimum_turns,
        )
        if feedback.scene_count < required_scenes and not dense_two_scene_resolution:
            reasons.append(f"本场还没有形成至少 {required_scenes} 个有实质变化的场景段落。")
        if feedback.meaningful_turns < max(20, int(minimum_turns)):
            reasons.append("本场有意义的桌面交换仍偏少，局面还没充分展开。")

        local_closed = feedback.local_question_resolved
        cliffhanger_earned = bool(
            feedback.deliberate_cliffhanger
            and feedback.reversal_reached
            and feedback.local_question_changed
            and feedback.local_payoff_present
        )
        if not local_closed and not cliffhanger_earned:
            reasons.append("本场核心问题尚未解决，也还没有形成经过转折铺垫并兑现局部回报的悬念收束。")
        if feedback.choice_count < 1 or feedback.consequence_count < 1:
            reasons.append("本场还缺少一个由玩家作出、并在世界中兑现后果的选择。")
        if not feedback.villain_move_observed:
            reasons.append("对立方或环境尚未根据自身目标主动改变局面。")
        if not feedback.memory_anchor_complete:
            reasons.append("本场还没有同时留下具体画面、玩家选择和可追踪后果。")
        if not feedback.session_identity_distinct or not feedback.signature_image_evolved:
            reasons.append("本场标志画面还没有随局势发生变化，记忆点仍不够独立。")
        if not feedback.local_payoff_present:
            reasons.append("本场还没有兑现局部回报；只增加新线索不能代替小故事的阶段性结果。")
        if not feedback.cause_effect_linked or not feedback.gm_control_present:
            reasons.append("玩家行动、GM回应与对立方动作尚未形成清楚的因果链。")
        if not feedback.npc_answer_complete or not feedback.player_agency_preserved:
            reasons.append("仍有NPC答复或玩家自主性问题，不能把当前实录视为完整收束。")
        if feedback.pending_blocking_decision_count:
            reasons.append("仍有玩家待决选择未处理，不能先行收团。")
        if feedback.pending_scene_commitment_count:
            reasons.append("仍有已经谈妥、但尚未实际履行的场景承诺，不能把条款成立当作结果兑现。")
        return SessionClosureDecision(can_end=not reasons, reasons=reasons)
