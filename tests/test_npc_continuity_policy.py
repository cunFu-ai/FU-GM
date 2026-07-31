from fu_gm.components.npc_continuity_policy import NPCCommitmentBoundary, NPCContinuityPolicy
from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.models import Action, ActionType, Clock


def _plan(*, direct_answer: str, condition: str = "") -> Action:
    return Action(
        ActionType.NARRATE,
        {
            "npc_speech_plan": {
                "speech_act": "condition" if condition else "answer",
                "direct_answer": direct_answer,
                "condition": condition,
                "promised_result": "",
            }
        },
    )


def test_explicit_changed_terms_are_detected() -> None:
    statement = "不等了。财团今天改口，不收一段一段的说辞，改收你们当场给出的立场。"

    assert NPCContinuityPolicy.explicitly_revises_prior_terms(statement)
    assert not NPCContinuityPolicy.explicitly_revises_prior_terms(
        "财团巡逻队已经到了门外。"
    )


def test_old_prepared_price_cannot_silently_return_after_public_revision() -> None:
    action = _plan(
        direct_answer="条件不变，今天只认你们把那一小段去路说到完整。",
        condition="把那一小段去路说到完整",
    )
    prepared = {
        "concrete_demand": "把失名旅人记得的那一小段去路完整说出",
        "acceptance_rule": "路线的起点、方向和终点都说明白",
    }

    assert NPCContinuityPolicy.reopens_superseded_bargain(
        action,
        prepared=prepared,
        supersedes_prior_terms=True,
    )


def test_a_second_explicit_revision_is_allowed_when_acknowledged() -> None:
    action = _plan(
        direct_answer="我刚才确实改过口，但现在我再次改主意：去路仍由你们保管，我只带走书面立场。",
    )

    assert not NPCContinuityPolicy.reopens_superseded_bargain(
        action,
        prepared={"concrete_demand": "完整说出去路"},
        supersedes_prior_terms=True,
    )


def test_ordinary_npc_plan_cannot_claim_unfinished_clock_consequence() -> None:
    action = _plan(direct_answer="巡逻者已经散开封住驿站外沿。")
    boundaries = ClockNarrativeBoundary.packet(
        [
            Clock(
                name="财团巡逻队逼近",
                max_segments=8,
                current=3,
                clock_type="threat",
                stakes="填满后财团巡逻队包围驿站。",
            )
        ]
    )

    assert "仍为 3/8" in NPCContinuityPolicy.clock_boundary_violation(
        action,
        boundaries,
    )


def test_explicit_private_deception_may_conflict_with_objective_clock_state() -> None:
    action = _plan(direct_answer="巡逻者已经散开封住驿站外沿。")
    action.parameters["npc_speech_plan"]["intent"] = "故意误导玩家，让他们误以为退路已断"
    boundaries = ClockNarrativeBoundary.packet(
        [
            Clock(
                name="财团巡逻队逼近",
                max_segments=8,
                current=3,
                clock_type="threat",
                stakes="填满后财团巡逻队包围驿站。",
            )
        ]
    )

    assert NPCContinuityPolicy.clock_boundary_violation(action, boundaries) == ""


def test_pending_handoff_blocks_instant_object_possession() -> None:
    latest = (
        "把那一页登记和这段路段记录拿到我眼前，"
        "我就当场对照这段痕迹。"
    )
    action = _plan(direct_answer="我现在就核对登记与路段记录。")
    action.parameters["npc_speech_plan"]["nonverbal_reaction"] = "伸手接过记录，低头比对"

    violation = NPCContinuityPolicy.object_access_violation(
        action,
        player_message="那就把它们拿来，我现在就当场看。",
        latest_public_statement=latest,
    )

    assert "没有完成交付" in violation
    assert "接过记录" in violation or "核对登记" in violation


