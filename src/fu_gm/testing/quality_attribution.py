from __future__ import annotations

import json
from typing import Any, Iterable

from fu_gm.testing.conversation_quality import ConversationQualityReport
from fu_gm.testing.session_progress_evaluator import SessionProgressAssessment


class LongRunIssueAttributor:
    """Separate framework defects from model behavior and provider outages."""

    MODEL_CHECKS = {
        "offline_session_evaluation_active",
        "memorable_anchor_per_session",
        "memory_anchors_are_distinct",
        "opposition_moves_each_session",
        "signature_image_present_at_each_opening",
        "concrete_npc_agenda_each_session",
        "signature_image_evolves_each_session",
        "local_payoff_each_session",
        "previous_consequence_recalled",
        "npc_answers_complete",
        "npc_personality_consistent",
        "player_agency_preserved",
        "player_actions_have_causal_feedback",
        "gm_control_present_per_session",
        "session_identity_distinct",
        "gm_responses_relevant",
        "gm_player_echo_rate_acceptable",
        "group_silence_precision_acceptable",
        "directed_reply_recall_acceptable",
        "near_duplicate_gm_reply_rate_acceptable",
        "tool_recovery_rate_acceptable",
        "no_exact_gm_reply_loop",
        "llm_player_simulator_active",
        "no_player_simulator_fallback",
        "player_simulator_outputs_valid",
        "player_action_lanes_diverse",
        "no_vague_gm_placeholders",
        "no_explanatory_player_intent_commentary",
    }
    PROVIDER_CHECKS = {
        "p95_latency_reported_and_bounded",
        "model_latency_reported_and_bounded",
        "no_unexpected_llm_fallback",
        "core_agent_available",
    }
    _FRAMEWORK_KINDS = {
        "short_lived_clocks_cleaned": "clock_lifecycle_leak",
        "no_repeated_out_of_turn_deadlock": "out_of_turn_deadlock",
        "no_sticky_opportunity_preference": "sticky_opportunity_state",
        "session_experience_uses_core_formula": "experience_formula_or_lifecycle",
        "no_backend_labels": "backend_label_leak",
        "free_discussion_silent_samples": "group_message_routing_overreply",
        "all_sessions_earned_an_ending": "session_advanced_before_local_story_finished",
        "no_blocking_decisions_at_session_end": "blocking_decision_lifecycle",
        "no_contradictory_check_responses": "check_resolution_contradiction",
        "no_retired_clock_reappearance": "retired_clock_reappearance",
        "no_premature_clock_consequences": "clock_consequence_boundary",
        "no_unbacked_state_change_claims": "agent_claim_without_tool_receipt",
        "no_failed_tool_success_claims": "failed_tool_reported_as_success",
    }
    _PROVIDER_MARKERS = (
        "llm http 502",
        "upstream request failed",
        "bad gateway",
        "remote end closed connection",
        "timed out",
        "timeout",
    )

    @classmethod
    def classify(
        cls,
        *,
        configured_model: str,
        calls: Iterable[dict[str, Any]],
        assessments: Iterable[SessionProgressAssessment],
        checks: dict[str, bool],
        check_applicability: dict[str, bool] | None = None,
        quality: ConversationQualityReport,
        player_validation_errors: list[dict[str, Any]] | None = None,
        repeated_long_replies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        call_rows = list(calls)
        semantic_rows = list(assessments)
        return {
            "configured_model": configured_model,
            "provider_availability": cls._provider_issues(call_rows),
            "model_or_prompt_behavior": cls._model_issues(
                semantic_rows,
                calls=call_rows,
                quality=quality,
                player_validation_errors=player_validation_errors or [],
                repeated_long_replies=repeated_long_replies or [],
            ),
            "fu_gm_framework": [
                {"kind": kind, "failed_check": check_name}
                for check_name, kind in cls._FRAMEWORK_KINDS.items()
                if checks.get(check_name) is False
                and (check_applicability or {}).get(check_name, True)
            ],
            "interpretation": (
                "供应端失败不归咎于FU-GM规则框架；语义、表达或角色边界失误先作为模型/提示词行为报告，"
                "不添加剧情关键词补丁。只有可复现的状态、规则、路由或生命周期错误列为框架问题。"
            ),
        }

    @classmethod
    def mechanical_checks(
        cls,
        checks: dict[str, bool],
        *,
        check_applicability: dict[str, bool] | None = None,
    ) -> dict[str, bool]:
        excluded = cls.MODEL_CHECKS | cls.PROVIDER_CHECKS
        applicability = check_applicability or {}
        return {
            name: passed
            for name, passed in checks.items()
            if name not in excluded and applicability.get(name, True)
        }

    @classmethod
    def _provider_issues(cls, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        seen_attempts: set[tuple[object, ...]] = set()
        for call in calls:
            status = int(call.get("status") or 0)
            direct_failed = status >= 500 or call.get("ok") is False or bool(call.get("error"))
            direct_error = " ".join(
                (
                    str(call.get("error") or ""),
                    str((call.get("body") or {}).get("error") or "")
                    if isinstance(call.get("body"), dict)
                    else "",
                    cls._diagnostic_errors(call.get("llm_diagnostics")),
                )
            ).strip()
            if direct_failed and cls._has_provider_marker(direct_error):
                issues.append(
                    {
                        "kind": "request_failure",
                        "index": call.get("index"),
                        "label": call.get("label"),
                        "status": status,
                        "elapsed_ms": call.get("elapsed_ms"),
                        "error": direct_error[:300],
                    }
                )

            for attempt in cls._failed_llm_attempts(call):
                error = str(attempt.get("error") or "")
                if not cls._has_provider_marker(error):
                    continue
                signature = (
                    attempt.get("at"),
                    attempt.get("model"),
                    attempt.get("endpoint"),
                    attempt.get("attempt"),
                    error,
                )
                if signature in seen_attempts:
                    continue
                seen_attempts.add(signature)
                issues.append(
                    {
                        "kind": "llm_attempt_failure",
                        "index": call.get("index"),
                        "label": call.get("label"),
                        "model": attempt.get("model"),
                        "endpoint": attempt.get("endpoint"),
                        "attempt": attempt.get("attempt"),
                        "elapsed_ms": attempt.get("elapsed_ms"),
                        "error": error[:300],
                    }
                )
        return issues

    @classmethod
    def _has_provider_marker(cls, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(marker in lowered for marker in cls._PROVIDER_MARKERS)

    @classmethod
    def _diagnostic_errors(cls, value: Any) -> str:
        errors: list[str] = []

        def collect(item: Any) -> None:
            if isinstance(item, dict):
                error = item.get("error")
                if error:
                    errors.append(str(error))
                for nested in item.values():
                    collect(nested)
            elif isinstance(item, list):
                for nested in item:
                    collect(nested)

        collect(value)
        return " ".join(errors)

    @staticmethod
    def _failed_llm_attempts(call: dict[str, Any]) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []

        def collect(item: Any) -> None:
            if isinstance(item, dict):
                if item.get("ok") is False and item.get("error") and item.get("model"):
                    attempts.append(item)
                for nested in item.values():
                    collect(nested)
            elif isinstance(item, list):
                for nested in item:
                    collect(nested)

        collect(call.get("body"))
        collect(call.get("llm_diagnostics"))
        return attempts

    @staticmethod
    def _model_issues(
        assessments: list[SessionProgressAssessment],
        *,
        calls: list[dict[str, Any]],
        quality: ConversationQualityReport,
        player_validation_errors: list[dict[str, Any]],
        repeated_long_replies: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for call in calls:
            body = call.get("body") if isinstance(call.get("body"), dict) else {}
            strict_failure = (
                call.get("strict_semantic_failure")
                if isinstance(call.get("strict_semantic_failure"), dict)
                else {}
            )
            invalid_output = bool(body.get("llm_invalid_output")) or (
                strict_failure.get("kind") == "model_invalid_output"
            )
            if not invalid_output:
                continue
            issues.append(
                {
                    "kind": "model_structured_output_invalid",
                    "index": call.get("index"),
                    "label": call.get("label"),
                    "failure_kind": str(
                        body.get("llm_failure_kind")
                        or strict_failure.get("failure_kind")
                        or "invalid_output"
                    ),
                    "error": str(body.get("error") or call.get("error") or "")[:300],
                }
            )
        fields = {
            "npc_answer_complete": "npc_answer_incomplete",
            "npc_personality_consistent": "npc_personality_drift",
            "player_agency_preserved": "player_agency_violation",
            "continuity_ok": "semantic_continuity_failure",
            "cause_effect_linked": "weak_action_consequence_link",
            "gm_control_present": "gm_scene_control_missing",
            "session_identity_distinct": "indistinct_session_identity",
            "gm_response_relevant": "irrelevant_gm_response",
        }
        for index, assessment in enumerate(assessments, start=1):
            failed = [kind for field, kind in fields.items() if not bool(getattr(assessment, field))]
            if failed:
                issues.append(
                    {
                        "kind": "semantic_session_failures",
                        "session": index,
                        "failures": failed,
                        "evidence": list(assessment.evidence[:3]),
                    }
                )
        if player_validation_errors:
            issues.append(
                {
                    "kind": "player_boundary_failures",
                    "evidence": player_validation_errors[:10],
                }
            )
        if repeated_long_replies:
            issues.append(
                {
                    "kind": "exact_long_reply_repetition",
                    "evidence": repeated_long_replies[:10],
                }
            )
        if quality.player_echo_rate > 0.12:
            issues.append(
                {
                    "kind": "player_echo_style",
                    "rate": quality.player_echo_rate,
                    "count": quality.player_echo_count,
                }
            )
        if quality.near_duplicate_gm_replies:
            issues.append(
                {
                    "kind": "near_duplicate_gm_style",
                    "count": quality.near_duplicate_gm_replies,
                }
            )
        return issues
