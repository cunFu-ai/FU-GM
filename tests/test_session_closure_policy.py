from fu_gm.components.session_closure_policy import SessionActEvidence, SessionClosurePolicy
from fu_gm.models import SessionFeedbackSignals


def test_four_act_policy_moves_from_reversal_to_climax_then_aftermath() -> None:
    policy = SessionClosurePolicy()

    climax = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="reversal",
            scene_change_recommended=True,
            reversal_reached=True,
            concrete_consequence=True,
        ),
    )
    aftermath = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="climax",
            local_question_resolved=True,
            local_payoff_present=True,
            concrete_consequence=True,
        ),
    )

    assert climax.advance and climax.next_act == 3
    assert aftermath.advance and aftermath.next_act == 4


def test_four_act_policy_never_talks_over_blocking_choice() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="closure",
            local_question_resolved=True,
            local_payoff_present=True,
        ),
        has_blocking_decision=True,
    )

    assert not decision.advance
    assert decision.next_act == 3


def test_opening_can_change_scene_after_opposition_consequence_lands() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=False,
            concrete_consequence=True,
            opposition_move_present=True,
            local_question_changed=True,
            npc_answer_complete=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 2


def test_opening_can_cut_after_current_scene_opposition_forces_a_new_situation() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=True,
            concrete_consequence=True,
            opposition_move_present=True,
            npc_answer_complete=True,
            scene_evidence_available=True,
            current_scene_player_actions=4,
            current_scene_material_change=True,
            current_scene_opposition_move=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 2


def test_prior_scene_progress_cannot_complete_a_fresh_empty_camera() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="reversal",
            scene_change_recommended=True,
            reversal_reached=True,
            concrete_consequence=True,
            scene_evidence_available=True,
            current_scene_player_actions=0,
            current_scene_material_change=False,
        ),
    )

    assert not decision.advance
    assert "没有英雄实际介入" in decision.reason


def test_current_exploration_scene_must_earn_its_own_reversal() -> None:
    policy = SessionClosurePolicy()

    stale = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="reversal",
            scene_change_recommended=True,
            reversal_reached=True,
            concrete_consequence=True,
            scene_evidence_available=True,
            current_scene_player_actions=3,
            current_scene_material_change=True,
            current_scene_reversal=False,
        ),
    )
    earned = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="reversal",
            scene_change_recommended=True,
            reversal_reached=True,
            concrete_consequence=True,
            scene_evidence_available=True,
            current_scene_player_actions=3,
            current_scene_material_change=True,
            current_scene_reversal=True,
        ),
    )

    assert not stale.advance
    assert earned.advance and earned.next_act == 3


def test_semantic_reversal_can_use_a_reveal_committed_in_current_scene() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="reversal",
            scene_change_recommended=True,
            reversal_reached=True,
            concrete_consequence=True,
            scene_evidence_available=True,
            current_scene_player_actions=3,
            current_scene_material_change=True,
            current_scene_reveal=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 3


def test_opening_cannot_leave_an_unfulfilled_finite_npc_bargain() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=True,
            concrete_consequence=True,
            local_payoff_present=True,
            npc_answer_complete=True,
            unresolved_scene_condition=True,
        ),
    )

    assert not decision.advance
    assert "承诺尚未兑现" in decision.reason


def test_opening_leaves_after_finite_npc_bargain_pays_out() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=False,
            local_question_changed=True,
            concrete_consequence=True,
            local_payoff_present=True,
            npc_answer_complete=True,
            opposition_move_present=False,
            unresolved_scene_condition=False,
        ),
    )

    assert decision.advance
    assert decision.next_act == 2


def test_opening_does_not_leave_for_a_clue_without_a_paid_out_result() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            local_question_changed=True,
            concrete_consequence=True,
            local_payoff_present=False,
            npc_answer_complete=True,
        ),
    )

    assert not decision.advance
    assert decision.next_act == 1


def test_opening_needs_a_local_outcome_not_only_a_minor_physical_change() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=1,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=True,
            concrete_consequence=True,
            npc_answer_complete=True,
        ),
    )

    assert not decision.advance
    assert decision.next_act == 1


def test_clue_without_reversal_cannot_skip_exploration_scene() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="development",
            scene_change_recommended=True,
            concrete_consequence=True,
            reversal_reached=False,
        ),
    )

    assert not decision.advance
    assert decision.next_act == 2


def test_repeated_development_loop_with_committed_opposition_move_enters_climax() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=2,
        evidence=SessionActEvidence(
            stage="development",
            repeated_loop_detected=True,
            opposition_move_present=True,
            concrete_consequence=True,
            npc_answer_complete=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 3
    assert "正面对决" in decision.reason


def test_resolved_core_question_enters_aftermath_even_if_stage_label_lags() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="development",
            local_question_resolved=True,
            local_payoff_present=True,
            npc_answer_complete=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 4


def test_changed_core_question_with_payoff_after_reversal_enters_aftermath() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="reversal",
            local_question_changed=True,
            local_payoff_present=True,
            reversal_reached=True,
            concrete_consequence=True,
            deliberate_cliffhanger=True,
            npc_answer_complete=True,
        ),
    )

    assert decision.advance
    assert decision.next_act == 4
    assert "悬念收束" in decision.reason


def test_changed_but_unresolved_question_cannot_enter_aftermath_without_cliffhanger() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="climax",
            local_question_changed=True,
            local_payoff_present=True,
            reversal_reached=True,
            concrete_consequence=True,
            npc_answer_complete=True,
        ),
    )

    assert not decision.advance
    assert decision.next_act == 3


def test_open_scene_condition_blocks_climax_to_aftermath_transition() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="closure",
            local_question_resolved=True,
            local_payoff_present=True,
            npc_answer_complete=True,
            unresolved_scene_condition=True,
        ),
    )

    assert not decision.advance
    assert "未结算" in decision.reason


def test_unperformed_accepted_scene_commitment_blocks_session_completion() -> None:
    policy = SessionClosurePolicy()
    feedback = SessionFeedbackSignals(
        session_number=1,
        meaningful_turns=32,
        scene_count=3,
        local_question_changed=True,
        deliberate_cliffhanger=True,
        reversal_reached=True,
        choice_count=2,
        consequence_count=2,
        villain_move_observed=True,
        memory_anchor_complete=True,
        signature_image_evolved=True,
        local_payoff_present=True,
        pending_scene_commitment_count=1,
    )

    decision = policy.assess_completion(
        feedback,
        minimum_scenes=3,
        minimum_turns=28,
    )

    assert not decision.can_end
    assert any("尚未实际履行" in reason for reason in decision.reasons)


def test_changed_question_without_payoff_or_consequence_stays_in_climax() -> None:
    policy = SessionClosurePolicy()

    decision = policy.recommend_act(
        current_act=3,
        evidence=SessionActEvidence(
            stage="reversal",
            local_question_changed=True,
            reversal_reached=True,
            npc_answer_complete=True,
        ),
    )

    assert not decision.advance
    assert decision.next_act == 3