def test_actual_player_handoff_allows_npc_to_open_the_object() -> None:
    latest = "把那页记录拿到我眼前，我就当场核对。"
    action = _plan(direct_answer="我现在开始核对记录。")
    action.parameters["npc_speech_plan"]["nonverbal_reaction"] = "接过记录并翻开"

    assert NPCContinuityPolicy.object_access_violation(
        action,
        player_message="艾薇娅把那页记录递给艾蕾娜。",
        latest_public_statement=latest,
    ) == ""


def test_future_object_access_does_not_claim_the_handoff_is_complete() -> None:
    latest = "把那页记录拿到我眼前，我就当场核对。"
    action = _plan(direct_answer="东西还没到我手里；拿到以后我才能核对记录。")

    assert NPCContinuityPolicy.object_access_violation(
        action,
        player_message="那就把它拿来。",
        latest_public_statement=latest,
    ) == ""


def test_fulfilled_exchange_cannot_add_an_unagreed_custody_requirement() -> None:
    terms = "接受先查明失忆旅人反应原因再决定开放旧路；原因未明前不放行。"
    text = "我现在为你们打开旧路，但失忆旅人必须留在守望会的视线内。"

    assert NPCContinuityPolicy.unapproved_fulfilled_payout_restriction(
        text,
        settled_terms=terms,
    ) == "失忆旅人必须留在守望会的视线内"
    assert NPCContinuityPolicy.strip_unapproved_fulfilled_payout_restrictions(
        text,
        settled_terms=terms,
    ) == "我现在为你们打开旧路。"


def test_fulfilled_exchange_preserves_a_restriction_that_was_already_agreed() -> None:
    terms = "先查明原因再开放旧路；失忆旅人必须留在守望会的视线内。"
    text = "我现在为你们打开旧路，但失忆旅人必须留在守望会的视线内。"

    assert NPCContinuityPolicy.unapproved_fulfilled_payout_restriction(
        text,
        settled_terms=terms,
    ) == ""
    assert NPCContinuityPolicy.strip_unapproved_fulfilled_payout_restrictions(
        text,
        settled_terms=terms,
    ) == text


def test_inspection_completion_must_honor_a_public_retreat_promise() -> None:
    ledger = [
        {
            "npc": "财团使者",
            "aliases": ["门外的使者"],
            "statements": [
                "先验这件遗物。验完之后，我只退开，不再碰登记小室里的别的东西。"
            ],
        }
    ]
    commitments = NPCCommitmentBoundary.due_commitments(ledger)

    violation = NPCCommitmentBoundary.violation(
        {
            "npc_speakers": [
                {
                    "npc": "门外的使者",
                    "public_statement": "验到这里已经够了。她没有退开，反而屈指敲了敲门板。",
                }
            ]
        },
        commitments,
    )

    assert len(commitments) == 1
    assert "必须让其实际退开" in violation


def test_inspection_completion_may_proceed_when_the_npc_retires_as_promised() -> None:
    commitments = NPCCommitmentBoundary.due_commitments(
        [
            {
                "npc": "财团使者",
                "statements": ["验完之后，我会退开，不再碰登记小室里的别的东西。"],
            }
        ]
    )

    assert NPCCommitmentBoundary.violation(
        {
            "npc_speakers": [
                {
                    "npc": "财团使者",
                    "public_statement": "验到这里已经够了。她收起验片，向后退开两步。",
                }
            ]
        },
        commitments,
    ) == ""


def test_single_pending_commitment_cannot_be_evaded_by_later_naming_the_npc() -> None:
    commitments = NPCCommitmentBoundary.due_commitments(
        [
            {
                "npc": "门外那位财团来者",
                "statements": ["验完之后，我会退开，不再碰登记小室里的别的东西。"],
            }
        ]
    )

    violation = NPCCommitmentBoundary.violation(
        {
            "npc_speakers": [
                {
                    "npc": "监察官艾蕾娜",
                    "public_statement": "验已经看过了，轮到你们做决定。",
                }
            ]
        },
        commitments,
    )

    assert "先前公开承诺核验完成后退开" in violation
