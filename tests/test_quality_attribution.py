from fu_gm.testing.conversation_quality import ConversationQualityReport
from fu_gm.testing.quality_attribution import LongRunIssueAttributor
from fu_gm.testing.session_progress_evaluator import SessionProgressAssessment


def test_attribution_separates_provider_model_and_framework_failures() -> None:
    checks = {
        "npc_answers_complete": False,
        "no_repeated_out_of_turn_deadlock": False,
        "model_latency_reported_and_bounded": False,
        "session_experience_uses_core_formula": True,
    }
    attribution = LongRunIssueAttributor.classify(
        configured_model="gpt-5.6-luna",
        calls=[
            {
                "index": 4,
                "label": "NPC回应",
                "status": 502,
                "elapsed_ms": 1200,
                "error": "upstream request failed",
            }
        ],
        assessments=[
            SessionProgressAssessment(
                npc_answer_complete=False,
                evidence=["玩家问路后NPC没有回答。"],
            )
        ],
        checks=checks,
        quality=ConversationQualityReport(),
    )

    assert attribution["provider_availability"][0]["status"] == 502
    assert "npc_answer_incomplete" in attribution["model_or_prompt_behavior"][0]["failures"]
    assert attribution["fu_gm_framework"] == [
        {
            "kind": "out_of_turn_deadlock",
            "failed_check": "no_repeated_out_of_turn_deadlock",
        }
    ]


def test_mechanical_ok_scope_excludes_model_and_provider_checks_only() -> None:
    mechanical = LongRunIssueAttributor.mechanical_checks(
        {
            "npc_answers_complete": False,
            "model_latency_reported_and_bounded": False,
            "short_lived_clocks_cleaned": True,
            "no_sticky_opportunity_preference": False,
        }
    )

    assert "npc_answers_complete" not in mechanical
    assert "model_latency_reported_and_bounded" not in mechanical
    assert mechanical == {
        "short_lived_clocks_cleaned": True,
        "no_sticky_opportunity_preference": False,
    }


def test_successful_call_with_timeout_configuration_is_not_provider_failure() -> None:
    issues = LongRunIssueAttributor._provider_issues(
        [
            {
                "index": 3,
                "label": "健康仪表盘",
                "status": 200,
                "ok": True,
                "body": {
                    "llm": {
                        "timeout_seconds": 60,
                        "last_call": {"model": "gpt-5.6-luna", "ok": True},
                    }
                },
            }
        ]
    )

    assert issues == []


def test_failed_nested_llm_attempt_is_reported_without_marking_dashboard_failed() -> None:
    issues = LongRunIssueAttributor._provider_issues(
        [
            {
                "index": 50,
                "label": "最终审计仪表盘",
                "status": 200,
                "ok": True,
                "body": {
                    "llm": {
                        "recent_calls": [
                            {
                                "model": "gpt-5.6-luna",
                                "ok": False,
                                "attempt": 2,
                                "endpoint": "https://example.test/v1/chat/completions",
                                "elapsed_ms": 20_000,
                                "error": "The read operation timed out",
                            }
                        ]
                    }
                },
            }
        ]
    )

    assert issues == [
        {
            "kind": "llm_attempt_failure",
            "index": 50,
            "label": "最终审计仪表盘",
            "model": "gpt-5.6-luna",
            "endpoint": "https://example.test/v1/chat/completions",
            "attempt": 2,
            "elapsed_ms": 20_000,
            "error": "The read operation timed out",
        }
    ]


def test_unexercised_framework_check_is_not_attributed_or_counted_mechanical() -> None:
    checks = {
        "session_experience_uses_core_formula": False,
        "free_discussion_silent_samples": False,
    }
    applicability = {
        "session_experience_uses_core_formula": False,
        "free_discussion_silent_samples": True,
    }
    attribution = LongRunIssueAttributor.classify(
        configured_model="gpt-5.6-luna",
        calls=[],
        assessments=[],
        checks=checks,
        check_applicability=applicability,
        quality=ConversationQualityReport(),
    )

    assert attribution["fu_gm_framework"] == [
        {
            "kind": "group_message_routing_overreply",
            "failed_check": "free_discussion_silent_samples",
        }
    ]
    assert LongRunIssueAttributor.mechanical_checks(
        checks,
        check_applicability=applicability,
    ) == {"free_discussion_silent_samples": False}


def test_invalid_structured_output_is_model_behavior_not_provider_outage() -> None:
    call = {
        "index": 52,
        "label": "第一场询问NPC",
        "status": 200,
        "ok": True,
        "body": {
            "llm_invalid_output": True,
            "llm_failure_kind": "invalid_json",
            "error": "NPC decision returned invalid JSON",
        },
    }

    attribution = LongRunIssueAttributor.classify(
        configured_model="gpt-5.6-luna",
        calls=[call],
        assessments=[],
        checks={},
        quality=ConversationQualityReport(),
    )

    assert attribution["provider_availability"] == []
    assert attribution["model_or_prompt_behavior"] == [
        {
            "kind": "model_structured_output_invalid",
            "index": 52,
            "label": "第一场询问NPC",
            "failure_kind": "invalid_json",
            "error": "NPC decision returned invalid JSON",
        }
    ]
