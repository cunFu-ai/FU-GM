from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolFreshnessGuard,
    GMToolParameter,
    GMToolReceipt,
    json_safe_value,
    GMToolRegistry,
)
from fu_gm.gm_tool_execution import GMToolCallLedger
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.gm_tool_protocol import (
    GMToolDecisionProtocolError,
    GMToolProtocol,
)
from fu_gm.context_governance import (
    GMContextBudget,
    GMContextGovernor,
)
from fu_gm.components.gm_agent_decision_requester import (
    GMToolAgentDecisionRequester,
)
from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_agent_failure_policy import GMToolAgentFailurePolicy
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_agent_loop_state import (
    GMAgentLoopPhase,
    GMAgentLoopState,
    GMAgentTerminalReason,
)
from fu_gm.components.gm_runtime_feedback import (
    GMRuntimeBudget,
    GMRuntimeFeedback,
    GMRuntimeFeedbackIssueCode,
    GMRuntimeFeedbackPhase,
    GMRuntimeFeedbackSeverity,
    GMRuntimeRecoveryAction,
    GMRuntimeTransactionStatus,
)
from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingVerifier,
)
from fu_gm.components.scene_change_authority import SceneChangeAuthorityPolicy
from fu_gm.components.gm_turn_state_delta import (
    GMTurnStateDeltaBudget,
    GMTurnStateDeltaTracker,
    apply_state_delta,
    projection_hash,
)
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.components.gm_semantic_tool_attestation import (
    clear_semantic_tool_attestations,
    remember_semantic_tool_attestations,
)
from fu_gm.components.gm_agent_prompts import (
    CORE_AGENT_SYSTEM_PREFIX,
    CORE_PUBLIC_EXPRESSION_CONTRACT,
    CORE_GM_SYSTEM_PROMPT,
    HEARTBEAT_SYSTEM_PROMPT,
    POST_TOOL_SYSTEM_PROMPT,
    TABLE_CHAT_HEARTBEAT_SYSTEM_PROMPT,
    TURN_STATE_DELTA_SYSTEM_PROMPT,
    build_initial_gm_system_prompt,
)
from fu_gm.components.gm_batch_tool_transaction import GMBatchToolTransaction
from fu_gm.components.gm_scene_batch_scheduler import GMSceneBatchScheduler
from fu_gm.components.gm_message_tool_transaction import (
    GMMessageToolTransaction,
)
from fu_gm.components.gm_message_envelope import GMMessageEnvelopeBuilder
from fu_gm.components.gm_message_integrity import (
    GMMessageIntegrityIssue,
    GMMessageIntegrityPlan,
    GMMessageIntegrityValidator,
    GMProposalConfirmationRequirement,
)
from fu_gm.components.gm_message_semantics import (
    GMMessageSemantics,
    GMMessageSemanticsError,
    HERO_STATE_INTENT_SUBJECT_TO_PATCH_FIELD,
    semantic_change_tool_names,
    tool_semantic_authority_error,
)
from fu_gm.components.world_setting_catalog import WorldSettingCatalog
from fu_gm.conversation.reply import DeliveryIntent
from fu_gm.llm_client import ChatMessage
from fu_gm.prompt_cache import (
    GM_DELTA_PROMPT_LAYOUT_VERSION,
    GM_PROMPT_LAYOUT_VERSION,
    build_cache_friendly_messages,
    prompt_layout_fingerprint,
)


class LLMGMToolAgent:
    """Small observe/act/observe loop for explicit GM capabilities.

    The model owns semantic intent and tool choice. The registry owns schemas,
    preconditions and side effects. A public reply is produced only after the
    relevant tool receipt exists, so prose cannot make an operation true.
    """

    # These tools finish one table-facing rules or scene transaction.  Once
    # their authoritative reply is committed, a second narrative rules tool
    # would resolve the same player message twice (for example Investigate +
    # Guard, or start_scene + commit_scene_response).  NPC dialogue remains
    # deliberately non-terminal because an accepted invitation may be
    # followed by one scene transition in the same transaction.
    _TERMINAL_PUBLIC_TOOLS = frozenset(
        {
            "start_adventure",
            "start_session",
            "propose_session_zero_update",
            "pause_session",
            "end_session",
            "start_scene",
            "transition_scene",
            "end_scene",
            "start_conflict",
            "end_conflict",
            "commit_scene_response",
            "introduce_npc",
            "declare_check_action",
            "declare_movement_check",
            "perform_check_action",
            "perform_character_action",
			"perform_scene_action",
			"perform_in_scene_action",
			"commit_story_item_action",
			"move_group_within_scene",
			"move_scene_group",
			"pass_in_scene_action",
			"set_absent_character_mode",
			"perform_ritual_project_action",
            "resolve_rule_window",
            "resolve_gm_opportunity",
            "run_current_npc_turn",
            "create_clock",
            "fill_clock",
            "erase_clock",
            "close_clock",
            "travel_party",
            "award_stage_reward",
            "generate_world_map_preview",
            "get_world_map_status",
            "edit_world_map",
            "delegate_background_task",
            "cancel_background_task",
            "resume_background_task",
        }
    )
    # ``python_auto_execute`` is a privileged receipt capability, not an open
    # extension hook. Every currently valid producer signs one of these exact
    # deterministic follow-ups. New tools must receive an explicit security
    # review and code change before a receipt marker can bypass another model
    # observation round.
    _PYTHON_AUTO_EXECUTE_TOOLS = frozenset(
        {
            "select_first_act",
            "create_world_setting",
            "update_world_setting",
            "delete_world_setting",
            "rename_world_setting",
            "run_current_npc_turn",
            "generate_world_map_preview",
            "get_world_map_status",
        }
    )
    _receipt_policy = GMToolReceiptPolicy
    _protocol = GMToolProtocol
    _failure_policy = GMToolAgentFailurePolicy
    _capability_policy = GMToolAgentCapabilityPolicy
    _message_integrity_validator = GMMessageIntegrityValidator
    _MESSAGE_INTEGRITY_METADATA_KEY = "_gm_message_integrity_issue"
    _MESSAGE_INTEGRITY_PHASE_METADATA_KEY = (
        "_gm_message_integrity_issue_phase"
    )
    _LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY = (
        "_gm_last_semantic_protocol_error"
    )
    _POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY = (
        "_gm_post_tool_completion_obligation"
    )
    _MESSAGE_SEMANTICS_METADATA_KEY = "_gm_message_semantics"
    _PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY = (
        "_gm_provisional_message_semantics"
    )
    _TRANSACTION_RECOVERY_GRACE_ITERATIONS = 2
    _STATE_CHANGE_ACKNOWLEDGEMENT_HINT = (
        "选择final，用一句与成功回执直接相关的自然确认收束；"
        "内容止于本次结果。"
    )
    _NATURAL_OPPORTUNITY_EFFECTS = frozenset(
        {
            "reveal",
            "揭示",
            "information",
            "情报",
            "favor",
            "青睐",
            "scan",
            "审视",
            "misstep",
            "失态",
            "lost_item",
            "失物",
            "twist",
            "转折",
            "custom",
            "自定义",
        }
    )
    _SEMANTIC_PREFLIGHT_TOOLS = frozenset(
        {
            "commit_scene_response",
            "commit_story_item_action",
            "confirm_session_zero_proposal",
            "create_npc_profile",
            "decide_collective_action",
            "decide_collective_response",
            "decide_npc_action",
            "decide_npc_response",
            "declare_check_action",
            "declare_movement_check",
            "end_conflict",
            "end_session",
            "introduce_npc",
            "move_group_within_scene",
            "move_scene_group",
            "perform_character_action",
            "perform_check_action",
            "perform_in_scene_action",
            "revise_npc_profile",
            "start_conflict",
            "start_scene",
            "transition_scene",
            "update_npc_state",
        }
    )
    _RULES_RISK_TOOLS = frozenset(
        {
            "declare_check_action",
            "declare_movement_check",
            "perform_check_action",
            "perform_character_action",
            "perform_scene_action",
            "perform_ritual_project_action",
            "resolve_rule_window",
            "resolve_gm_opportunity",
            "start_conflict",
            "end_conflict",
            "run_current_npc_turn",
            "create_clock",
            "fill_clock",
            "erase_clock",
            "close_clock",
            "start_dungeon",
            "end_dungeon",
            "travel_party",
            "continue_travel",
            "award_stage_reward",
            "level_up_character",
            "load_campaign",
            "delete_campaign",
            "delete_save",
        }
    )


    _SYSTEM_PROMPT = CORE_GM_SYSTEM_PROMPT
    _HEARTBEAT_SYSTEM_PROMPT = HEARTBEAT_SYSTEM_PROMPT
    _POST_TOOL_SYSTEM_PROMPT = POST_TOOL_SYSTEM_PROMPT

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        registry: GMToolRegistry,
        protocol_repair_model: str = "",
        max_iterations: int = 4,
        parse_retries: int = 1,
        max_output_tokens: int = 4096,
        timeout_seconds: float | None = None,
        gm_personality_prompt: str = "",
        reply_grounding_verifier: GMReplyGroundingVerifier | None = None,
        context_budget: GMContextBudget | None = None,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.registry = registry
        self.max_iterations = max(2, int(max_iterations))
        self.parse_retries = max(0, int(parse_retries))
        self.max_output_tokens = max(512, int(max_output_tokens))
        configured_timeout = float(
            getattr(getattr(client, "config", None), "timeout_seconds", 30.0)
        )
        self.timeout_seconds = max(
            2.0,
            float(timeout_seconds) if timeout_seconds is not None else configured_timeout,
        )
        # The core agent is also the final public author.  Keeping the complete
        # persona here lets both an initial final decision and a post-tool final
        # decision produce player-ready text without a second LLM rewrite.
        self.gm_personality_prompt = str(gm_personality_prompt or "").strip()
        self.reply_grounding_verifier = reply_grounding_verifier
        self.context_governor = GMContextGovernor(context_budget)
        self._decision_requester = GMToolAgentDecisionRequester(
            client,
            model=self.model,
            repair_model=protocol_repair_model or self.model,
            protocol=self._protocol,
            parse_retries=self.parse_retries,
            # The production OpenAICompatibleClient owns the single physical
            # empty-response retry, including the JSON-mode downgrade.  Keeping
            # this outer layer at zero prevents two retry loops multiplying.
            empty_response_retries=0,
            max_output_tokens=self.max_output_tokens,
        )
        self.last_error = ""
        self.last_trace: list[dict[str, object]] = []
        self.last_loop_state: dict[str, object] = {}

    def run(
        self,
        message: str,
        *,
        recent_context: str,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        state_summary_provider: Callable[[], dict[str, object]] | None = None,
        freshness_guard: GMToolFreshnessGuard | None = None,
        commit_freshness_guard: Callable[[], bool] | None = None,
        side_effect_lock: Any | None = None,
    ) -> GMToolAgentOutcome:
        loop_state = GMAgentLoopState(timeout_seconds=self.timeout_seconds)
        message_transaction = GMMessageToolTransaction.begin(
            registry=self.registry,
            context=context,
            state_summary=state_summary,
            side_effect_lock=side_effect_lock,
        )
        try:
            outcome = self._run_agent_loop(
                message,
                recent_context=recent_context,
                context=context,
                state_summary=state_summary,
                state_summary_provider=state_summary_provider,
                freshness_guard=freshness_guard,
                side_effect_lock=side_effect_lock,
                message_transaction=message_transaction,
                loop_state=loop_state,
            )
        except Exception as exc:
            message_transaction.rollback()
            loop_state.finish(
                GMAgentTerminalReason.EXCEPTION,
                details={"exception_type": type(exc).__name__},
            )
            self.last_loop_state = loop_state.to_dict()
            raise
        frozen_message_semantics = deepcopy(
            context.metadata.get(self._MESSAGE_SEMANTICS_METADATA_KEY) or {}
        )
        loop_state.enter(GMAgentLoopPhase.FINALIZING_TRANSACTION)
        try:
            finalized = self._finalize_message_transaction(
                outcome,
                context=context,
                transaction=message_transaction,
                commit_freshness_guard=commit_freshness_guard,
            )
        except Exception as exc:
            message_transaction.rollback()
            loop_state.finish(
                GMAgentTerminalReason.EXCEPTION,
                details={
                    "exception_type": type(exc).__name__,
                    "during": GMAgentLoopPhase.FINALIZING_TRANSACTION.value,
                },
            )
            self.last_loop_state = loop_state.to_dict()
            context.metadata["_gm_agent_loop_diagnostics"] = dict(
                self.last_loop_state
            )
            raise
        loop_state.finish(GMAgentLoopState.infer_terminal_reason(finalized))
        finalized = self._redact_internal_identifiers_at_publish_boundary(
            finalized
        )
        finalized.loop_diagnostics = loop_state.to_dict()
        finalized.message_semantics = dict(
            frozen_message_semantics
        )
        self.last_loop_state = dict(finalized.loop_diagnostics)
        context.metadata["_gm_agent_loop_diagnostics"] = dict(
            finalized.loop_diagnostics
        )
        return finalized

    @staticmethod
    def _redact_internal_identifiers_at_publish_boundary(
        outcome: GMToolAgentOutcome,
    ) -> GMToolAgentOutcome:
        """Apply the final public-data guard to every agent exit path.

        Earlier protocol checks can ask the model to repair ordinary prose,
        but locked receipts and terminal read tools intentionally return before
        that branch.  The publication boundary therefore performs a final,
        deterministic redaction while leaving internal receipts untouched for
        audit and transaction recovery.
        """

        pattern = re.compile(r"(?<![\w-])proposal-[0-9A-Za-z_-]{4,64}(?![\w-])")

        def redact(text: str) -> tuple[str, bool]:
            clean = str(text or "")
            redacted, count = pattern.subn("这条提案", clean)
            return redacted, bool(count)

        changed = False
        outcome.reply, reply_changed = redact(outcome.reply)
        changed = changed or reply_changed
        clean_parts: list[str] = []
        for part in outcome.reply_parts:
            clean_part, part_changed = redact(part)
            clean_parts.append(clean_part)
            changed = changed or part_changed
        outcome.reply_parts = clean_parts
        if changed:
            outcome.trace.append(
                {
                    "decision": "public_safety_redaction",
                    "protocol_error": "INTERNAL_PROPOSAL_ID_EXPOSED",
                    "reason": "最终发布边界已移除内部提案标识。",
                }
            )
        return outcome

    def _run_agent_loop(
        self,
        message: str,
        *,
        recent_context: str,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        state_summary_provider: Callable[[], dict[str, object]] | None = None,
        freshness_guard: GMToolFreshnessGuard | None = None,
        side_effect_lock: Any | None = None,
        message_transaction: GMMessageToolTransaction,
        loop_state: GMAgentLoopState,
    ) -> GMToolAgentOutcome:
        clean_message = str(message or "").strip()
        if not clean_message or clean_message.startswith("/"):
            return GMToolAgentOutcome(handled=False)
        deadline = loop_state.deadline_monotonic
        # Tool-owned subordinate agents share the core transaction deadline.
        # Otherwise each retry could start a fresh NPC request near the end of
        # every core-agent iteration.
        context.metadata["_gm_agent_deadline_monotonic"] = deadline

        ledger = GMToolCallLedger(
            registry=self.registry,
            context=context,
            state_summary=state_summary,
            freshness_guard=freshness_guard,
            side_effect_lock=side_effect_lock,
            tool_permission_guard=lambda tool_name: self._tool_is_permitted(
                tool_name,
                context,
            ),
            message_transaction=message_transaction,
        )
        history = ledger.history
        receipts = ledger.receipts
        integrity_plans = self._message_integrity_plans(
            clean_message,
            context=context,
            state_summary=state_summary,
        )
        # A context object normally lives for one routed request, but tests and
        # administrative callers may reuse one.  Never carry a prior message's
        # incomplete ledger into the new outer transaction.
        context.metadata.pop(self._MESSAGE_INTEGRITY_METADATA_KEY, None)
        context.metadata.pop(
            self._MESSAGE_INTEGRITY_PHASE_METADATA_KEY,
            None,
        )
        context.metadata.pop(
            self._LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY,
            None,
        )
        context.metadata.pop(
            self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
            None,
        )
        context.metadata.pop(self._MESSAGE_SEMANTICS_METADATA_KEY, None)
        context.metadata.pop(
            self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY,
            None,
        )
        clear_semantic_tool_attestations(context)
        trace: list[dict[str, object]] = []
        self.last_error = ""
        self.last_trace = []
        is_system_beat = bool(context.metadata.get("system_gm_beat_request"))
        must_decide = bool(
            self._context_requires_reply(context)
            or context.gate_status in {"pre_session", "session_zero", "adventure", "paused"}
            or context.metadata.get("forced_route_mode")
            or is_system_beat
        )
        must_reply_on_failure = bool(
            not is_system_beat and self._context_requires_reply(context)
        )
        material_change_required = bool(
            context.metadata.get("heartbeat_require_material_change")
        )
        semantic_message_kinds: set[str] = set()
        initial_semantic_message_kind = ""
        has_independent_followup = False
        clarification_authorized = False
        state_delta_tracker: GMTurnStateDeltaTracker | None = None
        state_delta_receipt_cursor = 0
        # Provider recovery signals are collected only after a request has
        # returned.  They are handed to the next ordinary core-GM iteration
        # once, without changing loop limits or creating a retry obligation.
        pending_runtime_feedback_issues: list[dict[str, object]] = []

        hard_iteration_limit = (
            self.max_iterations + self._TRANSACTION_RECOVERY_GRACE_ITERATIONS
        )
        for iteration in range(1, hard_iteration_limit + 1):
            loop_state.enter(
                GMAgentLoopPhase.OBSERVING_STATE,
                iteration=iteration,
            )
            if iteration > self.max_iterations:
                pending_followup = bool(
                    self._receipt_policy.required_followup_tools(receipts)
                )
                pending_integrity = bool(
                    context.metadata.get(self._MESSAGE_INTEGRITY_METADATA_KEY)
                    or context.metadata.get(
                        self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
                    )
                )
                if (
                    not pending_followup
                    and not ledger.required_retry_pending
                    and not pending_integrity
                ):
                    break
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "bounded_transaction_recovery_grace",
                        "required_followup": pending_followup,
                        "required_retry_tool": ledger.required_retry_tool,
                        "message_integrity_pending": pending_integrity,
                    }
                )
            deadline_outcome = self._deadline_outcome(
                deadline,
                receipts,
                trace,
                must_decide,
                must_reply_on_failure
                or (ledger.mutating_call_attempted and not is_system_beat),
            )
            if deadline_outcome is not None:
                return deadline_outcome
            runtime_feedback_issues = list(pending_runtime_feedback_issues)
            pending_runtime_feedback_issues.clear()
            observed_state = self._refresh_observed_state(
                state_summary,
                state_summary_provider=state_summary_provider,
                iteration=iteration,
                trace=trace,
                runtime_feedback_issues=runtime_feedback_issues,
            )
            deadline_outcome = self._deadline_outcome(
                deadline,
                receipts,
                trace,
                must_decide,
                must_reply_on_failure
                or (ledger.mutating_call_attempted and not is_system_beat),
            )
            if deadline_outcome is not None:
                return deadline_outcome
            (
                prompt_state_summary,
                turn_state_delta,
                state_delta_tracker,
                state_delta_receipt_cursor,
            ) = self._prepare_turn_state_context(
                observed_state=observed_state,
                context=context,
                receipts=receipts,
                tracker=state_delta_tracker,
                receipt_cursor=state_delta_receipt_cursor,
                enabled=(
                    not is_system_beat
                    and self._state_context_mode(context) == "summary_delta"
                ),
            )
            loop_state.enter(
                GMAgentLoopPhase.BUILDING_CONTEXT,
                iteration=iteration,
            )
            runtime_feedback = self._runtime_feedback_payload(
                issues=runtime_feedback_issues,
                iteration=iteration,
                max_iterations=(
                    self.max_iterations
                    if iteration <= self.max_iterations
                    else hard_iteration_limit
                ),
                elapsed_ms=loop_state.elapsed_ms,
                timeout_ms=max(0, int(loop_state.timeout_seconds * 1000)),
                ledger=ledger,
                receipts=receipts,
            )
            messages = self._build_decision_messages(
                current_message=clean_message,
                recent_context=recent_context,
                context=context,
                observed_state=observed_state,
                prompt_state_summary=prompt_state_summary,
                turn_state_delta=turn_state_delta,
                receipts=receipts,
                history=history,
                required_retry_tool=ledger.required_retry_tool,
                runtime_feedback=runtime_feedback,
            )
            try:
                loop_state.enter(
                    GMAgentLoopPhase.REQUESTING_MODEL,
                    iteration=iteration,
                )
                decision = self._decision_requester.request(
                    messages,
                    iteration=iteration,
                    deadline=deadline,
                    trace=trace,
                    runtime_feedback_issues=pending_runtime_feedback_issues,
                )
            except GMToolDecisionProtocolError as exc:
                # Readable-but-invalid tool JSON is a rejected GM proposal,
                # not a provider outage.  Keep the original transaction alive
                # and return the validator's exact correction to the model.
                history.append(
                    self._protocol.decision_protocol_error(
                        exc,
                        invalid_draft=exc.invalid_draft,
                    )
                )
                trace.append(
                    {
                        "iteration": iteration,
                        "phase": "decision_protocol_returned_to_agent",
                        "protocol_error": "INVALID_AGENT_TOOL_PROTOCOL",
                        "error": str(exc)[:300],
                        "invalid_draft_preview": str(exc.invalid_draft)[:2000],
                    }
                )
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self.last_trace = trace
                must_reply_after_review = self._review_reply_obligation_after_provider_failure(
                    current_message=clean_message,
                    recent_context=recent_context,
                    context=context,
                    receipts=receipts,
                    trace=trace,
                    error=self.last_error,
                    must_reply=(
                        must_reply_on_failure
                        or (ledger.mutating_call_attempted and not is_system_beat)
                    ),
                    is_system_beat=is_system_beat,
                )
                return self._failure_policy.provider_failure(
                    receipts=receipts,
                    trace=trace,
                    error=self.last_error,
                    must_decide=must_decide,
                    must_reply=must_reply_after_review,
                )

            loop_state.enter(
                GMAgentLoopPhase.DISPATCHING_DECISION,
                iteration=iteration,
                details={
                    "decision": str(decision.get("decision") or ""),
                    "tool_name": str(decision.get("tool_name") or ""),
                },
            )
            step = self._decision_trace_step(decision, iteration=iteration)
            capability_routing = self._capability_routing_trace(
                decision,
                context=context,
            )
            if capability_routing:
                step["capability_routing"] = capability_routing
            context_manifest = context.metadata.get("_gm_context_manifest")
            if isinstance(context_manifest, dict):
                step["context_manifest"] = deepcopy(context_manifest)
            if self._freeze_message_semantics(
                decision=decision,
                context=context,
                observed_state=observed_state,
                history=history,
                step=step,
                is_system_beat=is_system_beat,
            ):
                trace.append(step)
                continue
            self._apply_semantic_capability_plan(
                context=context,
                step=step,
            )
            integrity_plans, reconciled_plan_ids = (
                self._reconcile_integrity_plans_with_message_semantics(
                    integrity_plans,
                    context=context,
                )
            )
            if reconciled_plan_ids:
                step["message_integrity_semantic_reconciliation"] = {
                    "reconciled_event_ids": list(
                        reconciled_plan_ids
                    )
                }
            message_kind = str(decision.get("message_kind") or "").strip().lower()
            if message_kind:
                semantic_message_kinds.add(message_kind)
                if not initial_semantic_message_kind:
                    initial_semantic_message_kind = message_kind
            effective_message_kind = (
                initial_semantic_message_kind or message_kind
            )
            if decision.get("has_independent_followup") is True:
                has_independent_followup = True
            trace.append(step)
            action = str(decision.get("decision") or "").strip().lower()
            if self._silence_responsibility_requires_reply(
                action=action,
                decision=decision,
                context=context,
                current_message=clean_message,
                recent_context=recent_context,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                is_system_beat=is_system_beat,
            ):
                must_reply_on_failure = True
                continue
            semantic_gm_addressed = self._decision_semantically_addresses_gm(
                decision
            )
            silence_reviewed = isinstance(
                step.get("silence_responsibility"),
                dict,
            )
            if semantic_gm_addressed and not (
                action == "silent" and silence_reviewed
            ):
                # A silent decision receives an independent semantic review
                # before its audience can create a delivery obligation.  This
                # prevents one internally contradictory core JSON object from
                # turning ordinary player discussion into a forced GM echo.
                # When the reviewer is unavailable, retain the conservative
                # core-model fallback.
                context.metadata["_semantic_gm_addressed"] = True
                must_reply_on_failure = True
                step["semantic_gm_addressed"] = True
            if self._reject_npc_turn_driven_by_player_discussion(
                action=action,
                decision=decision,
                context=context,
                receipts=receipts,
                history=history,
                step=step,
                is_system_beat=is_system_beat,
            ):
                continue
            decision_risk = self._decision_risk_tier(decision)
            risk_rank = {"observe": 0, "commit": 1, "rules": 2}
            prior_risk = str(
                context.metadata.get("_gm_transaction_risk_tier") or "observe"
            ).strip().lower()
            transaction_risk = max(
                (prior_risk, decision_risk),
                key=lambda item: risk_rank.get(item, 0),
            )
            context.metadata["_gm_transaction_risk_tier"] = transaction_risk
            step["risk_tier"] = transaction_risk
            retry_followup, followup_outcome = self._enforce_receipt_followup(
                decision=decision,
                action=action,
                context=context,
                receipts=receipts,
                history=history,
                step=step,
                trace=trace,
                clarification_authorized=clarification_authorized,
            )
            if followup_outcome is not None:
                return followup_outcome
            if retry_followup:
                continue
            retry_protocol_error = ledger.retry_protocol_error(decision)
            if retry_protocol_error is not None:
                history.append(retry_protocol_error)
                step["protocol_error"] = "SCHEMA_RETRY_TOOL_OMITTED"
                continue
            integrity_decision_issue = next(
                (
                    issue
                    for plan in integrity_plans
                    if (
                        issue
                        := self._message_integrity_validator.validate_decision(
                            plan,
                            decision,
                            receipts,
                        )
                    )
                    is not None
                ),
                None,
            )
            if integrity_decision_issue is not None:
                self._remember_message_integrity_issue(
                    integrity_decision_issue,
                    context=context,
                    history=history,
                    trace=trace,
                    step=step,
                    phase="pre_execution",
                )
                must_reply_on_failure = True
                continue
            self._clear_resolved_pre_execution_integrity_issue(
                context=context,
                receipts=receipts,
                step=step,
            )
            if action in {
                "not_applicable",
                "silent",
                "external",
                "ask_user",
                "final",
            }:
                integrity_terminal_issue = next(
                    (
                        issue
                        for plan in integrity_plans
                        if (
                            issue
                            := self._message_integrity_validator.validate_terminal(
                                plan,
                                receipts,
                                semantic_message_kind=effective_message_kind,
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if (
                    action == "ask_user"
                    and integrity_terminal_issue is not None
                    and (
                        integrity_terminal_issue.error_code
                        == "SESSION_ZERO_PROPOSAL_CONFIRMATION_AMBIGUOUS"
                        or (
                            integrity_terminal_issue.error_code
                            == "SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE"
                            and any(
                                len(requirement.proposal_ids) > 1
                                for plan in integrity_plans
                                for requirement in plan.proposal_confirmations
                            )
                        )
                    )
                ):
                    step["message_integrity_clarification"] = (
                        integrity_terminal_issue.to_dict()
                    )
                    integrity_terminal_issue = None
                    self._clear_message_integrity_issue(context)
                if integrity_terminal_issue is not None:
                    self._remember_message_integrity_issue(
                        integrity_terminal_issue,
                        context=context,
                        history=history,
                        trace=trace,
                        step=step,
                        phase="pre_terminal",
                    )
                    must_reply_on_failure = True
                    continue
                self._clear_message_integrity_issue(context)
            if action in {"call_tool", "call_tools"} and not self._tool_proposals_are_grounded(
                decision=decision,
                context=context,
                current_message=clean_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                proposal_source_review_required=any(
                    plan.proposal_persistence_required
                    for plan in integrity_plans
                ),
            ):
                clarification_authorized = bool(
                    clarification_authorized
                    or step.get("tool_proposal_requires_clarification")
                )
                continue
            if action in {"call_tool", "call_tools"} and self._reject_tools_without_message_authority(
                decision=decision,
                context=context,
                receipts=receipts,
                history=history,
                step=step,
            ):
                continue
            receipt_start = len(receipts)
            if action in {"not_applicable", "silent", "external", "ask_user", "final"}:
                outcome = self._handle_terminal_action(
                    action=action,
                    decision=decision,
                    context=context,
                    current_message=clean_message,
                    recent_context=recent_context,
                    observed_state=observed_state,
                    receipts=receipts,
                    history=history,
                    trace=trace,
                    step=step,
                    deadline=deadline,
                    must_decide=must_decide,
                    must_reply_on_failure=must_reply_on_failure,
                    material_change_required=material_change_required,
                    is_system_beat=is_system_beat,
                )
            elif action == "call_tool":
                outcome = self._handle_single_tool_action(
                    decision=decision,
                    context=context,
                    ledger=ledger,
                    receipts=receipts,
                    history=history,
                    trace=trace,
                    step=step,
                    is_system_beat=is_system_beat,
                    must_reply_on_failure=must_reply_on_failure,
                    mixed_message=(
                        "mixed" in semantic_message_kinds
                        or has_independent_followup
                    ),
                )
            elif action == "call_tools":
                outcome = self._handle_batch_tool_action(
                    decision=decision,
                    context=context,
                    current_message=clean_message,
                    recent_context=recent_context,
                    observed_state=observed_state,
                    ledger=ledger,
                    receipts=receipts,
                    history=history,
                    trace=trace,
                    step=step,
                    deadline=deadline,
                    must_reply_on_failure=must_reply_on_failure,
                    material_change_required=material_change_required,
                    is_system_beat=is_system_beat,
                    mixed_message=(
                        "mixed" in semantic_message_kinds
                        or has_independent_followup
                    ),
                )
            else:
                history.append(self._protocol.invalid_decision_error())
                outcome = None
            if action in {"call_tool", "call_tools"}:
                self._annotate_semantically_complete_proposals(
                    receipts[receipt_start:],
                    step=step,
                )
                new_successful_mutation = any(
                    receipt.ok and receipt.state_changed
                    for receipt in receipts[receipt_start:]
                )
                if new_successful_mutation or outcome is not None:
                    integrity_terminal_issue = next(
                        (
                            issue
                            for plan in integrity_plans
                            if (
                                issue
                                := self._message_integrity_validator.validate_terminal(
                                    plan,
                                    receipts,
                                    semantic_message_kind=effective_message_kind,
                                )
                            )
                            is not None
                        ),
                        None,
                    )
                    if integrity_terminal_issue is not None:
                        self._remember_message_integrity_issue(
                            integrity_terminal_issue,
                            context=context,
                            history=history,
                            trace=trace,
                            step=step,
                            phase="post_execution",
                        )
                        must_reply_on_failure = True
                        # A locked/terminal receipt is still tentative until
                        # all obligations from the same player sentence have
                        # successful receipts.  Stay inside the outer message
                        # transaction and let the next iteration repair it.
                        outcome = None
                    else:
                        self._clear_message_integrity_issue(context)
                        if (
                            outcome is None
                            and new_successful_mutation
                            and not ledger.required_retry_pending
                            and not self._receipt_policy.required_followup_tools(receipts)
                            and not self._context_requires_reply(context)
                            and not has_independent_followup
                            and "mixed" not in semantic_message_kinds
                            and self._mutations_can_commit_silently(
                                receipts,
                                context=context,
                            )
                        ):
                            step["auto_terminal_silent_commit"] = True
                            self.last_trace = trace
                            outcome = GMToolAgentOutcome(
                                handled=True,
                                reply="",
                                receipts=receipts,
                                trace=trace,
                                target="silent",
                                mode="gm_agent_silent_commit",
                                stop_astrbot=True,
                                reason=(
                                    "当前玩家消息中的状态事项已经完整提交，"
                                    "原话本身已经公开，无需继续规划或复述。"
                                ),
                                terminal_action="silent",
                            )
            if (
                outcome is None
                and action in {"call_tool", "call_tools"}
            ):
                blocking_receipt = next(
                    (
                        receipt
                        for receipt in reversed(receipts[receipt_start:])
                        if (
                            not receipt.ok
                            and receipt.error_code
                            == "BLOCKING_DECISION_PENDING"
                        )
                    ),
                    None,
                )
                if (
                    blocking_receipt is not None
                    and self._decision_commits_new_action(
                        decision=decision,
                        context=context,
                    )
                ):
                    step["pending_decision_new_action_deferred"] = True
                    outcome = self._pending_decision_prompt_outcome(
                        observed_state=observed_state,
                        blocking_receipt=blocking_receipt,
                        receipts=list(receipts),
                        trace=trace,
                    )
                rejection = next(
                    (
                        receipt
                        for receipt in reversed(receipts[receipt_start:])
                        if not receipt.ok and not receipt.retryable
                    ),
                    None,
                )
                if rejection is not None:
                    if not any(
                        receipt.ok and receipt.state_changed
                        for receipt in receipts
                    ):
                        # A definitive permission or precondition rejection is
                        # itself the correct completion of this request.  Do not
                        # let the generic completeness ledger replace the useful
                        # public reason merely because the requested mutation was
                        # (correctly) refused.
                        self._clear_message_integrity_issue(context)
                        context.metadata.pop(
                            self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                            None,
                        )
                        step["authoritative_nonretryable_rejection_terminal"] = (
                            rejection.error_code
                        )
                    outcome = self._nonretryable_tool_rejection_outcome(
                        rejection,
                        receipts=list(receipts),
                        trace=trace,
                    )
                    step["nonretryable_tool_rejection_terminal"] = True
            if outcome is not None:
                pending_post_tool = context.metadata.get(
                    self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
                )
                if (
                    isinstance(pending_post_tool, dict)
                    and pending_post_tool.get("requires_followup_tool") is True
                    and outcome.target == "fu_gm"
                    and str(outcome.reply or "").strip()
                    and not any(
                        receipt.ok
                        and receipt.state_changed
                        and receipt.tool_name
                        == str(
                            pending_post_tool.get("required_followup_tool")
                            or "propose_session_zero_update"
                        )
                        for receipt in receipts[
                            int(
                                pending_post_tool.get(
                                    "receipt_cursor_at_detection"
                                )
                                or 0
                            ) :
                        ]
                    )
                ):
                    history.append(
                        {
                            "protocol_error": {
                                **pending_post_tool,
                                "message": (
                                    "玩家委托GM创作的公开设定尚未通过待确认提案工具保存。"
                                ),
                                "correction_hint": (
                                    "先调用propose_session_zero_update保存具体提案；"
                                    "成功后再把提案自然告诉玩家。"
                                ),
                                "retryable": True,
                            }
                        }
                    )
                    step["post_tool_followup_tool_missing"] = True
                    must_reply_on_failure = True
                    continue
                if self._no_tool_reply_requires_more_work(
                    action=action,
                    decision=decision,
                    outcome=outcome,
                    context=context,
                    current_message=clean_message,
                    recent_context=recent_context,
                    receipts=receipts,
                    history=history,
                    trace=trace,
                    step=step,
                    deadline=deadline,
                    is_system_beat=is_system_beat,
                ):
                    must_reply_on_failure = True
                    continue
                if self._post_tool_outcome_requires_more_work(
                    action=action,
                    decision=decision,
                    outcome=outcome,
                    context=context,
                    current_message=clean_message,
                    recent_context=recent_context,
                    receipts=receipts,
                    history=history,
                    trace=trace,
                    step=step,
                    deadline=deadline,
                    is_system_beat=is_system_beat,
                ):
                    must_reply_on_failure = True
                    continue
                if outcome.target == "fu_gm" and str(outcome.reply or "").strip():
                    context.metadata.pop(
                        self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                        None,
                    )
                self._attach_delivery_intent(
                    outcome,
                    decision=decision,
                    context=context,
                    trace_step=step,
                )
                return outcome
            if ledger.mutating_call_attempted and not is_system_beat:
                must_reply_on_failure = True

        self.last_trace = trace
        exhausted = self._failure_policy.exhausted(
            receipts=receipts,
            trace=trace,
            must_decide=must_decide,
            must_reply=(
                must_reply_on_failure
                or (ledger.mutating_call_attempted and not is_system_beat)
            ),
        )
        return self._apply_last_semantic_protocol_error(
            exhausted,
            context=context,
        )

    @staticmethod
    def _decision_commits_new_action(
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
    ) -> bool:
        """Use frozen message semantics to distinguish action from an answer."""

        raw = context.metadata.get(
            LLMGMToolAgent._MESSAGE_SEMANTICS_METADATA_KEY
        )
        source_events = [
            dict(item)
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        try:
            semantics = GMMessageSemantics.parse(
                raw,
                source_events=source_events,
            )
        except GMMessageSemanticsError:
            return False
        action = str(decision.get("decision") or "").strip().lower()
        calls = (
            [
                {
                    "tool_name": decision.get("tool_name"),
                    "arguments": decision.get("arguments"),
                }
            ]
            if action == "call_tool"
            else [
                dict(item)
                for item in list(decision.get("calls") or [])
                if isinstance(item, dict)
            ]
        )
        for call in calls:
            source = semantics.source_event(call.get("arguments"))
            if source is not None and source.action_commitment == "committed":
                return True
        return False

    @staticmethod
    def _pending_decision_prompt_outcome(
        *,
        observed_state: dict[str, object],
        blocking_receipt: GMToolReceipt,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
    ) -> GMToolAgentOutcome:
        """Return the existing player prompt instead of retrying an illegal skip."""

        gameplay = dict(observed_state.get("gameplay") or {})
        pending = [
            dict(item)
            for item in list(gameplay.get("pending_decisions") or [])
            if isinstance(item, dict)
        ]
        wanted_ids = {
            str(item.get("window_id") or "").strip()
            for item in list(blocking_receipt.result.get("pending_windows") or [])
            if isinstance(item, dict) and str(item.get("window_id") or "").strip()
        }
        window = next(
            (
                item
                for item in pending
                if not wanted_ids
                or str(item.get("window_id") or "").strip() in wanted_ids
            ),
            {},
        )
        prompt = str(window.get("prompt") or "").strip()
        if not prompt:
            prompt = "先处理刚才还没决定的选择，再继续这个动作。"
        return GMToolAgentOutcome(
            handled=True,
            reply=prompt,
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_ask_user",
            reason=(
                "玩家提交了新的场景行动，但其先前规则选择仍在等待本人处理；"
                "保留新动作未结算，并重新交还原选择。"
            ),
            terminal_action="ask_user",
        )

    @staticmethod
    def _nonretryable_tool_rejection_outcome(
        receipt: GMToolReceipt,
        *,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
    ) -> GMToolAgentOutcome:
        """一次性结束不可重试的权威拒绝，避免代理在同一错误上空转。"""

        reply = str(receipt.public_fallback_reply or "").strip()
        if not reply:
            if receipt.error_code in {
                "TOOL_EXECUTION_FAILED",
                "TOOL_ROLLBACK_FAILED",
                "TOOL_TRANSACTION_START_FAILED",
                "TOOL_ADMISSION_CHECK_FAILED",
                "INVALID_TOOL_RECEIPT",
            }:
                reply = "刚才处理这件事时出了点问题，当前进度没有改动。"
            else:
                reason = str(receipt.message or "当前条件不允许这样处理").strip()
                reply = f"这次还不能这样处理：{reason.rstrip('。！？!?；;')}。"
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_rule_rejected",
            reason=(
                f"工具 {receipt.tool_name} 返回不可重试的权威拒绝；"
                "本轮相关事项已标记为阻断并立即结束。"
            ),
        )

    @staticmethod
    def _current_source_event_id(context: GMToolExecutionContext) -> str:
        events = [
            item
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        if events:
            return str(events[-1].get("event_id") or "").strip()
        return str(context.metadata.get("source_event_id") or "").strip()

    def _message_integrity_plans(
        self,
        message: str,
        *,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
    ) -> tuple[GMMessageIntegrityPlan, ...]:
        """Freeze one completeness plan per authoritative source event.

        A bridge debounce turn may contain several players.  Tool execution is
        already source-event bound; completeness must use the same boundary so
        a receipt for the last message cannot hide an omitted contribution
        from an earlier player in the batch.
        """

        if bool(context.metadata.get("system_gm_beat_request")):
            # Heartbeat payloads describe what the GM may ask or advance. They
            # are not player-authored declarations, so parsing words embedded
            # in prompt_hint as mandatory Session 0 writes creates an
            # impossible loop: a read-only nudge is ordered to commit the very
            # topic it is inviting players to discuss.
            return (
                GMMessageIntegrityPlan(
                    source_event_id=self._current_source_event_id(context),
                    speaker=context.speaker,
                ),
            )

        events = [
            item
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        if not events:
            return (
                self._message_integrity_validator.plan(
                    message,
                    gate_status=context.gate_status,
                    source_event_id=self._current_source_event_id(context),
                    strict_source_event=False,
                    speaker=context.speaker,
                    state_summary=state_summary,
                ),
            )
        plans: list[GMMessageIntegrityPlan] = []
        strict_source_event = len(events) > 1
        for event_index, event in enumerate(events):
            event_text = str(event.get("text") or "").strip()
            if not event_text:
                continue
            plans.append(
                self._message_integrity_validator.plan(
                    event_text,
                    gate_status=context.gate_status,
                    source_event_id=str(event.get("event_id") or "").strip(),
                    strict_source_event=strict_source_event,
                    prior_source_event_ids=tuple(
                        str(item.get("event_id") or "").strip()
                        for item in events[:event_index]
                        if str(item.get("event_id") or "").strip()
                    ),
                    speaker=str(event.get("speaker") or context.speaker).strip(),
                    state_summary=state_summary,
                )
            )
        if plans:
            return tuple(plans)
        return (
            self._message_integrity_validator.plan(
                message,
                gate_status=context.gate_status,
                source_event_id=self._current_source_event_id(context),
                strict_source_event=False,
                speaker=context.speaker,
                state_summary=state_summary,
            ),
        )

    def _canonical_proposal_subject(self, subject: str) -> str:
        coverage = (
            self._message_integrity_validator
            .PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE
        )
        if subject in coverage:
            return subject
        if subject in WorldSettingCatalog.CATEGORIES:
            return subject
        matches = [
            candidate
            for candidate, categories in coverage.items()
            if subject in categories
        ]
        return matches[0] if len(matches) == 1 else ""

    def _canonical_world_requirement(self, subject: str) -> str:
        coverage = self._message_integrity_validator.WORLD_CATEGORY_COVERAGE
        if subject in coverage:
            return subject
        matches = [
            candidate
            for candidate, categories in coverage.items()
            if subject in categories
        ]
        return matches[0] if len(matches) == 1 else ""

    def _reconcile_integrity_plans_with_message_semantics(
        self,
        plans: tuple[GMMessageIntegrityPlan, ...],
        *,
        context: GMToolExecutionContext,
    ) -> tuple[tuple[GMMessageIntegrityPlan, ...], tuple[str, ...]]:
        """Let the frozen LLM interpretation override lexical false positives.

        The integrity parser intentionally over-detects potentially omitted
        writes, but words such as "赞成" can approve one clause while another
        clause proposes something new.  Once the core model has classified
        each transport event, Python validates those frozen semantic units
        instead of independently reinterpreting the prose.
        """

        semantics = self._parsed_frozen_message_semantics(context)
        if semantics is None:
            return plans, ()

        reconciled: list[GMMessageIntegrityPlan] = []
        changed_ids: list[str] = []
        for plan in plans:
            event = semantics.event(plan.source_event_id)
            if event is None and len(semantics.events) == 1:
                event = semantics.events[0]
            if event is None:
                reconciled.append(plan)
                continue

            state_intents = tuple(event.state_intents)
            safety_write_intent = any(
                intent.scope == "safety"
                and intent.subject == "safety_boundary"
                and intent.operation in {"contribute", "correct"}
                for intent in state_intents
            )
            semantic_safety_declarations = (
                plan.safety_declarations if safety_write_intent else ()
            )
            skipped_session_zero_topics = tuple(
                dict.fromkeys(
                    "safety"
                    for intent in state_intents
                    if intent.scope == "safety"
                    and intent.subject == "safety_boundary"
                    and intent.operation == "skip"
                )
            )
            deferred_session_zero_topics = tuple(
                dict.fromkeys(
                    "safety"
                    for intent in state_intents
                    if intent.scope == "safety"
                    and intent.subject == "safety_boundary"
                    and intent.operation == "defer"
                )
            )
            if not state_intents:
                # The semantic pass has explicitly found no persistent world
                # or hero meaning. Keep only structural obligations that are
                # not yet represented by the semantic contract. Lexical
                # world/proposal/hero guesses must not recreate state behind
                # the model's frozen player-to-player discussion decision.
                reconciled_plan = replace(
                    plan,
                    world_categories=(),
                    proposal=False,
                    proposal_subjects=(),
                    proposal_confirmations=(),
                    proposed_world_categories=(),
                    skipped_world_categories=(),
                    deferred_world_categories=(),
                    safety_declarations=semantic_safety_declarations,
                    skipped_session_zero_topics=(),
                    deferred_session_zero_topics=(),
                    hero_attributes_explicit=False,
                    hero_fields=(),
                    hero_skill_options=(),
                )
                reconciled.append(reconciled_plan)
                if reconciled_plan != plan:
                    changed_ids.append(event.event_id)
                continue

            proposal_subjects = tuple(
                dict.fromkeys(
                    normalized
                    for intent in state_intents
                    if intent.operation == "propose"
                    and (
                        normalized := self._canonical_proposal_subject(
                            intent.subject
                        )
                    )
                )
            )
            proposal_subject_set = set(proposal_subjects)
            semantic_confirmations: list[
                GMProposalConfirmationRequirement
            ] = []
            for intent in state_intents:
                if intent.operation != "confirm":
                    continue
                normalized = self._canonical_proposal_subject(intent.subject)
                if not normalized:
                    continue
                lexical_matches = tuple(
                    requirement
                    for requirement in plan.proposal_confirmations
                    if requirement.subject == normalized
                )
                if intent.proposal_id:
                    requirement = GMProposalConfirmationRequirement(
                        subject=normalized,
                        proposal_ids=(intent.proposal_id,),
                        replacement_required=(
                            normalized not in proposal_subject_set
                            and any(
                                item.replacement_required
                                for item in lexical_matches
                            )
                        ),
                        ambiguous=False,
                        clause=(
                            intent.summary
                            or intent.target
                            or (
                                lexical_matches[0].clause
                                if lexical_matches
                                else ""
                            )
                        ),
                    )
                    if requirement not in semantic_confirmations:
                        semantic_confirmations.append(requirement)
                    continue
                if lexical_matches:
                    for requirement in lexical_matches:
                        if (
                            normalized in proposal_subject_set
                            and requirement.replacement_required
                        ):
                            requirement = replace(
                                requirement,
                                replacement_required=False,
                            )
                        if requirement not in semantic_confirmations:
                            semantic_confirmations.append(requirement)
                    continue
                semantic_confirmations.append(
                    GMProposalConfirmationRequirement(
                        subject=normalized,
                        clause=intent.summary or intent.target,
                    )
                )
            semantic_world_categories = tuple(
                dict.fromkeys(
                    requirement
                    for intent in state_intents
                    if intent.scope in {"group", "world"}
                    and intent.operation in {"contribute", "correct"}
                    and (
                        requirement := self._canonical_world_requirement(
                            intent.subject
                        )
                    )
                )
            )
            proposed_world_categories = tuple(
                dict.fromkeys(
                    intent.subject
                    for intent in state_intents
                    if intent.scope in {"group", "world"}
                    and intent.operation == "propose"
                    and intent.subject
                    in self._message_integrity_validator.WORLD_CATEGORY_COVERAGE
                )
            )
            skipped_world_categories = tuple(
                dict.fromkeys(
                    intent.subject
                    for intent in state_intents
                    if intent.scope == "world"
                    and intent.operation == "skip"
                    and intent.subject
                    in self._message_integrity_validator.WORLD_SKIP_TOPIC_CODES
                )
            )
            deferred_world_categories = tuple(
                dict.fromkeys(
                    intent.subject
                    for intent in state_intents
                    if intent.scope == "world"
                    and intent.operation == "defer"
                    and intent.subject
                    in self._message_integrity_validator.WORLD_SKIP_TOPIC_CODES
                )
            )
            semantic_hero_fields = tuple(
                dict.fromkeys(
                    field_name
                    for intent in state_intents
                    if intent.scope == "hero"
                    and intent.operation in {"contribute", "correct"}
                    and (
                        field_name := (
                            HERO_STATE_INTENT_SUBJECT_TO_PATCH_FIELD.get(
                                intent.subject
                            )
                        )
                    )
                )
            )
            semantic_hero_field_set = set(semantic_hero_fields)
            reconciled_plan = replace(
                plan,
                world_categories=semantic_world_categories,
                proposal=bool(proposal_subjects),
                proposal_subjects=proposal_subjects,
                proposal_confirmations=tuple(semantic_confirmations),
                proposed_world_categories=proposed_world_categories,
                skipped_world_categories=skipped_world_categories,
                deferred_world_categories=deferred_world_categories,
                safety_declarations=semantic_safety_declarations,
                skipped_session_zero_topics=skipped_session_zero_topics,
                deferred_session_zero_topics=deferred_session_zero_topics,
                hero_attributes_explicit=(
                    "attributes" in semantic_hero_field_set
                ),
                hero_fields=semantic_hero_fields,
                hero_skill_options=(
                    plan.hero_skill_options
                    if "skill_options" in semantic_hero_field_set
                    else ()
                ),
            )
            reconciled.append(reconciled_plan)
            if reconciled_plan != plan:
                changed_ids.append(event.event_id)
        return tuple(reconciled), tuple(changed_ids)

    def _remember_message_integrity_issue(
        self,
        issue: GMMessageIntegrityIssue,
        *,
        context: GMToolExecutionContext,
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        phase: str,
    ) -> None:
        payload = issue.to_dict()
        context.metadata[self._MESSAGE_INTEGRITY_METADATA_KEY] = deepcopy(payload)
        context.metadata[self._MESSAGE_INTEGRITY_PHASE_METADATA_KEY] = str(
            phase or "validation"
        )
        history.append(issue.protocol_error())
        step["protocol_error"] = issue.error_code
        step["message_integrity"] = {
            "phase": str(phase or "validation"),
            **deepcopy(payload),
        }
        self.last_trace = trace

    def _clear_message_integrity_issue(
        self,
        context: GMToolExecutionContext,
    ) -> None:
        context.metadata.pop(self._MESSAGE_INTEGRITY_METADATA_KEY, None)
        context.metadata.pop(
            self._MESSAGE_INTEGRITY_PHASE_METADATA_KEY,
            None,
        )

    def _pending_message_integrity_repair_tools(
        self,
        context: GMToolExecutionContext,
    ) -> set[str]:
        """Return only Python-signed repair tools for the current message.

        These names come from ``GMMessageIntegrityIssue`` rather than model
        prose. They exist only while that exact issue remains pending, so they
        can safely interleave with a receipt-mandated continuation without
        becoming a general capability grant.
        """

        issue = context.metadata.get(self._MESSAGE_INTEGRITY_METADATA_KEY)
        if not isinstance(issue, dict):
            return set()
        return {
            name
            for item in list(issue.get("required_repair_tools") or [])
            if (name := str(item or "").strip()) and name in self.registry._tools
        }

    def _clear_resolved_pre_execution_integrity_issue(
        self,
        *,
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        step: dict[str, object],
    ) -> None:
        """Drop a corrected pre-write issue before later reviews run.

        This is deliberately limited to transactions with no successful
        mutation yet.  Once any write has succeeded, the terminal
        completeness check remains the authority for whether partial state may
        commit.
        """

        if str(
            context.metadata.get(
                self._MESSAGE_INTEGRITY_PHASE_METADATA_KEY
            )
            or ""
        ) != "pre_execution":
            return
        if any(receipt.ok and receipt.state_changed for receipt in receipts):
            return
        self._clear_message_integrity_issue(context)
        step["message_integrity_pre_execution_resolved"] = True

    def _apply_last_semantic_protocol_error(
        self,
        outcome: GMToolAgentOutcome,
        *,
        context: GMToolExecutionContext,
    ) -> GMToolAgentOutcome:
        payload = context.metadata.get(
            self._LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY
        )
        if not isinstance(payload, dict) or not payload:
            return outcome
        code = str(
            payload.get("error_code")
            or "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
        ).strip()
        message = str(
            payload.get("message")
            or "拟议写入没有通过事实一致性审校。"
        ).strip()
        claims = [
            str(item or "").strip()
            for item in list(payload.get("unsupported_claims") or [])
            if str(item or "").strip()
        ]
        detail = "；".join(claims[:3])
        if outcome.reply:
            outcome.reply = (
                "本轮最后仍未通过事实一致性审校"
                + (f"：{detail}" if detail else f"：{message}")
                + "。因此这条消息没有执行或结算，权威状态未改变。"
            )
        outcome.error = self._join_errors(
            outcome.error,
            f"{code}: {message}",
        )
        outcome.reason = (
            "工具循环耗尽时保留最后一个真实语义阻断原因；"
            "没有用更早且已修正的完整性错误覆盖它。"
        )
        return outcome

    @staticmethod
    def _semantic_tool_rejection_count(
        history: list[dict[str, object]],
        *,
        tool_name: str,
    ) -> int:
        """Count prior semantic rejections for one tool in this transaction."""

        count = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            error = item.get("protocol_error")
            if not isinstance(error, dict):
                continue
            if (
                str(error.get("error_code") or "").strip()
                != "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
            ):
                continue
            if str(error.get("tool_name") or "").strip() == tool_name:
                count += 1
        return count

    @staticmethod
    def _npc_response_repair_contract(
        *,
        rejection_count: int,
        repair_mode: str,
        unsupported_claims: list[str],
    ) -> dict[str, object]:
        """Give the core GM bounded semantic exits from a rejected NPC claim."""

        repeated = rejection_count >= 2
        return {
            "tool_name": "decide_npc_response",
            "mode": (
                "npc_fact_or_nonclaim"
                if repair_mode == "npc_fact_or_nonclaim"
                else "repair_npc_response"
            ),
            "rejection_count": rejection_count,
            "rejected_claims": list(unsupported_claims[:6]),
            "valid_fact_path": {
                "field": "fact_effects",
                "allowed_kinds": ["objective", "claim", "rumor", "lie"],
                "require_information_source_for_objective": True,
            },
            "valid_nonclaim_path": {
                "allowed_speech_acts": [
                    "admit_unknown",
                    "refuse",
                    "deflect",
                    "new_gate",
                ],
                "fact_effects_must_be_empty": True,
            },
            "must_preserve_npc_and_player_message": True,
            "must_not_rephrase_rejected_claim": True,
            "repeated_rejection_requires_safe_exit": repeated,
        }

    @staticmethod
    def _message_integrity_public_reply(issue: dict[str, object]) -> str:
        del issue
        return "刚才这件事没有处理完整，存档没有改动。麻烦再说一次。"

    def _finalize_message_transaction(
        self,
        outcome: GMToolAgentOutcome,
        *,
        context: GMToolExecutionContext,
        transaction: GMMessageToolTransaction,
        commit_freshness_guard: Callable[[], bool] | None = None,
    ) -> GMToolAgentOutcome:
        pending_integrity = context.metadata.get(
            self._MESSAGE_INTEGRITY_METADATA_KEY
        )
        if not pending_integrity:
            pending_integrity = context.metadata.get(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
            )
        if isinstance(pending_integrity, dict) and pending_integrity:
            issue = deepcopy(pending_integrity)
            rollback_error = transaction.rollback()
            rolled_back_tools = transaction.mark_receipts_rolled_back(
                outcome.receipts
            )
            transaction.mark_trace_rolled_back(outcome.trace)
            outcome.trace.append(
                {
                    "message_integrity_rollback": {
                        **issue,
                        "rolled_back_tools": rolled_back_tools,
                        "rollback_error": rollback_error,
                    }
                }
            )
            error_code = str(
                issue.get("error_code") or "MESSAGE_INTENT_INCOMPLETE"
            ).strip()
            issue_message = str(issue.get("message") or "本条消息没有完整处理。").strip()
            outcome.handled = True
            outcome.reply = self._message_integrity_public_reply(issue)
            outcome.reply_parts = []
            outcome.target = "fu_gm"
            outcome.mode = "gm_agent_message_transaction_rolled_back"
            outcome.stop_astrbot = True
            outcome.error = self._join_errors(
                outcome.error,
                f"{error_code}: {issue_message}",
                (
                    "；消息事务回滚失败：" + rollback_error
                    if rollback_error
                    else ""
                ),
            )
            outcome.reason = (
                "消息级完整性校验仍有未覆盖事项，拒绝提交并回滚："
                + error_code
            )
            return outcome

        successful_mutations = [
            receipt
            for receipt in outcome.receipts
            if receipt.ok and receipt.state_changed
        ]
        if not successful_mutations:
            rollback_error = transaction.rollback()
            if rollback_error:
                outcome.error = self._join_errors(
                    outcome.error,
                    "消息事务清理失败：" + rollback_error,
                )
            return outcome

        has_reply_media = any(
            isinstance(receipt.result.get("reply_media"), list)
            and bool(receipt.result.get("reply_media"))
            for receipt in successful_mutations
        )
        publicly_deliverable = bool(
            outcome.target == "fu_gm"
            and (
                str(outcome.reply or "").strip()
                or any(str(item or "").strip() for item in outcome.reply_parts)
                or has_reply_media
            )
        )
        silently_deliverable = bool(
            not publicly_deliverable
            and self._mutations_can_commit_silently(
                outcome.receipts,
                context=context,
            )
        )
        recovered = self._receipt_policy.state_change_recovered(outcome.receipts)
        if not recovered and outcome.mode == "gm_agent_rule_rejected":
            recovered = (
                self._receipt_policy.state_change_recovered_with_player_input_blocker(
                    outcome.receipts
                )
            )
        if recovered and (publicly_deliverable or silently_deliverable):
            commit_error = transaction.commit(
                freshness_guard=commit_freshness_guard,
            )
            if transaction.stale_before_commit:
                rollback_error = transaction.rollback()
                if not rollback_error:
                    rolled_back_tools = transaction.mark_receipts_rolled_back(
                        outcome.receipts
                    )
                    transaction.mark_trace_rolled_back(outcome.trace)
                    outcome.trace.append(
                        {
                            "transaction_freshness": {
                                "error_code": "STALE_AGENT_REQUEST",
                                "stale_discarded": True,
                                "rolled_back_tools": rolled_back_tools,
                                "rollback_error": "",
                            }
                        }
                    )
                    outcome.handled = True
                    outcome.reply = ""
                    outcome.reply_parts = []
                    outcome.target = "silent"
                    outcome.mode = "gm_agent_stale"
                    outcome.stop_astrbot = True
                    outcome.reason = (
                        "生成期间出现了新的群聊消息，"
                        "本轮尚未提交的改动已撤销。"
                    )
                    outcome.delivery = DeliveryIntent()
                    return outcome
                outcome.trace.append(
                    {
                        "transaction_freshness": {
                            "stale_after_commit": True,
                            "rollback_error": rollback_error,
                        }
                    }
                )
                outcome.error = self._join_errors(
                    outcome.error,
                    "过期事务回滚失败：" + rollback_error,
                )
                return outcome
            if not commit_error:
                if silently_deliverable:
                    outcome.reply = ""
                    outcome.reply_parts = []
                    outcome.target = "silent"
                    outcome.mode = "gm_agent_silent_commit"
                    outcome.stop_astrbot = True
                    outcome.reason = (
                        "玩家消息已经公开表达了完整的本地动作；"
                        "状态已登记，无需由GM复述。"
                    )
                return outcome
            rollback_error = transaction.rollback()
            outcome.error = self._join_errors(
                outcome.error,
                "消息事务提交失败：" + commit_error,
                (
                    "；回滚也失败：" + rollback_error
                    if rollback_error
                    else ""
                ),
            )
        else:
            rollback_error = transaction.rollback()
            if rollback_error:
                outcome.error = self._join_errors(
                    outcome.error,
                    "消息事务回滚失败：" + rollback_error,
                )

        rolled_back_tools = transaction.mark_receipts_rolled_back(outcome.receipts)
        transaction.mark_trace_rolled_back(outcome.trace)
        outcome.trace.append(
            {
                "message_transaction_rollback": {
                    "rolled_back_tools": rolled_back_tools,
                    "complete": recovered,
                    "publicly_deliverable": publicly_deliverable,
                    "silently_deliverable": silently_deliverable,
                    "rollback_error": rollback_error,
                }
            }
        )
        is_system_beat = bool(context.metadata.get("system_gm_beat_request"))
        semantic_gm_addressed = any(
            bool(item.get("semantic_gm_addressed"))
            for item in outcome.trace
            if isinstance(item, dict)
        )
        must_reply = bool(
            not is_system_beat
            and (
                self._context_requires_reply(context)
                or semantic_gm_addressed
            )
        )
        outcome.reply = (
            "工具事务没有完整完成，已全部回滚，没有留下改动；"
            "这条消息没有记入或结算，请重试。"
            if must_reply
            else ""
        )
        outcome.reply_parts = []
        outcome.target = "fu_gm" if must_reply else "silent"
        outcome.mode = "gm_agent_message_transaction_rolled_back"
        outcome.stop_astrbot = True
        outcome.reason = (
            "同一条消息的工具事务未完整形成可交付结果，已回滚："
            + "、".join(rolled_back_tools)
        )
        return outcome

    @staticmethod
    def _mutations_can_commit_silently(
        receipts: list[GMToolReceipt],
        *,
        context: GMToolExecutionContext,
    ) -> bool:
        """Honor an explicit receipt capability, never infer silent writes."""

        mutations = [
            receipt
            for receipt in receipts
            if receipt.ok and receipt.state_changed
        ]
        # Some writes only prepare the transaction so its actual gameplay
        # operation can run, such as restoring an already-existing split-party
        # camera before recording that actor's action.  Those receipts must not
        # veto an explicit silence capability from the final operation, but a
        # preparatory write can never authorize silence by itself.
        governing_mutations = [
            receipt
            for receipt in mutations
            if not (
                receipt.result.get("silent_commit_neutral") is True
                and not receipt.lock_public_reply
                and not receipt.public_fallback_reply
            )
        ]
        if not governing_mutations or not all(
            receipt.result.get("silent_commit_allowed") is True
            for receipt in governing_mutations
        ):
            return False
        if not LLMGMToolAgent._context_requires_reply(context):
            return True
        # A small set of deterministic gameplay tools can prove that the
        # player's own public sentence already *is* the complete visible
        # result.  This stronger capability lets /game/turn persist a local
        # movement or pass without forcing the GM to paraphrase it merely as
        # acknowledgement.  Generic addressed writes still require a reply.
        return all(
            receipt.result.get("source_message_already_public") is True
            for receipt in governing_mutations
        )

    @staticmethod
    def _context_requires_reply(context: GMToolExecutionContext) -> bool:
        """Return whether transport or semantic evidence requires a reply."""

        return bool(
            context.directly_addressed
            or context.is_private
            or context.metadata.get("force_gm_reply")
            or context.metadata.get("_semantic_gm_addressed")
        )

    @staticmethod
    def _decision_semantically_addresses_gm(decision: dict[str, object]) -> bool:
        if str(decision.get("audience") or "").strip().lower() != "gm":
            return False
        # ``audience=gm`` also means that a game action needs GM tooling.  It
        # does not, by itself, prove that the player called or questioned the
        # GM.  Only request-like clauses create the stronger delivery duty.
        return str(decision.get("message_kind") or "").strip().lower() in {
            "gm_request",
            "mixed",
        }

    @staticmethod
    def _attach_delivery_intent(
        outcome: GMToolAgentOutcome,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        trace_step: dict[str, object],
    ) -> None:
        """Carry the model's presentation choice without changing semantics.

        The platform boundary validates ids later.  Missing delivery data is
        intentionally ordinary unquoted speech, even for a directly addressed
        group message.
        """

        if outcome.target != "fu_gm" or not outcome.reply:
            outcome.delivery = DeliveryIntent()
            return
        proposed = DeliveryIntent.from_dict(
            decision.get("delivery")
            if isinstance(decision.get("delivery"), dict)
            else None
        )
        outcome.delivery = proposed
        trace_step["delivery"] = proposed.to_dict()

    @staticmethod
    def _join_errors(*parts: str) -> str:
        return "；".join(str(part or "").strip("；") for part in parts if str(part or "").strip())

    @staticmethod
    def _refresh_observed_state(
        state_summary: dict[str, object],
        *,
        state_summary_provider: Callable[[], dict[str, object]] | None,
        iteration: int,
        trace: list[dict[str, object]],
        runtime_feedback_issues: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        observed_state = dict(state_summary or {})
        if not callable(state_summary_provider):
            return observed_state
        try:
            observed_state = dict(state_summary_provider() or {})
            state_summary.clear()
            state_summary.update(deepcopy(observed_state))
        except Exception as exc:
            trace.append(
                {
                    "iteration": iteration,
                    "phase": "state_refresh_fallback",
                    "error_type": type(exc).__name__,
                }
            )
            if runtime_feedback_issues is not None:
                runtime_feedback_issues.append(
                    {
                        "code": "STATE_REFRESH_FAILED",
                        "phase": "refreshing_state",
                        "severity": "warning",
                        "retryable": False,
                        "correction_hint": (
                            "本次权威状态刷新未完成；当前请求保留并使用了"
                            "最近一次可用的权威状态视图。"
                        ),
                        "recovery_action": (
                            "use_retained_authoritative_context"
                        ),
                    }
                )
        return observed_state

    @staticmethod
    def _runtime_feedback_payload(
        *,
        issues: list[dict[str, object]],
        iteration: int,
        max_iterations: int,
        elapsed_ms: int,
        timeout_ms: int,
        ledger: GMToolCallLedger,
        receipts: list[GMToolReceipt],
    ) -> dict[str, object] | None:
        """Build one bounded, model-visible runtime condition envelope.

        Only fixed enum mappings cross this boundary.  Arbitrary provider
        exceptions, endpoints and monitor events never become model input.
        The envelope is descriptive: loop continuation is still controlled by
        receipts, required follow-ups and the existing Python loop.
        """

        budget = GMRuntimeBudget.from_limits(
            iteration=iteration,
            max_iterations=max_iterations,
            elapsed_ms=elapsed_ms,
            timeout_ms=timeout_ms,
        )
        successful_mutation = any(
            receipt.ok and receipt.state_changed for receipt in receipts
        )
        if successful_mutation:
            transaction_status = GMRuntimeTransactionStatus.PENDING_COMMIT
        elif ledger.mutating_call_attempted:
            transaction_status = GMRuntimeTransactionStatus.UNCOMMITTED
        else:
            transaction_status = GMRuntimeTransactionStatus.READ_ONLY
        feedback = GMRuntimeFeedback(
            phase=GMRuntimeFeedbackPhase.BUILDING_CONTEXT,
            budget=budget,
            transaction_status=transaction_status,
        )

        code_map = {
            item.value: item
            for item in (
                GMRuntimeFeedbackIssueCode.PROVIDER_RECOVERED,
                GMRuntimeFeedbackIssueCode.EMPTY_RESPONSE_RECOVERED,
                GMRuntimeFeedbackIssueCode.RESPONSE_FORMAT_DOWNGRADED,
                GMRuntimeFeedbackIssueCode.CONTEXT_COMPACTED,
                GMRuntimeFeedbackIssueCode.STATE_REFRESH_FAILED,
            )
        }
        phase_map = {
            item.value: item
            for item in (
                GMRuntimeFeedbackPhase.REQUESTING_MODEL,
                GMRuntimeFeedbackPhase.PROVIDER_RECOVERY,
                GMRuntimeFeedbackPhase.REFRESHING_STATE,
                GMRuntimeFeedbackPhase.BUILDING_CONTEXT,
            )
        }
        severity_map = {
            item.value: item
            for item in (
                GMRuntimeFeedbackSeverity.INFO,
                GMRuntimeFeedbackSeverity.WARNING,
                GMRuntimeFeedbackSeverity.ERROR,
            )
        }
        action_map = {
            item.value: item
            for item in (
                GMRuntimeRecoveryAction.NONE,
                GMRuntimeRecoveryAction.USE_RETAINED_AUTHORITATIVE_CONTEXT,
                GMRuntimeRecoveryAction.RETURN_VALID_PROTOCOL_JSON,
            )
        }
        for raw_issue in issues:
            code = code_map.get(
                str(raw_issue.get("code") or "").strip().upper()
            )
            if code is None:
                continue
            feedback.report_issue(
                code=code,
                phase=phase_map.get(
                    str(raw_issue.get("phase") or "").strip().lower(),
                    GMRuntimeFeedbackPhase.BUILDING_CONTEXT,
                ),
                severity=severity_map.get(
                    str(raw_issue.get("severity") or "").strip().lower(),
                    GMRuntimeFeedbackSeverity.WARNING,
                ),
                retryable=raw_issue.get("retryable") is True,
                correction_hint=str(
                    raw_issue.get("correction_hint") or ""
                ),
                recovery_action=action_map.get(
                    str(
                        raw_issue.get("recovery_action") or ""
                    ).strip().lower(),
                    GMRuntimeRecoveryAction.NONE,
                ),
            )

        pending_retry = ledger.pending_required_retry
        if isinstance(pending_retry, dict):
            feedback.report_issue(
                code=GMRuntimeFeedbackIssueCode.TOOL_RETRY_REQUIRED,
                phase=GMRuntimeFeedbackPhase.EXECUTING_TOOL,
                severity=GMRuntimeFeedbackSeverity.WARNING,
                retryable=True,
                tool_name=str(pending_retry.get("tool_name") or ""),
                correction_hint=str(
                    pending_retry.get("correction_hint")
                    or "按上一条权威工具回执修正同一工具调用。"
                ),
                recovery_action=(
                    GMRuntimeRecoveryAction.RETRY_TOOL_WITH_CORRECTION
                ),
            )
        return feedback.to_payload()

    @staticmethod
    def _state_context_mode(context: GMToolExecutionContext) -> str:
        mode = str(
            context.metadata.get("gm_state_context_mode") or "full"
        ).strip().lower()
        return mode if mode in {"full", "summary_delta"} else "full"

    @staticmethod
    def _state_projection_scopes(
        context: GMToolExecutionContext,
        observed_state: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        scopes: set[str] = set()
        if (
            str(
                context.metadata.get("gm_capability_routing_mode") or ""
            ).strip().lower()
            == "intent"
            and context.metadata.get("gm_intent_router_status") == "planned"
        ):
            scopes.update(
                str(item or "").strip()
                for item in list(
                    context.metadata.get("gm_intent_state_scopes") or []
                )
                if str(item or "").strip()
            )
        observation = dict((observed_state or {}).get("observation") or {})
        scopes.update(
            f"domain:{str(item or '').strip()}"
            for item in list(observation.get("expanded_domains") or [])
            if str(item or "").strip()
        )
        if not scopes:
            scopes.add("model_projection")
        return tuple(sorted(scopes))

    @staticmethod
    def _state_projection_profile(
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
    ) -> str:
        observation = dict(observed_state.get("observation") or {})
        scene = dict(observed_state.get("scene") or {})
        gameplay = dict(observed_state.get("gameplay") or {})
        conflict = dict(gameplay.get("conflict") or {})
        routing_mode = str(
            context.metadata.get("gm_capability_routing_mode") or "baseline"
        ).strip().lower()
        identity = {
            "authority_scope": {
                "campaign_id": str(context.campaign_id or ""),
                "session_id": str(context.session_id or ""),
                "channel_id": str(context.channel_id or ""),
                "speaker": str(context.speaker or ""),
                "speaker_id": str(
                    context.metadata.get("speaker_id") or ""
                ),
                "is_private": bool(context.is_private),
            },
            "intent_profiles": (
                sorted(
                    str(item or "").strip()
                    for item in list(
                        context.metadata.get("gm_intent_profile_ids") or []
                    )
                    if str(item or "").strip()
                )
                if routing_mode == "intent"
                else []
            ),
            "observation_profile": str(
                observation.get("profile") or "unknown"
            ),
            "expanded_domains": sorted(
                str(item or "").strip()
                for item in list(
                    observation.get("expanded_domains") or []
                )
                if str(item or "").strip()
            ),
            "gate_status": str(
                observed_state.get("gate_status") or context.gate_status or ""
            ),
            "scene_id": str(scene.get("scene_id") or ""),
            "conflict_id": str(
                conflict.get("conflict_id") or conflict.get("id") or ""
            ),
            "conflict_active": bool(conflict.get("active")),
        }
        digest = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"gm-model-state-{digest}"

    @staticmethod
    def _state_delta_source_tool(
        receipts: list[GMToolReceipt],
        *,
        cursor: int,
    ) -> str:
        names = [
            str(receipt.tool_name or "").strip()
            for receipt in receipts[max(0, int(cursor or 0)) :]
            if str(receipt.tool_name or "").strip()
        ]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        return "batch:" + ",".join(names)

    def _prepare_turn_state_context(
        self,
        *,
        observed_state: dict[str, object],
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        tracker: GMTurnStateDeltaTracker | None,
        receipt_cursor: int,
        enabled: bool,
    ) -> tuple[
        dict[str, object],
        dict[str, object] | None,
        GMTurnStateDeltaTracker | None,
        int,
    ]:
        """Build a verified base-plus-cumulative-delta model payload.

        ``observed_state`` remains the live input for grounding and tool
        execution.  Only the model request receives the returned projection.
        Any tracker error widens safely to the existing full projection for
        that iteration.
        """

        if not enabled:
            return observed_state, None, None, len(receipts)
        revision = int(
            context.metadata.get("_gm_campaign_observed_version") or 0
        )
        scopes = self._state_projection_scopes(context, observed_state)
        profile = self._state_projection_profile(context, observed_state)
        visibility = {
            "audience": "gm_private_model_view",
            "is_private_thread": bool(context.is_private),
        }
        source_tool = self._state_delta_source_tool(
            receipts,
            cursor=receipt_cursor,
        )
        try:
            if tracker is None:
                tracker = GMTurnStateDeltaTracker(
                    observed_state,
                    base_revision=revision,
                    projection_version="gm-model-projection-v1",
                    scopes=scopes,
                    profile=profile,
                    visibility=visibility,
                    budget=GMTurnStateDeltaBudget(
                        max_ratio=0.45,
                        max_operations=48,
                        max_chars=6_000,
                    ),
                )
            else:
                tracker.update(
                    observed_state,
                    source_tool=source_tool,
                    base_revision=revision,
                    projection_version="gm-model-projection-v1",
                    scopes=scopes,
                    profile=profile,
                    visibility=visibility,
                )
            if not tracker.verify(observed_state):
                raise ValueError(
                    "turn state delta does not reconstruct the observed projection"
                )
            envelope = tracker.envelope()
            base_projection = dict(envelope.pop("base_projection") or {})
            context.metadata["_gm_state_delta_manifest"] = {
                "status": "verified",
                "base_hash": str(envelope.get("base_hash") or ""),
                "effective_hash": str(
                    envelope.get("effective_hash") or ""
                ),
                "operation_count": len(list(envelope.get("ops") or [])),
                "delta_chars": len(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "reset_reason": str(
                    envelope.get("reset_reason") or ""
                ),
            }
            return base_projection, envelope, tracker, len(receipts)
        except Exception as exc:
            context.metadata["_gm_state_delta_manifest"] = {
                "status": "full_fallback",
                "error_type": type(exc).__name__,
            }
            return observed_state, None, None, len(receipts)

    def _build_decision_messages(
        self,
        *,
        current_message: str,
        recent_context: str,
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
        prompt_state_summary: dict[str, object] | None = None,
        turn_state_delta: dict[str, object] | None = None,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        required_retry_tool: str = "",
        runtime_feedback: dict[str, object] | None = None,
    ) -> list[ChatMessage]:
        request_context_keys = (
            "forced_route_mode",
            "system_gm_beat_request",
            "heartbeat_action",
            "heartbeat_beat_purpose",
            "heartbeat_instruction",
            "heartbeat_force",
            "heartbeat_require_material_change",
            "heartbeat_require_consequence",
            "heartbeat_require_local_change",
            "heartbeat_require_local_resolution",
            "heartbeat_require_session_resolution",
            "heartbeat_require_signature_image_evolution",
            "heartbeat_idle_episode",
            "heartbeat_session_zero_target",
            "heartbeat_supervisor_alerts",
            "heartbeat_defeat_aftermath",
            "heartbeat_persona_chat_only",
            "gm_authored_scene_opening",
            "gm_authored_free_scene_beat",
            "inspection_focus",
            "message_semantics_contract_required",
        )
        request_context = {
            key: context.metadata[key]
            for key in request_context_keys
            if key in context.metadata
        }
        persona_chat_only = bool(
            context.metadata.get("heartbeat_persona_chat_only")
            and str(context.metadata.get("heartbeat_action") or "")
            == "adventure_table_nudge"
        )
        if not persona_chat_only:
            request_context.update(
                GMMessageEnvelopeBuilder.model_request_context(context.metadata)
            )
        current_turn = request_context.pop("current_turn", None)
        recent_messages = (
            context.metadata.get("recent_public_messages")
            if persona_chat_only
            else request_context.pop("recent_messages", None)
        )
        available_tools = self._available_tool_schemas(
            context,
            receipts=receipts,
            required_retry_tool=required_retry_tool,
        )
        # Put granted schemas before authoritative state and turn-local prose.
        # Tool schemas are the largest stable user-side block and remain
        # reusable even when a preceding rules action changed campaign state.
        # When state is unchanged, the second boundary reuses both blocks.
        delta_context = bool(
            isinstance(turn_state_delta, dict) and not persona_chat_only
        )
        prompt_layout_version = (
            GM_DELTA_PROMPT_LAYOUT_VERSION
            if delta_context
            else GM_PROMPT_LAYOUT_VERSION
        )
        if persona_chat_only:
            request = {
                "prompt_layout_version": prompt_layout_version,
                "available_tools": [],
                "recent_messages": [
                    dict(item)
                    for item in list(recent_messages or [])
                    if isinstance(item, dict)
                    and str(item.get("role") or "")
                    in {"user", "player", "table_talk"}
                ],
                "request_context": {
                    "heartbeat_action": "adventure_table_nudge",
                    "heartbeat_persona_chat_only": True,
                },
                **({"history": history} if history else {}),
            }
        else:
            request = {
                "prompt_layout_version": prompt_layout_version,
                "available_tools": available_tools,
                "current_state_summary": (
                    prompt_state_summary
                    if isinstance(prompt_state_summary, dict)
                    else observed_state
                ),
            }
            if delta_context:
                request["turn_state_delta"] = dict(turn_state_delta or {})
            request.update({
                "current_message": current_message,
                "current_turn": current_turn
                or {
                    "message_count": 1,
                    "events": [
                        {
                            "speaker": context.speaker,
                            "text": current_message,
                        }
                    ],
                },
                "recent_messages": list(recent_messages or []),
                "session": {
                    "campaign_id": context.campaign_id,
                    "session_id": context.session_id,
                    "speaker": context.speaker,
                    "gate_status": context.gate_status,
                    "is_private": context.is_private,
                    "directly_addressed": context.directly_addressed,
                },
                "request_context": request_context,
                "history": history,
            })
            frozen_message_semantics = context.metadata.get(
                self._MESSAGE_SEMANTICS_METADATA_KEY
            )
            if isinstance(frozen_message_semantics, dict):
                request["frozen_message_semantics"] = deepcopy(
                    frozen_message_semantics
                )
        if runtime_feedback:
            request.update(runtime_feedback)
        prompt_family = "gm-agent"
        system_prompt = self._system_prompt(
            context,
            observed_state=observed_state,
            has_receipts=bool(receipts),
        )
        if delta_context:
            system_prompt = (
                system_prompt
                + "\n\n"
                + TURN_STATE_DELTA_SYSTEM_PROMPT
            )
        core_prefix = (
            self.gm_personality_prompt or TABLE_CHAT_HEARTBEAT_SYSTEM_PROMPT
            if persona_chat_only
            else CORE_AGENT_SYSTEM_PREFIX
        )
        governed = self.context_governor.govern(
            json_safe_value(request),
            state_version=int(
                context.metadata.get("_gm_campaign_observed_version") or 0
            ),
            prompt_layout_version=prompt_layout_version,
            layout_fingerprint=prompt_layout_fingerprint(
                static_system_prompt=system_prompt,
                tool_schemas=available_tools,
                layout_version=prompt_layout_version,
            ),
            protected_root_fields=(
                ("current_state_summary", "turn_state_delta")
                if delta_context
                else None
            ),
        )
        if delta_context:
            try:
                governed_base = governed.request.get(
                    "current_state_summary"
                )
                governed_delta = governed.request.get("turn_state_delta")
                if not isinstance(governed_base, dict) or not isinstance(
                    governed_delta,
                    dict,
                ):
                    raise ValueError("governed delta roots are missing")
                applied = apply_state_delta(
                    governed_base,
                    list(governed_delta.get("ops") or []),
                )
                if projection_hash(governed_base) != str(
                    governed_delta.get("base_hash") or ""
                ):
                    raise ValueError("governed base hash changed")
                if projection_hash(applied) != str(
                    governed_delta.get("effective_hash") or ""
                ):
                    raise ValueError("governed effective hash changed")
            except Exception as exc:
                context.metadata["_gm_state_delta_manifest"] = {
                    "status": "full_fallback",
                    "error_type": type(exc).__name__,
                    "reset_reason": "post_govern_verification_failed",
                }
                return self._build_decision_messages(
                    current_message=current_message,
                    recent_context=recent_context,
                    context=context,
                    observed_state=observed_state,
                    prompt_state_summary=observed_state,
                    turn_state_delta=None,
                    receipts=receipts,
                    history=history,
                    required_retry_tool=required_retry_tool,
                    runtime_feedback=runtime_feedback,
                )
        context_manifest = governed.manifest.to_dict()
        routing_mode = str(
            context.metadata.get("gm_capability_routing_mode") or "baseline"
        ).strip().lower()
        profile_ids = sorted(
            str(item or "").strip()
            for item in list(
                context.metadata.get("gm_intent_profile_ids") or []
            )
            if str(item or "").strip()
        )
        schema_names = sorted(
            str(schema.get("name") or "")
            for schema in available_tools
            if str(schema.get("name") or "")
        )
        context_manifest.update(
            {
                "capability_routing_mode": routing_mode,
                "capability_profile_ids": (
                    profile_ids if routing_mode == "intent" else []
                ),
                "shadow_profile_ids": (
                    profile_ids if routing_mode == "shadow" else []
                ),
                "schema_count": len(available_tools),
                "schema_chars": len(
                    json.dumps(
                        available_tools,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                "schema_names_hash": hashlib.sha256(
                    "\n".join(schema_names).encode("utf-8")
                ).hexdigest()[:16],
                "state_context_mode": (
                    "summary_delta" if delta_context else "full"
                ),
            }
        )
        delta_manifest = context.metadata.get("_gm_state_delta_manifest")
        if isinstance(delta_manifest, dict):
            context_manifest.update(
                {
                    "state_delta_status": str(
                        delta_manifest.get("status") or ""
                    ),
                    "state_base_hash": str(
                        delta_manifest.get("base_hash") or ""
                    ),
                    "state_effective_hash": str(
                        delta_manifest.get("effective_hash") or ""
                    ),
                    "state_delta_operations": int(
                        delta_manifest.get("operation_count") or 0
                    ),
                    "state_delta_chars": int(
                        delta_manifest.get("delta_chars") or 0
                    ),
                    "state_reset_reason": str(
                        delta_manifest.get("reset_reason") or ""
                    ),
                }
            )
        context.metadata["_gm_context_manifest"] = context_manifest
        request_json = governed.rendered
        state_boundary_offset = request_json.find('"current_state_summary"')
        second_boundary_offset = request_json.find(
            '"turn_state_delta"'
            if delta_context
            else '"current_message"'
        )
        messages = build_cache_friendly_messages(
            static_system_prompt=system_prompt,
            user_content=request_json,
            cache_family=prompt_family,
            cache_breakpoint_offsets=(
                len(core_prefix),
                len(system_prompt),
            ),
            # Together with the two system layers this stays within the four
            # explicit breakpoints supported by the configured gateways.
            user_cache_breakpoint_offsets=tuple(
                offset
                for offset in (state_boundary_offset, second_boundary_offset)
                if offset >= 0
            ),
        )
        return messages

    def _decision_trace_step(
        self,
        decision: dict[str, object],
        *,
        iteration: int,
    ) -> dict[str, object]:
        step: dict[str, object] = {
            "iteration": iteration,
            "decision": str(decision.get("decision") or ""),
            "message_kind": str(decision.get("message_kind") or ""),
            "has_independent_followup": (
                decision.get("has_independent_followup") is True
            ),
            "audience": str(decision.get("audience") or ""),
            "tool_name": str(decision.get("tool_name") or ""),
            "reason": str(decision.get("reason") or "")[:300],
        }
        if isinstance(decision.get("delivery"), dict):
            step["proposed_delivery"] = self._protocol.trace_arguments(
                decision.get("delivery")
            )
        action = str(decision.get("decision") or "").strip().lower()
        if action == "call_tool":
            step["arguments"] = self._protocol.trace_arguments(
                decision.get("arguments")
            )
        elif action == "call_tools":
            step["calls"] = [
                {
                    "tool_name": str(call.get("tool_name") or ""),
                    "arguments": self._protocol.trace_arguments(
                        call.get("arguments")
                    ),
                }
                for call in (decision.get("calls") or [])
                if isinstance(call, dict)
            ]
        return step

    def _freeze_message_semantics(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
        history: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
    ) -> bool:
        """Validate the first interpretation and keep it immutable for retries.

        The model still performs all semantic interpretation. Python only checks
        that every transport event received one well-formed interpretation and
        prevents a later tool retry from silently reclassifying the same words.
        """

        required = bool(
            context.metadata.get("message_semantics_contract_required")
        ) and not is_system_beat
        if not required:
            return False
        source_events = [
            dict(item)
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        frozen = context.metadata.get(self._MESSAGE_SEMANTICS_METADATA_KEY)
        provisional = context.metadata.get(
            self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY
        )
        raw = decision.get("message_semantics")
        if isinstance(frozen, dict) and raw is None:
            decision["message_semantics"] = deepcopy(frozen)
            step["message_semantics"] = deepcopy(frozen)
            step["message_semantics_source"] = "frozen_initial_decision"
            return False
        if isinstance(frozen, dict):
            # The first validated interpretation is the transaction authority.
            # Later model turns only repair tools or wording, so reparsing a
            # repeated semantic payload cannot add safety.  Ignore any drift
            # and inject the frozen value instead of spending every remaining
            # iteration asking the model to reproduce byte-identical JSON.
            if raw is not None and raw != frozen:
                step["message_semantics_model_drift_ignored"] = True
            decision["message_semantics"] = deepcopy(frozen)
            step["message_semantics"] = deepcopy(frozen)
            step["message_semantics_source"] = "frozen_initial_decision"
            return False
        action = str(decision.get("decision") or "").strip().lower()
        if isinstance(provisional, dict) and raw is None:
            if action in {"call_tool", "call_tools"}:
                repair_error = GMMessageSemanticsError(
                    "MESSAGE_STATE_INTENT_REPAIR_REQUIRED",
                    "首轮语义与其状态工具矛盾，修复轮不能省略message_semantics。",
                    (
                        "保留上一轮的event_id、speaker、relation、targets、"
                        "dialogue_act、action_commitment、response_expectation与"
                        "responds_to_event_id，"
                        "重新输出完整message_semantics并补全准确state_intents；"
                        "若上一轮工具选择本身错误，则改用silent或普通回应。"
                    ),
                )
                history.append(repair_error.to_protocol_error())
                step["protocol_error"] = repair_error.code
                step["message_semantics_repair_required"] = True
                return True
            raw = deepcopy(provisional)
        try:
            parsed = GMMessageSemantics.parse(raw, source_events=source_events)
        except GMMessageSemanticsError as exc:
            history.append(exc.to_protocol_error())
            step["protocol_error"] = exc.code
            step["message_semantics_error"] = str(exc)[:500]
            return True
        if isinstance(provisional, dict):
            try:
                prior = GMMessageSemantics.parse(
                    provisional,
                    source_events=source_events,
                )
            except GMMessageSemanticsError:
                context.metadata.pop(
                    self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY,
                    None,
                )
                prior = None
            if prior is not None:
                def core_signature(item: GMMessageSemantics) -> tuple[object, ...]:
                    return tuple(
                        (
                            event.event_id,
                            event.speaker,
                            event.relation,
                            tuple(event.targets),
                            event.dialogue_act,
                            event.action_commitment,
                            event.response_expectation,
                            event.responds_to_event_id,
                        )
                        for event in item.events
                    )

                if core_signature(parsed) != core_signature(prior):
                    repair_error = GMMessageSemanticsError(
                        "MESSAGE_SEMANTICS_CORE_DRIFT_DURING_REPAIR",
                        "状态意图修复改变了已判定的消息关系或行动承诺。",
                        (
                            "保持上一轮event_id、speaker、relation、targets、"
                            "dialogue_act、action_commitment、response_expectation与"
                            "responds_to_event_id"
                            "逐项不变；只修正state_scope、state_intents和reason。"
                        ),
                    )
                    history.append(repair_error.to_protocol_error())
                    step["protocol_error"] = repair_error.code
                    step["message_semantics_core_drift"] = True
                    return True
        parsed, ignored_self_confirmations = (
            self._normalize_session_zero_self_confirmations(
                parsed,
                context=context,
                observed_state=observed_state,
            )
        )
        if ignored_self_confirmations:
            step["ignored_self_confirmations"] = ignored_self_confirmations
        grounding_error = self._session_zero_semantics_grounding_error(
            parsed,
            context=context,
            observed_state=observed_state,
        )
        if grounding_error is not None:
            history.append(grounding_error.to_protocol_error())
            step["protocol_error"] = grounding_error.code
            step["message_semantics_grounding_error"] = str(
                grounding_error
            )[:500]
            return True
        if action == "call_tool":
            initial_calls = (
                {
                    "tool_name": decision.get("tool_name"),
                    "arguments": decision.get("arguments"),
                },
            )
        elif action == "call_tools":
            initial_calls = tuple(
                dict(item)
                for item in list(decision.get("calls") or [])
                if isinstance(item, dict)
            )
        else:
            initial_calls = ()
        for call in initial_calls:
            tool_name = str(call.get("tool_name") or "").strip()
            authority_error = tool_semantic_authority_error(
                tool_name=tool_name,
                arguments=call.get("arguments"),
                semantics=parsed,
            )
            if authority_error is None:
                continue
            rule_window_repair = self._rule_window_answer_semantics_repair(
                semantics=parsed,
                call=call,
                context=context,
                observed_state=observed_state,
                authority_error=authority_error,
            )
            if rule_window_repair is not None:
                history.append(rule_window_repair.to_protocol_error())
                step["protocol_error"] = rule_window_repair.code
                step["message_semantics_rule_window_repair"] = True
                context.metadata.pop(
                    self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY,
                    None,
                )
                return True
            repairable_hero_field_interpretation = (
                authority_error.code == "MESSAGE_STATE_INTENT_TOOL_MISMATCH"
                and tool_name == "update_hero_draft"
            )
            if (
                authority_error.code != "MESSAGE_STATE_INTENT_REQUIRED"
                and not repairable_hero_field_interpretation
            ):
                # The interpretation itself is valid and must remain stable.
                # Freeze it below; the ordinary authority gate will reject the
                # mismatched tool while preventing a later semantic rewrite.
                step["initial_tool_authority_mismatch"] = {
                    "tool_name": tool_name,
                    "code": authority_error.code,
                }
                continue
            # The interpretation has not been frozen yet. Returning the
            # model's own semantic/tool contradiction now lets the next turn
            # repair both together; freezing first would make every later
            # retry fail against an immutable, incomplete state-intent list.
            repair_error = authority_error
            if repairable_hero_field_interpretation:
                repair_error = GMMessageSemanticsError(
                    authority_error.code,
                    str(authority_error),
                    (
                        "保持event_id、speaker、relation、targets、dialogue_act、"
                        "action_commitment与responds_to_event_id不变，重新核对玩家原话："
                        "若所属玩家已经完整给出自己的角色字段，将对应state_intent改为"
                        "contribute；若明确替换既有字段则改为correct；若只是暂定候选或"
                        "建议他人的角色，不调用update_hero_draft。赞同他人已经写入的"
                        "角色字段不产生持久state_intent。不要改写玩家原话，也不能借此"
                        "修改世界、场景或其他玩家的角色。"
                    ),
                )
            history.append(repair_error.to_protocol_error())
            step["protocol_error"] = authority_error.code
            step["message_semantics_tool_mismatch"] = {
                "tool_name": tool_name,
                "message": str(authority_error)[:500],
            }
            context.metadata[
                self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY
            ] = deepcopy(parsed.to_dict())
            return True
        normalized = parsed.to_dict()
        if not isinstance(frozen, dict):
            context.metadata[self._MESSAGE_SEMANTICS_METADATA_KEY] = deepcopy(
                normalized
            )
            step["message_semantics_source"] = "initial_decision"
        context.metadata.pop(
            self._PROVISIONAL_MESSAGE_SEMANTICS_METADATA_KEY,
            None,
        )
        decision["message_semantics"] = deepcopy(normalized)
        step["message_semantics"] = deepcopy(normalized)
        return False

    @staticmethod
    def _rule_window_answer_semantics_repair(
        *,
        semantics: GMMessageSemantics,
        call: dict[str, object],
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
        authority_error: GMMessageSemanticsError,
    ) -> GMMessageSemanticsError | None:
        """Let the model repair one internally inconsistent window answer.

        No player prose is interpreted here.  Repair is available only when the
        model itself called the resolver, labelled the source as an answer to the
        GM, and referenced an active window owned by that source speaker.
        """

        if (
            authority_error.code != "RULE_WINDOW_NOT_ANSWERED_BY_SOURCE_MESSAGE"
            or str(call.get("tool_name") or "").strip() != "resolve_rule_window"
        ):
            return None
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            return None
        source = semantics.source_event(arguments)
        if (
            source is None
            or source.relation not in {"gm", "mixed"}
            or source.dialogue_act not in {"answer", "correction"}
            or source.action_commitment == "answer"
        ):
            return None
        window_id = str(arguments.get("window_id") or "").strip()
        decisions = dict(
            dict(observed_state.get("processes") or {}).get("decisions") or {}
        )
        pending = next(
            (
                dict(item)
                for item in list(decisions.get("pending") or [])
                if isinstance(item, dict)
                and str(item.get("window_id") or "").strip() == window_id
            ),
            None,
        )
        if pending is None:
            return None
        speaker = str(source.speaker or context.speaker or "").strip()
        allowed_speakers = {
            str(item or "").strip()
            for item in list(pending.get("allowed_speakers") or [])
            if str(item or "").strip()
        }
        owner = str(pending.get("owner") or "").strip()
        turn_participants = dict(observed_state.get("turn_participants") or {})
        controls = {
            str(item or "").strip()
            for item in list(
                dict(
                    turn_participants.get("controlled_characters_by_speaker") or {}
                ).get(speaker, [])
            )
            if str(item or "").strip()
        }
        if not (
            speaker in allowed_speakers
            or owner == speaker
            or owner in controls
        ):
            return None
        return GMMessageSemanticsError(
            "RULE_WINDOW_ANSWER_COMMITMENT_REPAIR_REQUIRED",
            (
                f"事件{source.event_id}被判断为对活动待决窗口{window_id}的回答，"
                "但action_commitment没有使用answer。"
            ),
            (
                "重新输出完整message_semantics；保持event_id、speaker、relation、"
                "targets、dialogue_act与窗口工具调用不变，仅将该事件的"
                "action_commitment改为answer。完成既有规则选择不等于新增角色行动。"
            ),
        )

    def _normalize_session_zero_self_confirmations(
        self,
        semantics: GMMessageSemantics,
        *,
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
    ) -> tuple[GMMessageSemantics, list[dict[str, str]]]:
        """Remove confirmations that cannot add table authority.

        This is an authority normalization, not a prose classifier. The model
        has already interpreted the message and selected a persisted proposal.
        In a multiplayer Session Zero, its author cannot turn that proposal
        into table consensus by agreeing with themself. The rest of a compound
        message remains intact and is frozen normally.
        """

        if context.gate_status not in {"pre_session", "session_zero"}:
            return semantics, []
        validator = self._message_integrity_validator
        if not validator._pending_proposals_state_is_authoritative(
            observed_state
        ):
            return semantics, []
        session_zero_state = observed_state.get("session_zero")
        session_zero_state = (
            session_zero_state
            if isinstance(session_zero_state, dict)
            else {}
        )
        participants = {
            str(item or "").strip()
            for item in list(session_zero_state.get("participants") or [])
            if str(item or "").strip()
        }
        if len(participants) <= 1:
            return semantics, []

        pending = tuple(validator._pending_proposals(observed_state))
        pending_by_id = {
            str(item.get("id") or "").strip(): item
            for item in pending
            if str(item.get("id") or "").strip()
        }
        changed_events = []
        ignored: list[dict[str, str]] = []
        for event in semantics.events:
            retained = []
            for intent in event.state_intents:
                if intent.operation != "confirm":
                    retained.append(intent)
                    continue
                proposal_id = str(intent.proposal_id or "").strip()
                proposal = pending_by_id.get(proposal_id)
                if proposal is None and not proposal_id:
                    subject = self._canonical_proposal_subject(intent.subject)
                    candidates = [
                        item
                        for item in pending
                        if subject
                        in set(validator._proposal_subjects_for_pending(item))
                    ]
                    if len(candidates) == 1:
                        proposal = candidates[0]
                        proposal_id = str(proposal.get("id") or "").strip()
                proposal_speaker = str(
                    proposal.get("speaker") if isinstance(proposal, dict) else ""
                ).strip()
                if proposal_speaker and proposal_speaker == event.speaker:
                    ignored.append(
                        {
                            "event_id": event.event_id,
                            "proposal_id": proposal_id,
                            "subject": intent.subject,
                        }
                    )
                    continue
                retained.append(intent)

            if len(retained) == len(event.state_intents):
                changed_events.append(event)
                continue
            remaining_scopes = {item.scope for item in retained}
            state_scope = (
                "none"
                if not remaining_scopes
                else next(iter(remaining_scopes))
                if len(remaining_scopes) == 1
                else "mixed"
            )
            changed_events.append(
                replace(
                    event,
                    state_scope=state_scope,
                    state_intents=tuple(retained),
                )
            )
        if not ignored:
            return semantics, []
        return replace(semantics, events=tuple(changed_events)), ignored

    def _session_zero_semantics_grounding_error(
        self,
        semantics: GMMessageSemantics,
        *,
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
    ) -> GMMessageSemanticsError | None:
        """Ground ``confirm`` intents in the persisted proposal ledger.

        The model still decides what the player means. Python only verifies
        that a semantic operation whose definition requires an existing
        proposal points to one that actually exists. This runs before the
        interpretation is frozen, allowing the model to repair a mistaken
        agreement/proposal split once instead of retrying an impossible tool
        call for the rest of the transaction.
        """

        if context.gate_status not in {"pre_session", "session_zero"}:
            return None
        validator = self._message_integrity_validator
        if not validator._pending_proposals_state_is_authoritative(
            observed_state
        ):
            return None
        pending = tuple(validator._pending_proposals(observed_state))
        pending_by_id = {
            str(item.get("id") or "").strip(): (index, item)
            for index, item in enumerate(pending)
            if str(item.get("id") or "").strip()
        }
        session_zero_state = observed_state.get("session_zero")
        session_zero_state = (
            session_zero_state
            if isinstance(session_zero_state, dict)
            else {}
        )
        participant_names = {
            str(item or "").strip()
            for item in list(session_zero_state.get("participants") or [])
            if str(item or "").strip()
        }
        multiplayer = len(participant_names) > 1
        for event in semantics.events:
            selected_proposal_ids: list[str] = []
            for intent in event.state_intents:
                if (
                    intent.operation != "confirm"
                    or intent.scope not in {"group", "world", "scene"}
                ):
                    continue
                subject = self._canonical_proposal_subject(intent.subject)
                proposal_id = str(intent.proposal_id or "").strip()
                matching: list[dict[str, object]] = []
                if proposal_id:
                    matching = [
                        dict(proposal)
                        for proposal in pending
                        if str(proposal.get("id") or "").strip()
                        == proposal_id
                    ]
                    if matching and subject:
                        exact_categories = (
                            self._semantic_pending_proposal_categories(
                                matching[0]
                            )
                        )
                        proposal_subjects = set(
                            validator._proposal_subjects_for_pending(
                                matching[0]
                            )
                        )
                        if (
                            intent.subject not in exact_categories
                            and subject not in exact_categories
                            and subject not in proposal_subjects
                        ):
                            matching = []
                else:
                    broad_candidates = [
                        dict(proposal)
                        for proposal in pending
                        if not subject
                        or subject
                        in set(
                                validator._proposal_subjects_for_pending(proposal)
                            )
                    ]
                    exact_candidates = [
                        dict(proposal)
                        for proposal in broad_candidates
                        if intent.subject
                        in self._semantic_pending_proposal_categories(proposal)
                    ]
                    # Omitting an internal proposal id is safe only when both
                    # the exact world-setting category and the broader
                    # confirmation subject identify the same sole proposal.
                    # In particular, world_shape and map_locations both roll
                    # up to world_map but are not interchangeable facts.
                    if (
                        len(exact_candidates) == 1
                        and len(broad_candidates) == 1
                    ):
                        matching = exact_candidates
                if matching:
                    matched_proposal = matching[0]
                    proposal_speaker = str(
                        matched_proposal.get("speaker") or ""
                    ).strip()
                    if (
                        multiplayer
                        and proposal_speaker
                        and proposal_speaker == event.speaker
                    ):
                        return GMMessageSemanticsError(
                            "MESSAGE_CONFIRM_OWN_PROPOSAL",
                            (
                                f"事件{event.event_id}把{event.speaker}自己的待定提案"
                                "标成了confirm；这不会产生新的全桌同意。"
                            ),
                            (
                                "保持玩家原话不变重新拆分state_intents：不要确认自己的"
                                "旧提案。若本句提出了同主题的新综合版本，保留propose，"
                                "执行propose_session_zero_update时把旧提案ID放入"
                                "superseded_proposal_ids；若只是重申旧想法，则不新增"
                                "confirm，继续等待另一名玩家明确赞成。"
                            ),
                        )
                    if proposal_id:
                        selected_proposal_ids.append(proposal_id)
                    continue
                available = [
                    str(item.get("summary") or "").strip()[:120]
                    for item in pending[:4]
                    if str(item.get("summary") or "").strip()
                ]
                available_hint = (
                    "当前待定提案只有：" + "；".join(available) + "。"
                    if available
                    else "当前没有任何待确认提案。"
                )
                target_label = intent.target or intent.subject
                candidate_ids = [
                    str(item.get("id") or "").strip()
                    for item in pending
                    if str(item.get("id") or "").strip()
                ][:8]
                return GMMessageSemanticsError(
                    "MESSAGE_CONFIRM_TARGET_NOT_PENDING",
                    (
                        f"事件{event.event_id}把“{target_label}”标为confirm，"
                        "但权威待定提案中没有匹配对象。"
                    ),
                    (
                        available_hint
                        + "保持玩家原话不变并重新拆分state_intents：confirm只用于"
                        "确实存在的待定提案；赞同已经正式写入的事实不产生confirm；"
                        "同句新增的暂定细节使用propose，明确改定使用correct。"
                        + (
                            "若是确认待定提案，proposal_id必须从以下权威ID中逐字选择："
                            + "、".join(candidate_ids)
                            + "。"
                            if candidate_ids
                            else ""
                        )
                    ),
                )
            selected_proposal_ids = list(
                dict.fromkeys(selected_proposal_ids)
            )
            if len(selected_proposal_ids) < 2:
                continue
            selected_entries = [
                pending_by_id[proposal_id]
                for proposal_id in selected_proposal_ids
                if proposal_id in pending_by_id
            ]
            selected_subjects = set().union(
                *(
                    self._semantic_pending_proposal_subject_keys(proposal)
                    for _index, proposal in selected_entries
                )
            )
            if not selected_subjects or not selected_entries:
                continue
            latest_selected_index = max(index for index, _item in selected_entries)
            target_speakers = set(event.targets)
            newer_composites = [
                item
                for index, item in enumerate(pending)
                if index > latest_selected_index
                and str(item.get("speaker") or "").strip()
                in target_speakers
                and self._semantic_pending_proposal_subject_keys(item)
                == selected_subjects
            ]
            if not newer_composites:
                continue
            newest = newer_composites[-1]
            newest_id = str(newest.get("id") or "").strip()
            return GMMessageSemanticsError(
                "MESSAGE_CONFIRM_NEWER_COMPOSITE_PENDING",
                (
                    f"事件{event.event_id}把同一组对象拆回了多条旧提案，"
                    "但被回应玩家已有一条更晚的组合版本。"
                ),
                (
                    "保持玩家原话不变，把这些确认合并为一项confirm；"
                    f"proposal_id使用最新组合提案{newest_id}，summary保留其完整关系，"
                    "不要分别确认更早的组成草稿。"
                ),
            )
        return None

    @staticmethod
    def _semantic_pending_proposal_categories(
        proposal: dict[str, object],
    ) -> set[str]:
        """Read exact setting categories without interpreting proposal prose."""

        categories: set[str] = set()
        for item in list(proposal.get("subject_keys") or []):
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            if category:
                categories.add(category)
        for item in list(proposal.get("world_operations") or []):
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            if category:
                categories.add(category)
        for item in list(proposal.get("scope_categories") or []):
            category = str(item or "").strip()
            if category:
                categories.add(category)
        proposed_updates = proposal.get("proposed_updates")
        if isinstance(proposed_updates, dict):
            categories.update(
                str(item or "").strip()
                for item in proposed_updates
                if str(item or "").strip()
            )
        return categories

    @staticmethod
    def _semantic_pending_proposal_subject_keys(
        proposal: dict[str, object],
    ) -> set[tuple[str, str, str]]:
        """Read stable proposal subjects without interpreting player prose."""

        keys: set[tuple[str, str, str]] = set()
        raw_keys = proposal.get("subject_keys")
        if isinstance(raw_keys, list):
            for item in raw_keys:
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category") or "").strip()
                visibility = str(
                    item.get("visibility") or "public"
                ).strip()
                name = re.sub(
                    r"\s+",
                    "",
                    str(item.get("name") or "").strip(),
                )
                if category and item.get("singleton") is True:
                    keys.add((category, visibility, "__singleton__"))
                elif category and name:
                    keys.add((category, visibility, name))
        if keys:
            return keys
        operations = proposal.get("world_operations")
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                category = str(operation.get("category") or "").strip()
                visibility = str(
                    operation.get("visibility") or "public"
                ).strip()
                names = (
                    operation.get("name"),
                    operation.get("old_name"),
                )
                clean_names = {
                    re.sub(r"\s+", "", str(name or "").strip())
                    for name in names
                    if str(name or "").strip()
                }
                if category and clean_names:
                    keys.update(
                        (category, visibility, name)
                        for name in clean_names
                    )
        return keys

    def _reject_tools_without_message_authority(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
    ) -> bool:
        """Reject action tools that contradict the model's frozen semantics."""

        if not context.metadata.get("message_semantics_contract_required"):
            return False
        raw_semantics = context.metadata.get(self._MESSAGE_SEMANTICS_METADATA_KEY)
        source_events = [
            dict(item)
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        try:
            semantics = GMMessageSemantics.parse(
                raw_semantics,
                source_events=source_events,
            )
        except GMMessageSemanticsError as exc:
            history.append(exc.to_protocol_error())
            step["protocol_error"] = exc.code
            return True

        action = str(decision.get("decision") or "").strip().lower()
        if action == "call_tool":
            calls = [
                {
                    "tool_name": decision.get("tool_name"),
                    "arguments": decision.get("arguments"),
                }
            ]
        else:
            calls = [
                dict(item)
                for item in list(decision.get("calls") or [])
                if isinstance(item, dict)
            ]
        required_followups = set(
            self._receipt_policy.required_followup_tools(receipts) or set()
        )
        for call in calls:
            tool_name = str(call.get("tool_name") or "").strip()
            # Once a successful receipt explicitly requires another tool, that
            # Python-signed continuation is authorized by the receipt rather than
            # by reinterpreting the player's short answer as a second action.
            if tool_name in required_followups:
                continue
            error = tool_semantic_authority_error(
                tool_name=tool_name,
                arguments=call.get("arguments"),
                semantics=semantics,
            )
            if error is None:
                continue
            history.append(error.to_protocol_error())
            step["protocol_error"] = error.code
            step["message_authority_rejection"] = {
                "tool_name": tool_name,
                "message": str(error)[:500],
            }
            return True
        return False

    @staticmethod
    def _capability_routing_trace(
        decision: dict[str, object],
        *,
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        mode = str(
            context.metadata.get("gm_capability_routing_mode") or "baseline"
        ).strip().lower()
        if mode not in {"shadow", "intent"}:
            return {}
        action = str(decision.get("decision") or "").strip().lower()
        actual_tools: list[str] = []
        if action == "call_tool":
            name = str(decision.get("tool_name") or "").strip()
            if name:
                actual_tools.append(name)
        elif action == "call_tools":
            actual_tools.extend(
                str(call.get("tool_name") or "").strip()
                for call in list(decision.get("calls") or [])
                if isinstance(call, dict)
                and str(call.get("tool_name") or "").strip()
            )
        candidates = {
            str(item or "").strip()
            for item in list(
                context.metadata.get("gm_intent_tool_names") or []
            )
            if str(item or "").strip()
        }
        # Discovery is the intentionally retained escape hatch rather than an
        # intent miss.  It can safely expand only this message transaction.
        candidates.add(GMCapabilityBroker.DISCOVERY_TOOL)
        misses = sorted(set(actual_tools) - candidates)
        return {
            "mode": mode,
            "profile_revision": "intent-profiles-v1",
            "profile_ids": sorted(
                str(item or "").strip()
                for item in list(
                    context.metadata.get("gm_intent_profile_ids") or []
                )
                if str(item or "").strip()
            ),
            "confidence": float(
                context.metadata.get("gm_intent_confidence") or 0.0
            ),
            "router_status": str(
                context.metadata.get("gm_intent_router_status") or ""
            ),
            "actual_tools": sorted(set(actual_tools)),
            "candidate_miss": bool(misses),
            "candidate_miss_tools": misses,
            "fallback_discovery": bool(
                context.metadata.get("gm_intent_fallback_discovery")
            ),
        }

    def _handle_terminal_action(
        self,
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_decide: bool,
        must_reply_on_failure: bool,
        material_change_required: bool,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        """Resolve decisions that do not execute another tool call."""

        authority_hold = self._material_change_silence_allowed(
            context=context,
            receipts=receipts,
            terminal=action,
            is_system_beat=is_system_beat,
        )
        if material_change_required and not authority_hold and not any(
            self._receipt_policy.public_material_change_committed(receipt)
            for receipt in receipts
        ):
            history.append(self._protocol.material_change_error())
            return None
        if action == "not_applicable":
            self.last_trace = trace
            if receipts:
                return GMToolAgentOutcome(
                    handled=True,
                    reply=self._receipt_policy.authoritative_reply(receipts),
                    receipts=receipts,
                    trace=trace,
                )
            if must_decide:
                history.append(
                    {
                        "protocol_error": {
                            "error_code": "AGENT_MUST_OWN_ACTIVE_MESSAGE",
                            "message": "活跃跑团消息或明确发给时悠的消息不能交给旧语义栈重新判断。",
                            "correction_hint": "在silent、external、final、ask_user或call_tool中重新选择。",
                            "retryable": True,
                        }
                    }
                )
                return None
            return GMToolAgentOutcome(handled=False, trace=trace)
        if action in {"silent", "external"}:
            return self._handle_silent_or_external_action(
                action=action,
                decision=decision,
                context=context,
                current_message=current_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                must_reply_on_failure=must_reply_on_failure,
                is_system_beat=is_system_beat,
            )
        return self._handle_reply_action(
            action=action,
            decision=decision,
            context=context,
            current_message=current_message,
            recent_context=recent_context,
            observed_state=observed_state,
            receipts=receipts,
            history=history,
            trace=trace,
            step=step,
            deadline=deadline,
            must_reply_on_failure=must_reply_on_failure,
            is_system_beat=is_system_beat,
        )

    def _handle_silent_or_external_action(
        self,
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        audience = str(decision.get("audience") or "").strip().lower()
        silent_commit_allowed = (
            action == "silent"
            and self._mutations_can_commit_silently(
                receipts,
                context=context,
            )
        )
        if not is_system_beat and not receipts and not audience:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "MESSAGE_AUDIENCE_REQUIRED_FOR_SILENCE",
                        "message": "静默或转交前必须先判断当前消息的实际受众。",
                        "correction_hint": (
                            "结合current_turn与recent_messages解析称呼、代词和省略主语，"
                            "在audience中填写gm、players、table或external后重新决策。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return None
        if not is_system_beat and audience == "gm" and not silent_commit_allowed:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "SEMANTICALLY_ADDRESSED_MESSAGE_REQUIRES_REPLY",
                        "message": "你已判断这句话的受众是时悠，因此不能静默或转交。",
                        "correction_hint": "改用final、ask_user或合适的工具，并自然回应当前说话者。",
                        "retryable": True,
                    }
                }
            )
            return None
        if (
            not is_system_beat
            and self._context_requires_reply(context)
            and not silent_commit_allowed
        ):
            history.append(
                {
                    "protocol_error": {
                        "error_code": "ADDRESSED_MESSAGE_REQUIRES_REPLY",
                        "message": "这条消息由平台确认正在直接对时悠说话，不能静默或转交。",
                        "correction_hint": "改用final、ask_user或call_tool。",
                        "retryable": True,
                    }
                }
            )
            return None
        if not self._terminal_message_kind_is_valid(
            decision=decision,
            receipts=receipts,
            history=history,
            step=step,
            is_system_beat=is_system_beat,
        ):
            return None
        if (
            not is_system_beat
            and any(receipt.ok and receipt.state_changed for receipt in receipts)
            and not silent_commit_allowed
        ):
            history.append(
                {
                    "protocol_error": {
                        "error_code": "PLAYER_STATE_CHANGE_REQUIRES_ACKNOWLEDGEMENT",
                        "message": "当前玩家请求已成功改变权威状态，不能无声结束。",
                        "correction_hint": self._STATE_CHANGE_ACKNOWLEDGEMENT_HINT,
                        "retryable": True,
                    }
                }
            )
            return None
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if locked_reply:
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply=locked_reply,
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="成功工具回执要求公开发送锁定回复。",
            )
        if action == "external" and receipts:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "STATE_CHANGE_CANNOT_ROUTE_EXTERNAL",
                        "message": "已经调用过FU-GM工具，不能再把本句转交外部机器人。",
                        "correction_hint": "根据工具回执final，或在无需公开回复时silent。",
                        "retryable": True,
                    }
                }
            )
            return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            receipts=receipts,
            trace=trace,
            target="silent" if action == "silent" else "astrbot",
            mode="gm_agent_silent" if action == "silent" else "gm_agent_external",
            stop_astrbot=action == "silent",
            reason=str(decision.get("reason") or "").strip(),
            terminal_action=action,
        )

    def _silence_responsibility_requires_reply(
        self,
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        is_system_beat: bool,
    ) -> bool:
        """让独立语义复核器阻止核心模型误吞真实主持请求。"""

        if action == "silent" and not is_system_beat and not receipts:
            semantics = self._parsed_frozen_message_semantics(context)
            unresolved = [
                event
                for event in (semantics.events if semantics is not None else ())
                if event.action_commitment == "committed"
                and not (
                    event.dialogue_act == "roleplay_speech"
                    and event.response_expectation == "none"
                    and not event.state_intents
                )
            ]
            if unresolved:
                event_ids = [event.event_id for event in unresolved]
                step["silence_responsibility"] = {
                    "requires_gm_reply": True,
                    "category": "unresolved_committed_action",
                    "reason": "冻结语义中仍有正式行动尚未产生成功工具回执。",
                    "event_ids": event_ids,
                    "model_call_skipped": True,
                }
                history.append(
                    {
                        "protocol_error": {
                            "error_code": "COMMITTED_ACTION_UNRESOLVED",
                            "message": "已冻结的正式角色行动尚未结算，不能静默结束。",
                            "correction_hint": (
                                "保持message_semantics不变；为对应source_event_id调用忠实表达该行动的工具。"
                                "若权威状态缺少无法自行决定的必要参数，再向玩家提出一个具体问题。"
                            ),
                            "event_ids": event_ids,
                            "retryable": True,
                        }
                    }
                )
                return True

        verifier = self.reply_grounding_verifier
        if (
            action != "silent"
            or is_system_beat
            or receipts
            or verifier is None
            or self._context_requires_reply(context)
        ):
            return False
        if self._frozen_semantics_prove_player_only_discussion(context):
            step["silence_responsibility"] = {
                "requires_gm_reply": False,
                "category": "player_discussion",
                "reason": (
                    "首轮冻结语义已逐条确认消息只指向其他玩家或全桌，"
                    "且没有提交角色行动或主持事项。"
                ),
                "model_call_skipped": True,
            }
            decision.update(
                {
                    "message_kind": "discussion",
                    "audience": "players",
                }
            )
            return False
        try:
            review = verifier.verify_silence_responsibility(
                current_message=current_message,
                recent_context=recent_context,
                gate_status=context.gate_status,
                proposed_message_kind=str(decision.get("message_kind") or ""),
                proposed_audience=str(decision.get("audience") or ""),
                decision_reason=str(decision.get("reason") or ""),
                deadline=deadline,
                proposed_delivery=(
                    decision.get("delivery")
                    if isinstance(decision.get("delivery"), dict)
                    else {}
                ),
                has_independent_followup=(
                    decision.get("has_independent_followup") is True
                ),
            )
        except Exception as exc:
            trace.append(
                {
                    "phase": "silence_responsibility_review_failed",
                    "error": str(exc)[:300],
                }
            )
            return False
        step["silence_responsibility"] = {
            "requires_gm_reply": review.requires_gm_reply,
            "category": review.category,
            "reason": review.reason,
        }
        if not review.requires_gm_reply:
            normalized = self._verified_silent_semantics(review.category)
            if normalized is not None:
                original = {
                    "message_kind": str(
                        decision.get("message_kind") or ""
                    ).strip(),
                    "audience": str(decision.get("audience") or "").strip(),
                }
                decision.update(normalized)
                step["semantic_normalization"] = {
                    "source": "independent_silence_review",
                    "original": original,
                    "effective": dict(normalized),
                }
            return False
        context.metadata["_semantic_gm_addressed"] = True
        context.metadata["_semantic_gm_addressed_reviewed"] = True
        history.append(
            {
                "protocol_error": {
                    "error_code": "SILENCE_REVIEW_REQUIRES_GM_REPLY",
                    "message": "独立语义复核确认当前消息包含必须由主持人回应的事项。",
                    "correction_hint": (
                        review.reason
                        or "重新结合current_message与recent_messages回答该主持事项；不要创造新事实。"
                    ),
                    "category": review.category,
                    "retryable": True,
                }
            }
        )
        return True

    def _frozen_semantics_prove_player_only_discussion(
        self,
        context: GMToolExecutionContext,
    ) -> bool:
        """Trust only frozen, model-authored player-to-player semantics.

        This is not a second text classifier. The core model has already
        interpreted every buffered transport event, and Python merely checks
        that the immutable result contains no committed, NPC-facing, or
        GM-facing event. Ambiguous cases still use the independent reviewer.
        """

        semantics = self._parsed_frozen_message_semantics(context)
        if semantics is None or not semantics.events:
            return False
        safe_dialogue_acts = {
            "discussion",
            "question",
            "proposal",
            "agreement",
            "disagreement",
            "acknowledgement",
        }
        return all(
            event.relation in {"player", "table"}
            and event.dialogue_act in safe_dialogue_acts
            and event.action_commitment in {"none", "tentative"}
            for event in semantics.events
        )

    def _parsed_frozen_message_semantics(
        self,
        context: GMToolExecutionContext,
    ) -> GMMessageSemantics | None:
        raw_semantics = context.metadata.get(self._MESSAGE_SEMANTICS_METADATA_KEY)
        source_events = [
            dict(item)
            for item in list(context.metadata.get("current_turn_events") or [])
            if isinstance(item, dict)
        ]
        if not isinstance(raw_semantics, dict) or not source_events:
            return None
        try:
            return GMMessageSemantics.parse(
                raw_semantics,
                source_events=source_events,
            )
        except GMMessageSemanticsError:
            return None

    def _apply_semantic_capability_plan(
        self,
        *,
        context: GMToolExecutionContext,
        step: dict[str, object],
    ) -> None:
        """Expose the narrow CRUD family selected by frozen LLM semantics.

        The first decision still receives the conservative phase hot set. Once
        the model has interpreted every transport event, retries and compound
        follow-ups can use a much smaller, semantically justified working set.
        This never grants execution authority beyond the phase policy or live
        registry; normal admission and receipt validation remain mandatory.
        """

        semantics = self._parsed_frozen_message_semantics(context)
        if semantics is None:
            return
        phase_tools = set(
            self._capability_policy.phase_tool_names(self.registry, context)
            or set()
        )
        planned = set(semantic_change_tool_names(semantics))
        effective = planned & phase_tools & set(self.registry._tools)
        if not effective:
            return
        GMCapabilityBroker.grant(context, effective)
        context.metadata["gm_semantic_tool_names"] = sorted(effective)
        step["semantic_capability_plan"] = {
            "tool_names": sorted(effective),
            "source": "frozen_message_semantics",
        }

    @staticmethod
    def _verified_silent_semantics(
        category: str,
    ) -> dict[str, object] | None:
        """Translate a completed semantic review into terminal route fields.

        These categories describe messages that can end without a GM reply.
        The mapping is protocol vocabulary rather than textual intent
        detection; the LLM reviewer has already interpreted the conversation.
        """

        normalized = {
            "player_discussion": {
                "message_kind": "discussion",
                "audience": "players",
            },
            "idle": {
                "message_kind": "idle",
                "audience": "table",
            },
            "external": {
                "message_kind": "external",
                "audience": "external",
            },
        }
        value = normalized.get(str(category or "").strip().lower())
        return dict(value) if value is not None else None

    def _no_tool_reply_requires_more_work(
        self,
        *,
        action: str,
        decision: dict[str, object],
        outcome: GMToolAgentOutcome,
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        is_system_beat: bool,
    ) -> bool:
        """Reject a direct GM reply that only promises work for later.

        Tool-backed replies have receipt obligations below.  A direct reply had
        no equivalent guard, so a model could correctly understand "查询缺项"
        yet finish with only "我来看看".  Review only direct GM requests to
        avoid adding a semantic call to ordinary player-to-player discussion.
        """

        if (
            is_system_beat
            or receipts
            or action not in {"final", "ask_user"}
            or outcome.target != "fu_gm"
            or not str(outcome.reply or "").strip()
        ):
            return False
        message_kind = str(decision.get("message_kind") or "").strip().lower()
        audience = str(decision.get("audience") or "").strip().lower()
        if not (
            message_kind in {"gm_request", "mixed"}
            and (
                audience == "gm"
                or self._context_requires_reply(context)
                or context.metadata.get("_semantic_gm_addressed") is True
            )
        ):
            return False
        request_fulfilled: bool | None = None
        category = ""
        reason = ""
        grounding = step.get("reply_grounding")
        if isinstance(grounding, dict) and isinstance(
            grounding.get("request_fulfilled"),
            bool,
        ):
            request_fulfilled = bool(grounding["request_fulfilled"])
            category = str(grounding.get("category") or "")
            reason = str(grounding.get("completion_reason") or "")
            step["no_tool_completion_source"] = "reply_grounding"
        else:
            verifier = self.reply_grounding_verifier
            if verifier is None or not hasattr(
                verifier,
                "verify_silence_responsibility",
            ):
                return False
            try:
                review = verifier.verify_silence_responsibility(
                    current_message=current_message,
                    recent_context=recent_context,
                    gate_status=context.gate_status,
                    proposed_message_kind=message_kind,
                    proposed_audience=audience,
                    decision_reason=str(decision.get("reason") or ""),
                    deadline=deadline,
                    proposed_delivery=(
                        decision.get("delivery")
                        if isinstance(decision.get("delivery"), dict)
                        else {}
                    ),
                    has_independent_followup=(
                        decision.get("has_independent_followup") is True
                    ),
                    proposed_public_reply=str(outcome.reply or "").strip(),
                )
            except Exception as exc:
                # A reviewer outage must not swallow a valid direct answer.
                trace.append(
                    {
                        "phase": "no_tool_completion_review_failed",
                        "error": str(exc)[:300],
                    }
                )
                return False
            request_fulfilled = not review.requires_gm_reply
            category = review.category
            reason = review.reason
        step["no_tool_completion_review"] = {
            "request_fulfilled": request_fulfilled,
            "category": category,
            "reason": reason,
        }
        if request_fulfilled:
            return False
        context.metadata["_semantic_gm_addressed"] = True
        history.append(
            {
                "protocol_error": {
                    "error_code": "DIRECT_REPLY_LEFT_REQUEST_UNHANDLED",
                    "message": (
                        "当前公开回复只承诺稍后处理，尚未在本次请求中完成玩家交给GM的事项。"
                    ),
                    "correction_hint": (
                        reason
                        or "立即调用所需的权威查询或管理工具，并在同一轮给出实际结果。"
                    ),
                    "category": category,
                    "retryable": True,
                }
            }
        )
        return True

    def _post_tool_outcome_requires_more_work(
        self,
        *,
        action: str,
        decision: dict[str, object],
        outcome: GMToolAgentOutcome,
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        is_system_beat: bool,
    ) -> bool:
        """Prevent a partial tool batch from ending the whole message.

        Tool receipts prove only the operations they contain.  A model may
        correctly persist one clause while overlooking a question or a task
        delegated to the GM in another clause.  A superficial public promise
        can hide the same omission as silence, so review both terminal forms
        after the receipts exist.
        """

        if is_system_beat or outcome.target not in {"silent", "fu_gm"}:
            return False
        if outcome.mode == "gm_agent_rule_rejected":
            # A non-retryable domain rejection already answered the request.
            # Reviewing it as an unfinished mutation would replace the useful
            # permission/precondition explanation with a generic rollback.
            context.metadata.pop(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                None,
            )
            step["post_tool_completion_skipped"] = "authoritative_rejection"
            return False
        required_followups = self._receipt_policy.required_followup_tools(receipts)
        if required_followups:
            # A public acknowledgement is not a substitute for a mandatory
            # continuation signed by the successful tool receipt.  Without
            # this guard a batch-level final reply can escape before the next
            # iteration reaches ``_enforce_receipt_followup``; the outer
            # transaction then has no choice but to roll every valid write
            # back as incomplete.
            history.append(
                {
                    "protocol_error": {
                        "error_code": "REQUIRED_FOLLOWUP_PENDING",
                        "message": (
                            "成功回执仍要求完成后续工具，当前事务尚未结束。"
                        ),
                        "correction_hint": (
                            "下一轮只调用以下工具之一："
                            + "、".join(sorted(required_followups))
                            + "；完成前不得公开确认、静默或结束事务。"
                        ),
                        "required_followup_tools": sorted(required_followups),
                        "retryable": True,
                    }
                }
            )
            step["post_tool_required_followup_pending"] = sorted(
                required_followups
            )
            return True
        pending = context.metadata.get(
            self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
        )
        if self._locked_session_zero_proposal_is_publicly_complete(
            outcome=outcome,
            receipts=receipts,
        ):
            # The proposal tool receives model-authored, player-facing summary
            # text together with the concrete pending operations.  Once that
            # exact summary is locked as the public reply, another semantic
            # round-trip cannot add authority and only risks timing out after
            # the valid proposal has already been prepared.
            context.metadata.pop(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                None,
            )
            step["post_tool_completion_skipped"] = (
                "locked_session_zero_proposal_summary"
            )
            return False
        if isinstance(pending, dict) and pending and outcome.target == "silent":
            history.append(
                {
                    "protocol_error": {
                        **pending,
                        "retryable": True,
                    }
                }
            )
            step["post_tool_completion_obligation_pending"] = True
            return True
        if (
            not isinstance(pending, dict)
            and outcome.target == "fu_gm"
            and not self._should_review_post_tool_public_reply(
                decision=decision,
                context=context,
            )
        ):
            return False
        completed_receipts = [
            receipt
            for receipt in receipts
            if receipt.ok
        ]
        if (
            not isinstance(pending, dict)
            and outcome.target == "silent"
            and self._frozen_semantics_and_receipts_prove_complete_statement(
                decision=decision,
                context=context,
                completed_receipts=completed_receipts,
            )
        ):
            context.metadata.pop(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                None,
            )
            step["post_tool_completion_skipped"] = (
                "frozen_semantics_and_source_receipts"
            )
            step["post_tool_completion_model_call_skipped"] = True
            return False
        verifier = self.reply_grounding_verifier
        if not completed_receipts or verifier is None or not hasattr(
            verifier,
            "verify_silence_responsibility",
        ):
            return False
        try:
            review = verifier.verify_silence_responsibility(
                current_message=current_message,
                recent_context=recent_context,
                gate_status=context.gate_status,
                proposed_message_kind=str(decision.get("message_kind") or ""),
                proposed_audience=str(decision.get("audience") or ""),
                decision_reason=str(decision.get("reason") or ""),
                deadline=deadline,
                proposed_delivery=(
                    decision.get("delivery")
                    if isinstance(decision.get("delivery"), dict)
                    else {}
                ),
                has_independent_followup=(
                    decision.get("has_independent_followup") is True
                ),
                completed_receipts=completed_receipts,
                proposed_public_reply=(
                    str(outcome.reply or "").strip()
                    if outcome.target == "fu_gm"
                    else ""
                ),
            )
        except Exception as exc:
            trace.append(
                {
                    "phase": "post_tool_completion_review_failed",
                    "error": str(exc)[:300],
                }
            )
            if isinstance(pending, dict) and pending:
                history.append(
                    {"protocol_error": {**pending, "retryable": True}}
                )
                return True
            return False
        step["post_tool_completion_review"] = {
            "requires_gm_reply": review.requires_gm_reply,
            "category": review.category,
            "reason": review.reason,
            "completed_tool_count": len(completed_receipts),
            "proposed_public_reply": outcome.target == "fu_gm",
        }
        if not review.requires_gm_reply:
            context.metadata.pop(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY,
                None,
            )
            return False
        receipt_cursor = (
            int(pending.get("receipt_cursor_at_detection") or 0)
            if isinstance(pending, dict)
            else len(receipts)
        )
        has_proposal_receipt = any(
            receipt.ok
            and receipt.state_changed
            and receipt.tool_name == "propose_session_zero_update"
            for receipt in receipts
        )
        obligation = {
            "error_code": (
                "POST_TOOL_SILENCE_LEFT_REQUEST_UNHANDLED"
                if outcome.target == "silent"
                else "POST_TOOL_REPLY_LEFT_REQUEST_UNHANDLED"
            ),
            "message": (
                "工具回执与拟公开回复仍未完整履行玩家消息中的主持事项。"
            ),
            "correction_hint": (
                review.reason
                or "继续完成原句中尚未履行的主持请求；需要创作公开设定时先保存为待确认提案，再把提案内容告诉玩家。"
            ),
            "category": review.category,
            "requires_followup_tool": bool(
                review.category == "delegated_gm_task"
                and context.gate_status == "session_zero"
                and not has_proposal_receipt
            ),
            "required_followup_tool": (
                "propose_session_zero_update"
                if review.category == "delegated_gm_task"
                and context.gate_status == "session_zero"
                and not has_proposal_receipt
                else ""
            ),
            "receipt_cursor_at_detection": receipt_cursor,
        }
        context.metadata[
            self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
        ] = obligation
        context.metadata["_semantic_gm_addressed"] = True
        history.append({"protocol_error": {**obligation, "retryable": True}})
        return True

    def _frozen_semantics_and_receipts_prove_complete_statement(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        completed_receipts: list[GMToolReceipt],
    ) -> bool:
        """Skip one review only when semantics and receipts prove completion.

        The fast path is intentionally narrow: it accepts only a completed
        Session 0 state contribution whose public source statement is already
        visible. Questions, delegated creativity, corrections, actions,
        mixed/unclear relations, read-before-write workflows, and receipts that
        do not identify their source continue through the semantic verifier.
        """

        if context.gate_status != "session_zero":
            return False
        if str(decision.get("message_kind") or "").strip().lower() != (
            "state_contribution"
        ):
            return False
        if decision.get("has_independent_followup") is True:
            return False
        if not completed_receipts or any(
            not receipt.state_changed for receipt in completed_receipts
        ):
            return False
        if any(
            receipt.result.get("silent_commit_allowed") is not True
            or receipt.result.get("source_message_already_public") is not True
            for receipt in completed_receipts
        ):
            return False

        semantics = self._parsed_frozen_message_semantics(context)
        if semantics is None or not semantics.events:
            return False
        safe_dialogue_acts = {
            "answer",
            "agreement",
            "acknowledgement",
            "state_contribution",
        }
        if any(
            event.relation in {"npc", "mixed", "unclear"}
            or event.dialogue_act not in safe_dialogue_acts
            or event.action_commitment not in {"none", "answer"}
            for event in semantics.events
        ):
            return False

        covered_event_ids: set[str] = set()
        full_statement_receipts = 0
        for receipt in completed_receipts:
            source_event = receipt.result.get("source_event")
            if isinstance(source_event, dict):
                event_id = str(source_event.get("event_id") or "").strip()
                if event_id:
                    covered_event_ids.add(event_id)
            if receipt.result.get("completion_scope") == "source_statement":
                full_statement_receipts += 1
        semantic_event_ids = {event.event_id for event in semantics.events}
        if semantic_event_ids.issubset(covered_event_ids):
            return True
        return len(semantics.events) == 1 and full_statement_receipts > 0

    @staticmethod
    def _locked_session_zero_proposal_is_publicly_complete(
        *,
        outcome: GMToolAgentOutcome,
        receipts: list[GMToolReceipt],
    ) -> bool:
        public_reply = str(outcome.reply or "").strip()
        if outcome.target != "fu_gm" or not public_reply:
            return False
        for receipt in reversed(receipts):
            if not (
                receipt.ok
                and receipt.state_changed
                and receipt.lock_public_reply
                and receipt.tool_name == "propose_session_zero_update"
            ):
                continue
            proposal = receipt.result.get("proposal")
            summary = (
                str(proposal.get("summary") or "").strip()
                if isinstance(proposal, dict)
                else ""
            )
            return bool(
                summary
                and "".join(public_reply.split()) == "".join(summary.split())
            )
        return False

    def _should_review_post_tool_public_reply(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
    ) -> bool:
        """Review replies that can prematurely hide unfinished player intent.

        Session 0 keeps its existing addressed-request rule.  During an
        adventure, a successful NPC response must not terminate a compound
        message when the frozen interpretation also contains a committed
        state-bearing character action.  The independent reviewer compares
        the source message, receipts and proposed reply, avoiding a lexical
        action parser in Python.
        """

        if context.gate_status == "session_zero":
            message_kind = str(decision.get("message_kind") or "").strip().lower()
            audience = str(decision.get("audience") or "").strip().lower()
            return bool(
                context.metadata.get("_semantic_gm_addressed")
                or (audience == "gm" and message_kind in {"gm_request", "mixed"})
            )
        if context.gate_status != "adventure":
            return False
        semantics = self._parsed_frozen_message_semantics(context)
        if semantics is None:
            return False
        return any(
            event.action_commitment == "committed"
            and event.dialogue_act in {"action_declaration", "roleplay_speech"}
            and bool(event.state_intents)
            for event in semantics.events
        )

    def _review_reply_obligation_after_provider_failure(
        self,
        *,
        current_message: str,
        recent_context: str,
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        error: str,
        must_reply: bool,
        is_system_beat: bool,
    ) -> bool:
        """在大型主持请求失败后，用小上下文确认是否可以静默。

        普通群消息原本需要先由核心模型区分玩家讨论与真实行动。如果核心
        请求在作出这项判断前就失败，直接静默会让行动看起来像被主持人无视。
        这里复用独立静默职责审计器，只发送当前消息与近期公开聊天；若连这次
        小型复核也不可用，则前台玩家消息宁可明确报告未结算，也不能无声丢失。
        """

        if must_reply or is_system_beat or receipts:
            return must_reply
        verifier = self.reply_grounding_verifier
        if verifier is None:
            return must_reply
        review_deadline = time.monotonic() + min(
            6.0,
            max(2.0, self.timeout_seconds * 0.2),
        )
        try:
            review = verifier.verify_silence_responsibility(
                current_message=current_message,
                recent_context=recent_context,
                gate_status=context.gate_status,
                proposed_message_kind="unknown_provider_failure",
                proposed_audience="table",
                decision_reason=(
                    "核心主持请求在完成消息分类前失败；仅复核该消息是否包含"
                    "必须由主持人处理的行动、问题或裁定。"
                ),
                deadline=review_deadline,
                proposed_delivery={},
                has_independent_followup=False,
            )
        except Exception as review_error:
            trace.append(
                {
                    "phase": "provider_failure_reply_obligation_review_failed",
                    "provider_error": str(error or "")[:300],
                    "review_error": str(review_error)[:300],
                    "fallback": "report_unsettled_foreground_message",
                }
            )
            return context.gate_status in {
                "pre_session",
                "session_zero",
                "adventure",
                "paused",
            }
        trace.append(
            {
                "phase": "provider_failure_reply_obligation_review",
                "requires_gm_reply": review.requires_gm_reply,
                "category": review.category,
                "reason": review.reason,
            }
        )
        return bool(review.requires_gm_reply)

    @staticmethod
    def _reject_npc_turn_driven_by_player_discussion(
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
    ) -> bool:
        """阻止把纯玩家讨论误当成NPC回合的时间片。

        消息语义仍由模型判断；这里仅执行调度契约。玩家明确行动后的
        工具回执可以要求紧接NPC回合，而没有回执的discussion只能静默，
        由独立的系统主动节拍推进敌方。
        """

        if (
            is_system_beat
            or receipts
            or context.directly_addressed
            or str(decision.get("message_kind") or "").strip().lower()
            != "discussion"
            or action not in {"call_tool", "call_tools"}
        ):
            return False
        tool_names: list[str] = []
        if action == "call_tool":
            tool_names.append(str(decision.get("tool_name") or "").strip())
        else:
            tool_names.extend(
                str(call.get("tool_name") or "").strip()
                for call in list(decision.get("calls") or [])
                if isinstance(call, dict)
            )
        if "run_current_npc_turn" not in tool_names:
            return False
        history.append(
            {
                "protocol_error": {
                    "error_code": "PLAYER_DISCUSSION_CANNOT_DRIVE_NPC_TURN",
                    "message": (
                        "当前消息已被判定为玩家之间的discussion，不能借它执行NPC回合。"
                    ),
                    "correction_hint": (
                        "本条玩家消息选择silent，不增加world_response；"
                        "NPC回合由system_gm_beat_request主动触发。"
                    ),
                    "retryable": True,
                }
            }
        )
        step["protocol_error"] = "PLAYER_DISCUSSION_CANNOT_DRIVE_NPC_TURN"
        return True

    @staticmethod
    def _terminal_message_kind_is_valid(
        *,
        decision: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
    ) -> bool:
        """Keep the core agent's semantic classification internally coherent."""

        if is_system_beat or receipts:
            return True
        message_kind = str(decision.get("message_kind") or "").strip().lower()
        allowed_kinds = {
            "discussion",
            "performed_action",
            "npc_or_world_interaction",
            "gm_request",
            "state_contribution",
            "idle",
            "external",
            "mixed",
        }
        if message_kind not in allowed_kinds:
            error_code = "MESSAGE_KIND_REQUIRED_FOR_TERMINAL_ROUTE"
            message = (
                "静默或转交前必须先明确区分玩家间讨论、已执行行动、"
                "NPC/世界互动、GM请求、设定贡献、闲聊或外部消息。"
            )
            correction_hint = (
                "结合current_turn与recent_messages填写message_kind，"
                "再重新选择是否静默；不要只根据audience判断。"
            )
        elif message_kind in {
            "performed_action",
            "npc_or_world_interaction",
            "gm_request",
            "state_contribution",
            "mixed",
        }:
            error_code = "ACTIONABLE_MESSAGE_CANNOT_BE_SILENCED"
            message = "你已判定当前消息包含需要GM处理的内容，因此不能静默或转交。"
            correction_hint = (
                "只处理当前说话者已经做出或明确请求的部分，调用最具体工具；"
                "不得替其他玩家角色回应或行动。"
            )
        else:
            return True
        step["protocol_error"] = error_code
        history.append(
            {
                "protocol_error": {
                    "error_code": error_code,
                    "message": message,
                    "correction_hint": correction_hint,
                    "retryable": True,
                }
            }
        )
        return False

    def _unaddressed_table_reply_error(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        is_system_beat: bool,
    ) -> dict[str, object] | None:
        """Reject replies that intrude on ordinary player-to-player talk."""

        audience = str(decision.get("audience") or "").strip().lower()
        message_kind = str(decision.get("message_kind") or "").strip().lower()
        if (
            is_system_beat
            or receipts
            or self._context_requires_reply(context)
            or audience not in {"players", "table"}
            or message_kind not in {"discussion", "idle", "external"}
        ):
            return None
        return {
            "protocol_error": {
                "error_code": "UNADDRESSED_TABLE_TALK_SHOULD_STAY_SILENT",
                "message": (
                    "你已判断当前消息的受众是其他玩家或全桌，平台也未显示玩家在呼叫时悠；"
                    "不能用final或ask_user替队伍分工、回答玩家彼此的问题。"
                ),
                "correction_hint": (
                    "若这只是玩家间商量、征求同伴意见或闲聊，改用silent。"
                    "只有消息确实要求规则裁定、NPC/环境回应或其他明确主持职责时，"
                    "才重新说明该职责并选择相应工具或回复。"
                ),
                "retryable": True,
            }
        }

    def _independent_review_suppresses_unaddressed_reply(
        self,
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        """Stop an uncertain core reply from intruding on table discussion.

        The core model's frozen semantics remain authoritative for tool
        execution, but they are not enough to force a public message when the
        transport itself shows no direct address.  Reuse the independent
        conversational-responsibility reviewer only on this narrow mismatch:
        a non-private, unaddressed message for which the core wants to publish
        prose without any authoritative receipt.
        """

        verifier = self.reply_grounding_verifier
        if (
            action not in {"final", "ask_user"}
            or is_system_beat
            or receipts
            or context.directly_addressed
            or context.is_private
            or context.metadata.get("force_gm_reply")
            or not context.metadata.get("message_semantics_contract_required")
            or context.metadata.get("_semantic_gm_addressed_reviewed") is True
            or verifier is None
            or not hasattr(verifier, "verify_silence_responsibility")
        ):
            return None
        message_kind = str(decision.get("message_kind") or "").strip().lower()
        audience = str(decision.get("audience") or "").strip().lower()
        if message_kind not in {"gm_request", "mixed"} and audience not in {
            "gm",
            "mixed",
        }:
            return None
        transport_delivery = {
            "transport_directly_addressed": False,
            "transport_is_private": False,
        }
        try:
            review = verifier.verify_silence_responsibility(
                current_message=current_message,
                recent_context=recent_context,
                gate_status=context.gate_status,
                # This is an independent relation review. Feeding the core
                # model's disputed audience and reason back into the reviewer
                # would merely amplify the same classification error.
                proposed_message_kind="",
                proposed_audience="",
                decision_reason="",
                deadline=deadline,
                proposed_delivery=transport_delivery,
                has_independent_followup=(
                    decision.get("has_independent_followup") is True
                ),
                # Review the responsibility of the source message itself.
                # Supplying the proposed reply here would let an unnecessary
                # answer appear to have completed a request that never existed.
                proposed_public_reply="",
            )
        except Exception as exc:
            trace.append(
                {
                    "phase": "unaddressed_reply_responsibility_review_failed",
                    "error": str(exc)[:300],
                }
            )
            return None
        step["reply_responsibility"] = {
            "requires_gm_reply": review.requires_gm_reply,
            "category": review.category,
            "reason": review.reason,
            "transport_directly_addressed": False,
        }
        if review.requires_gm_reply or review.category not in {
            "player_discussion",
            "idle",
            "external",
        }:
            return None
        original = {
            "message_kind": message_kind,
            "audience": audience,
        }
        normalized = self._verified_silent_semantics(review.category) or {
            "message_kind": "discussion",
            "audience": "players",
        }
        decision.update(normalized)
        context.metadata.pop("_semantic_gm_addressed", None)
        step["semantic_normalization"] = {
            "source": "independent_reply_responsibility_review",
            "original": original,
            "effective": dict(normalized),
        }
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            receipts=receipts,
            trace=trace,
            target="silent",
            mode="gm_agent_silent",
            stop_astrbot=True,
            reason=review.reason or "独立语义复核确认这只是玩家间讨论。",
            terminal_action="silent",
        )

    def _handle_reply_action(
        self,
        *,
        action: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        if action == "ask_user" and not str(decision.get("reply") or "").strip():
            blocking_reply = self._blocking_window_wait_reply(receipts)
            if blocking_reply:
                decision = dict(decision)
                decision["reply"] = blocking_reply
                step["blocking_window_reply_fallback"] = True
        suppressed = self._independent_review_suppresses_unaddressed_reply(
            action=action,
            decision=decision,
            context=context,
            current_message=current_message,
            recent_context=recent_context,
            receipts=receipts,
            trace=trace,
            step=step,
            deadline=deadline,
            is_system_beat=is_system_beat,
        )
        if suppressed is not None:
            return suppressed
        table_reply_error = self._unaddressed_table_reply_error(
            decision=decision,
            context=context,
            receipts=receipts,
            is_system_beat=is_system_beat,
        )
        if table_reply_error is not None:
            history.append(table_reply_error)
            return None
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if (
            action == "final"
            and not locked_reply
            and not self._context_requires_reply(context)
            and not context.metadata.get(
                self._POST_TOOL_COMPLETION_OBLIGATION_METADATA_KEY
            )
            and self._mutations_can_commit_silently(
                receipts,
                context=context,
            )
        ):
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                target="silent",
                mode="gm_agent_silent_commit",
                stop_astrbot=True,
                reason=(
                    "玩家原消息已经完整公开了本地行动；"
                    "工具仅登记行动轮，无需由GM改写复述。"
                ),
                terminal_action="silent",
            )
        reply_parts = self._post_tool_reply_parts(
            decision=decision,
            receipts=receipts,
            current_message=current_message,
            history=history,
            step=step,
        )
        if reply_parts is None:
            return None
        resolution_reply, independent_reply = reply_parts
        reply, reply_parts = self._compose_post_tool_public_reply(
            action=action,
            decision=decision,
            receipts=receipts,
            resolution_reply=resolution_reply,
            independent_reply=independent_reply,
            step=step,
        )
        if not reply:
            self._record_terminal_reply_required(
                action=action,
                receipts=receipts,
                history=history,
                step=step,
            )
            return None
        if self._reject_internal_proposal_id_disclosure(
            reply,
            observed_state=observed_state,
            receipts=receipts,
            history=history,
            step=step,
        ):
            return None
        if not receipts and self._protocol.is_exact_player_echo(reply, current_message):
            if not is_system_beat and self._context_requires_reply(context):
                history.append(self._protocol.exact_echo_error())
                return None
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                target="silent",
                mode="gm_agent_silent",
                stop_astrbot=True,
                reason="模型只复述了玩家原话；该消息没有需要公开的GM回应。",
            )
        should_ground_reply = bool(
            context.gate_status == "adventure"
            or any(receipt.tool_name == "end_session" for receipt in receipts)
        )
        if not locked_reply and should_ground_reply:
            grounding_ok = self._reply_is_grounded(
                reply=reply,
                decision=decision,
                current_message=current_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                risk_tier=str(
                    context.metadata.get("_gm_transaction_risk_tier") or "observe"
                ),
            )
            if not grounding_ok:
                return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
            reply_parts=reply_parts,
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool" if receipts else "gm_agent_reply",
            reason=str(decision.get("reason") or "").strip(),
            terminal_action=action,
        )

    @staticmethod
    def _record_terminal_reply_required(
        *,
        action: str,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
    ) -> None:
        """Explain why a public terminal decision could not finish.

        Previously an empty ``final`` simply started another iteration. The
        model received no new information and could repeat the same terminal
        envelope until the loop limit. A failed write receipt is called out
        separately because public prose must not make that write sound
        successful.
        """

        last_receipt = receipts[-1] if receipts else None
        if last_receipt is not None and not last_receipt.ok:
            error_code = "FAILED_TOOL_REQUIRES_RECOVERY"
            message = (
                f"工具{last_receipt.tool_name}的最后回执失败，"
                "不能用空白或成功式final结束。"
            )
            correction_hint = (
                "若该工具仍是必要的，根据最后回执的error_code与"
                "correction_hint修正后重试；若本次主动节拍根本不应使用"
                "该工具，改用silent，不得宣称失败的改动已生效。"
            )
        else:
            error_code = "TERMINAL_REPLY_REQUIRED"
            message = f"{action}决策没有可发送的公开文本。"
            correction_hint = (
                "若需要对玩家说话，保留当前决策并填写非空reply；"
                "若此刻不应说话，明确改用silent。"
            )
        step["protocol_error"] = error_code
        history.append(
            {
                "protocol_error": {
                    "error_code": error_code,
                    "message": message,
                    "correction_hint": correction_hint,
                    "retryable": True,
                }
            }
        )

    @classmethod
    def _reject_internal_proposal_id_disclosure(
        cls,
        reply: str,
        *,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
    ) -> bool:
        exposed = cls._exposed_internal_proposal_ids(
            reply,
            observed_state=observed_state,
            receipts=receipts,
        )
        if not exposed:
            return False
        step["protocol_error"] = "INTERNAL_PROPOSAL_ID_EXPOSED"
        step["internal_proposal_id_exposed"] = len(exposed)
        history.append(
            {
                "protocol_error": {
                    "error_code": "INTERNAL_PROPOSAL_ID_EXPOSED",
                    "message": "公开回复泄露了只供工具调用的内部提案标识。",
                    "correction_hint": (
                        "重新组织回复，只用提案人、内容摘要或上一版/修订版等"
                        "自然称呼；不得向玩家展示任何proposal_id。"
                    ),
                    "retryable": True,
                }
            }
        )
        return True

    @staticmethod
    def _exposed_internal_proposal_ids(
        reply: str,
        *,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
    ) -> tuple[str, ...]:
        """Return known transaction-only proposal IDs copied into prose."""

        known: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key) == "pending_proposals" and isinstance(nested, list):
                        for proposal in nested:
                            if isinstance(proposal, dict):
                                visit_identifier_value(proposal.get("id"))
                    if str(key) in {
                        "proposal_id",
                        "superseded_proposal_ids",
                        "cleared_proposal_ids",
                    }:
                        visit_identifier_value(nested)
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        def visit_identifier_value(value: object) -> None:
            if isinstance(value, str):
                clean = value.strip()
                if clean:
                    known.add(clean)
            elif isinstance(value, list):
                for nested in value:
                    visit_identifier_value(nested)

        visit(observed_state)
        for receipt in receipts:
            visit(receipt.result)
        text = str(reply or "")
        return tuple(sorted(item for item in known if item and item in text))

    @staticmethod
    def _blocking_window_wait_reply(receipts: list[GMToolReceipt]) -> str:
        """模型漏写公开提示时，用权威待决窗口结束本轮等待。"""

        for receipt in reversed(receipts):
            if receipt.ok or receipt.error_code != "BLOCKING_DECISION_PENDING":
                continue
            raw_windows = receipt.result.get("pending_windows")
            if not isinstance(raw_windows, list):
                continue
            window = next((item for item in raw_windows if isinstance(item, dict)), None)
            if window is None:
                continue
            owner = str(window.get("owner") or "").strip()
            if not owner:
                responders = window.get("allowed_responders")
                if isinstance(responders, list) and responders:
                    owner = str(responders[0] or "").strip()
            owner = owner or "对应玩家"
            kind = str(window.get("kind") or "").strip()
            if kind == "critical_opportunity":
                return f"先等一下，{owner}的大成功机会还没决定怎么用；这一步先放到它之后。"
            if kind in {"check_roll_confirmation", "invoke_reroll", "failed_check_grace"}:
                return f"先等一下，{owner}的检定还在等确认；这一步先放到它之后。"
            return f"先等一下，{owner}还有一个选择没处理；这一步先放到它之后。"
        return ""

    @staticmethod
    def _combine_public_reply_parts(
        first_part: str,
        second_part: str,
    ) -> str:
        """Combine two non-empty public parts without duplicating either."""

        first = str(first_part or "").strip()
        second = str(second_part or "").strip()
        if first and first in second:
            second = second.replace(first, "", 1).strip()
        if second and second in first:
            first = first.replace(second, "", 1).strip()
        if not first:
            return second
        if not second:
            return first
        return f"{first}\n{second}"

    @staticmethod
    def _ordered_public_reply_parts(*parts: str) -> list[str]:
        """Return distinct non-empty chat messages in their delivery order."""

        ordered: list[str] = []
        for part in parts:
            text = str(part or "").strip()
            if not text or text in ordered:
                continue
            ordered.append(text)
        return ordered

    def _compose_post_tool_public_reply(
        self,
        *,
        action: str,
        decision: dict[str, object],
        receipts: list[GMToolReceipt],
        resolution_reply: str,
        independent_reply: str,
        step: dict[str, object],
    ) -> tuple[str, list[str]]:
        """Apply receipt obligations and choose one or more public messages."""

        natural_pending = self._receipt_policy.natural_resolution_pending(receipts)
        mixed_pending = self._receipt_policy.mixed_message_followup_pending(receipts)
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        reply = str(decision.get("reply") or "").strip()
        if action == "final" and receipts and not receipts[-1].ok:
            reply = ""
        public_parts: list[str] = []
        if natural_pending:
            if mixed_pending:
                public_parts = self._ordered_public_reply_parts(
                    independent_reply,
                    resolution_reply,
                )
                reply = self._combine_public_reply_parts(*public_parts)
            else:
                reply = resolution_reply
            step["natural_resolution_completed"] = True
            if mixed_pending:
                step["mixed_message_followup_completed"] = True
        elif mixed_pending:
            reply = self._combine_public_reply_parts(
                self._receipt_policy.receipt_fallback(receipts),
                independent_reply,
            )
            step["mixed_message_followup_completed"] = True
        elif locked_reply:
            reply = locked_reply
        if not reply:
            reply = self._receipt_policy.receipt_fallback(receipts)
        return reply, public_parts

    def _post_tool_reply_parts(
        self,
        *,
        decision: dict[str, object],
        receipts: list[GMToolReceipt],
        current_message: str,
        history: list[dict[str, object]],
        step: dict[str, object],
    ) -> tuple[str, str] | None:
        """Validate prose obligations created by successful rule receipts."""

        resolution_reply = str(decision.get("resolution_reply") or "").strip()
        independent_reply = str(decision.get("independent_reply") or "").strip()
        if self._receipt_policy.natural_resolution_pending(receipts):
            if not resolution_reply:
                fallback = self._natural_resolution_fallback(receipts)
                already_retried = self._history_has_protocol_error(
                    history,
                    "NATURAL_RESOLUTION_REPLY_REQUIRED",
                )
                if (
                    already_retried
                    and fallback
                    and not self._protocol.substantially_restates_player(
                        fallback,
                        current_message,
                    )
                ):
                    resolution_reply = fallback
                    step["natural_resolution_fallback_used"] = True
                else:
                    step["protocol_error"] = "NATURAL_RESOLUTION_REPLY_REQUIRED"
                    history.append(
                        {
                            "protocol_error": {
                                "error_code": "NATURAL_RESOLUTION_REPLY_REQUIRED",
                                "message": "规则事实已经提交，但还没有面向桌面的现场兑现。",
                                "correction_hint": (
                                    "在resolution_reply中用一至两句演出这个结果；"
                                    "不要照抄玩家原句，不要以规则标签开头。"
                                ),
                                "retryable": True,
                            }
                        }
                    )
                    return None
            if self._protocol.substantially_restates_player(
                resolution_reply,
                current_message,
            ):
                step["protocol_error"] = "RESOLUTION_REPLY_RESTATES_PLAYER"
                history.append(self._protocol.player_restatement_error())
                return None
        if (
            self._receipt_policy.mixed_message_followup_pending(receipts)
            and not independent_reply
        ):
            step["protocol_error"] = "INDEPENDENT_FOLLOWUP_REPLY_REQUIRED"
            history.append(
                {
                    "protocol_error": {
                        "error_code": "INDEPENDENT_FOLLOWUP_REPLY_REQUIRED",
                        "message": "规则选择已经成功结算，但玩家同一句中的独立问题仍未回答。",
                        "correction_hint": (
                            "读取current_message与current_state_summary，只在independent_reply中"
                            "回答尚未回答的独立问题；不要再次调用resolve_rule_window。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return None
        return resolution_reply, independent_reply

    @staticmethod
    def _history_has_protocol_error(
        history: list[dict[str, object]],
        error_code: str,
    ) -> bool:
        """判断模型是否已经收到过一次同类修复提示。"""

        expected = str(error_code or "").strip()
        return any(
            isinstance(item.get("protocol_error"), dict)
            and str(item["protocol_error"].get("error_code") or "").strip()
            == expected
            for item in history
        )

    @staticmethod
    def _natural_resolution_fallback(receipts: list[GMToolReceipt]) -> str:
        """复用已生成的现场文本，同时移除不应公开的规则标签。"""

        for receipt in reversed(receipts):
            if not (
                receipt.ok
                and receipt.state_changed
                and receipt.result.get("natural_resolution_pending") is True
            ):
                continue
            text = str(receipt.public_fallback_reply or "").strip()
            if text.startswith("机会【") and "】：" in text:
                text = text.split("】：", 1)[1].strip()
            return text
        return ""

    def _reply_is_grounded(
        self,
        *,
        reply: str,
        decision: dict[str, object],
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        risk_tier: str,
    ) -> bool:
        """Return unsupported prose to the same GM transaction before publish."""

        message_kind = str(decision.get("message_kind") or "").strip().lower()
        clean_risk = str(risk_tier or "observe").strip().lower()
        step["reply_grounding_risk_tier"] = clean_risk
        local_source = self._locally_proven_exact_reply(
            reply=reply,
            decision=decision,
            observed_state=observed_state,
            receipts=receipts,
            risk_tier=clean_risk,
        )
        if local_source:
            step["reply_grounding"] = {
                "valid": True,
                "category": "local_authoritative_exact",
                "source": local_source,
                "unsupported_claims": [],
            }
            return True
        if (
            clean_risk == "observe"
            and not receipts
            and message_kind in {"idle", "external"}
        ):
            # These categories contain no GM answer or world-state assertion by
            # contract.  The exact-echo guard has already run, so another model
            # call cannot add useful authority here.
            step["reply_grounding"] = {
                "valid": True,
                "category": "local_low_risk",
                "unsupported_claims": [],
            }
            return True
        verifier = self.reply_grounding_verifier
        if verifier is None:
            # Lightweight test/embedded compositions may deliberately omit
            # this production review stage.
            return True
        try:
            review = verifier.verify(
                current_message=current_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                proposed_reply=reply,
                message_kind=message_kind,
                decision_reason=str(decision.get("reason") or "").strip(),
                deadline=deadline,
            )
        except Exception as exc:
            trace.append(
                {
                    "phase": "reply_grounding_review_failed",
                    "error": str(exc)[:300],
                    "fallback_message_kind": message_kind,
                }
            )
            return message_kind not in {
                "performed_action",
                "npc_or_world_interaction",
                "state_contribution",
                "mixed",
            }
        step["reply_grounding"] = {
            "valid": review.valid,
            "category": review.category,
            "unsupported_claims": list(review.unsupported_claims),
            # Third-party and test verifiers created before the combined
            # completion review do not expose this optional field.
            "request_fulfilled": getattr(review, "request_fulfilled", None),
        }
        if review.valid:
            return True
        history.append(
            {
                "protocol_error": {
                    "error_code": "PUBLIC_REPLY_NOT_GROUNDED",
                    "message": (
                        "拟发布回复包含当前权威状态或成功工具回执尚未支持的外部结果。"
                    ),
                    "unsupported_claims": list(review.unsupported_claims),
                    "correction_hint": (
                        review.correction_hint
                        or (
                            "调用最具体的场景、NPC、移动、规则或冲突工具提交结果；"
                            "若玩家前提不成立，只澄清已知现状，不替NPC说话或制造新变化。"
                        )
                    ),
                    "retryable": True,
                }
            }
        )
        return False

    @classmethod
    def _locally_proven_exact_reply(
        cls,
        *,
        reply: str,
        decision: dict[str, object],
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        risk_tier: str,
    ) -> str:
        """Prove only byte-near canonical status acknowledgements locally.

        This is intentionally a two-result policy: exact proof or not
        applicable.  Anything with an extra clause, paraphrase, negation,
        failed receipt or mixed answer continues to the model verifier.
        """

        if (
            str(decision.get("decision") or "").strip().lower() != "final"
            or bool(str(decision.get("resolution_reply") or "").strip())
            or bool(str(decision.get("independent_reply") or "").strip())
        ):
            return ""
        message_kind = str(decision.get("message_kind") or "").strip().lower()
        receipt_source = cls._locally_proven_receipt_reply(
            reply=reply,
            receipts=receipts,
        )
        if receipt_source:
            return receipt_source
        if message_kind in {
            "performed_action",
            "npc_or_world_interaction",
            "state_contribution",
            "mixed",
        }:
            return ""
        if (
            str(risk_tier or "").strip().lower() != "observe"
            or receipts
        ):
            return ""
        claim_source = cls._locally_proven_state_claim_reply(
            reply=reply,
            decision=decision,
            observed_state=observed_state,
        )
        if claim_source:
            return claim_source
        session = dict(
            dict(observed_state.get("processes") or {}).get("session") or {}
        )
        gate_status = str(session.get("gate_status") or "").strip().lower()
        canonical = {
            "adventure": "第一章已经开始了",
            "session_zero": "现在还在第零章",
            "paused": "当前场次处于暂停状态",
        }.get(gate_status, "")
        if not canonical:
            return ""
        normalize = lambda value: "".join(str(value or "").split()).rstrip(
            "。！？!?"
        )
        return "gate_status" if normalize(reply) == normalize(canonical) else ""

    @classmethod
    def _locally_proven_receipt_reply(
        cls,
        *,
        reply: str,
        receipts: list[GMToolReceipt],
    ) -> str:
        """Accept only an exact public sentence signed by successful tools."""

        if not receipts or any(not receipt.ok for receipt in receipts):
            return ""
        candidates = {
            str(receipt.public_fallback_reply or "").strip()
            for receipt in receipts
            if str(receipt.public_fallback_reply or "").strip()
        }
        if len(candidates) != 1:
            return ""
        expected = next(iter(candidates))
        normalize = lambda value: "".join(str(value or "").split()).rstrip(
            "。！？!?"
        )
        return (
            "successful_receipt_public_reply"
            if normalize(reply) == normalize(expected)
            else ""
        )

    @classmethod
    def _locally_proven_state_claim_reply(
        cls,
        *,
        reply: str,
        decision: dict[str, object],
        observed_state: dict[str, object],
    ) -> str:
        """Verify one whitelisted state claim and its complete fixed wording."""

        raw_claims = decision.get("claims")
        if not isinstance(raw_claims, list) or len(raw_claims) != 1:
            return ""
        claim = raw_claims[0]
        if not isinstance(claim, dict):
            return ""
        if str(claim.get("type") or "").strip() != "state_reference":
            return ""
        path = str(claim.get("path") or "").strip()
        processes = dict(observed_state.get("processes") or {})
        session = dict(processes.get("session") or {})
        conflict = dict(processes.get("conflict") or {})
        scene = dict(observed_state.get("scene") or {})
        actual_by_path: dict[str, object] = {
            "processes.session.gate_status": session.get("gate_status"),
            "processes.session.ledger_active": session.get("ledger_active"),
            "scene.name": scene.get("name"),
            "scene.location": scene.get("location"),
            "processes.conflict.active": conflict.get("active"),
        }
        if path not in actual_by_path:
            return ""
        actual = actual_by_path[path]
        expected = claim.get("expected")
        if isinstance(actual, bool):
            if not isinstance(expected, bool) or expected is not actual:
                return ""
        elif str(expected or "").strip() != str(actual or "").strip():
            return ""
        canonical = cls._canonical_state_claim_reply(path, actual)
        if not canonical:
            return ""
        normalize = lambda value: "".join(str(value or "").split()).rstrip(
            "。！？!?"
        )
        return (
            f"state_reference:{path}"
            if normalize(reply) == normalize(canonical)
            else ""
        )

    @staticmethod
    def _canonical_state_claim_reply(path: str, value: object) -> str:
        if path == "processes.session.gate_status":
            return {
                "adventure": "第一章已经开始了。",
                "session_zero": "现在还在第零章。",
                "paused": "当前场次处于暂停状态。",
            }.get(str(value or "").strip().lower(), "")
        if path == "processes.session.ledger_active":
            return "当前场次正在进行中。" if value is True else (
                "当前没有正在进行的场次。" if value is False else ""
            )
        if path == "scene.name" and str(value or "").strip():
            return f"当前场景是【{str(value).strip()}】。"
        if path == "scene.location" and str(value or "").strip():
            return f"当前场景位于【{str(value).strip()}】。"
        if path == "processes.conflict.active":
            return "当前正在战斗中。" if value is True else (
                "当前没有正在进行的战斗。" if value is False else ""
            )
        return ""

    def _decision_risk_tier(
        self,
        decision: dict[str, object],
        *,
        fallback: str = "observe",
    ) -> str:
        action = str(decision.get("decision") or "").strip().lower()
        if action == "call_tool":
            tool_names = [str(decision.get("tool_name") or "").strip()]
        elif action == "call_tools":
            tool_names = [
                str(call.get("tool_name") or "").strip()
                for call in list(decision.get("calls") or [])
                if isinstance(call, dict)
            ]
        else:
            tool_names = []
        if any(name in self._RULES_RISK_TOOLS for name in tool_names):
            return "rules"
        if any(self.registry.side_effect(name) == "replace_state" for name in tool_names):
            return "rules"
        if any(
            self.registry.side_effect(name) not in {"", "read"}
            for name in tool_names
        ):
            return "commit"
        clean_fallback = str(fallback or "observe").strip().lower()
        return clean_fallback if clean_fallback in {"observe", "commit", "rules"} else "observe"

    @classmethod
    def _tool_requires_semantic_preflight(
        cls,
        tool_name: str,
        arguments: object,
    ) -> bool:
        clean_name = str(tool_name or "").strip()
        if clean_name in cls._SEMANTIC_PREFLIGHT_TOOLS:
            return True
        if clean_name != "resolve_rule_window" or not isinstance(arguments, dict):
            return False
        return str(arguments.get("action_type") or "").strip().lower() == "invoketrait"

    @staticmethod
    def _tool_proposal_authority_context(
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        """Expose only Python-issued scene authority to the semantic auditor."""

        metadata = context.metadata
        if not bool(metadata.get("system_gm_beat_request")):
            return {}
        keys = (
            "system_gm_beat_request",
            "heartbeat_action",
            "heartbeat_force",
            "gm_authored_scene_opening",
            "gm_authored_free_scene_beat",
            "heartbeat_require_material_change",
            "heartbeat_require_consequence",
            "heartbeat_require_local_change",
            "heartbeat_require_local_resolution",
            "heartbeat_require_session_resolution",
        )
        return {
            key: deepcopy(metadata[key])
            for key in keys
            if key in metadata
        }

    def _tool_proposals_are_grounded(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        proposal_source_review_required: bool = False,
    ) -> bool:
        """Fail closed on semantic writes before any handler can mutate state."""

        clear_semantic_tool_attestations(context)
        verifier = self.reply_grounding_verifier
        verify_proposal = getattr(verifier, "verify_tool_proposal", None)
        verify_proposals = getattr(verifier, "verify_tool_proposals", None)
        if verifier is None or not (
            callable(verify_proposal) or callable(verify_proposals)
        ):
            return True
        action = str(decision.get("decision") or "").strip().lower()
        if action == "call_tool":
            calls = [
                {
                    "tool_name": str(decision.get("tool_name") or "").strip(),
                    "arguments": decision.get("arguments"),
                }
            ]
        else:
            calls = [
                {
                    "tool_name": str(call.get("tool_name") or "").strip(),
                    "arguments": call.get("arguments"),
                }
                for call in list(decision.get("calls") or [])
                if isinstance(call, dict)
            ]
        batch_context = [
            {
                "tool_name": str(call.get("tool_name") or "").strip(),
                "arguments": call.get("arguments"),
            }
            for call in calls
        ]
        semantic_proposals: list[dict[str, object]] = []
        frozen_message_semantics = context.metadata.get(
            self._MESSAGE_SEMANTICS_METADATA_KEY
        )
        if not isinstance(frozen_message_semantics, dict):
            frozen_message_semantics = {}
        proposal_authority = self._tool_proposal_authority_context(context)
        for call in calls:
            tool_name = str(call.get("tool_name") or "").strip()
            arguments = call.get("arguments")
            requires_review = self._tool_requires_semantic_preflight(
                tool_name,
                arguments,
            ) or (
                proposal_source_review_required
                and tool_name == "propose_session_zero_update"
            )
            if not requires_review:
                continue
            proposal_message = current_message
            semantic_proposals.append(
                {
                    "current_message": proposal_message,
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
            )
        if not semantic_proposals:
            return True

        try:
            if callable(verify_proposals) and len(semantic_proposals) > 1:
                reviews = list(
                    verify_proposals(
                        recent_context=recent_context,
                        observed_state=observed_state,
                        proposals=semantic_proposals,
                        deadline=deadline,
                        batch_context=batch_context,
                        receipts=receipts,
                        frozen_message_semantics=frozen_message_semantics,
                        proposal_authority=proposal_authority,
                    )
                )
                if len(reviews) != len(semantic_proposals):
                    raise ValueError("批量语义审计没有返回与提案数量一致的结果。")
                step["tool_proposal_grounding_mode"] = "batch"
            else:
                if not callable(verify_proposal):
                    reviews = list(
                        verify_proposals(
                            recent_context=recent_context,
                            observed_state=observed_state,
                            proposals=semantic_proposals,
                            deadline=deadline,
                            batch_context=batch_context,
                            receipts=list(receipts),
                            frozen_message_semantics=frozen_message_semantics,
                            proposal_authority=proposal_authority,
                        )
                    )
                else:
                    reviews = [
                        verify_proposal(
                            current_message=str(
                                proposal.get("current_message") or ""
                            ),
                            recent_context=recent_context,
                            observed_state=observed_state,
                            tool_name=str(proposal.get("tool_name") or ""),
                            arguments=proposal.get("arguments"),
                            deadline=deadline,
                            batch_context=batch_context,
                            receipts=list(receipts),
                            frozen_message_semantics=frozen_message_semantics,
                            proposal_authority=proposal_authority,
                        )
                        for proposal in semantic_proposals
                    ]
                if len(reviews) != len(semantic_proposals):
                    raise ValueError("语义审计没有返回与提案数量一致的结果。")
                step["tool_proposal_grounding_mode"] = "individual"
        except Exception as exc:
            tool_names = [
                str(proposal.get("tool_name") or "")
                for proposal in semantic_proposals
            ]
            trace.append(
                {
                    "phase": "tool_proposal_grounding_review_failed",
                    "tool_names": tool_names,
                    "error": str(exc)[:300],
                }
            )
            step["protocol_error"] = "SEMANTIC_TOOL_PROPOSAL_REVIEW_FAILED"
            protocol_error = {
                "error_code": "SEMANTIC_TOOL_PROPOSAL_REVIEW_FAILED",
                "message": (
                    f"工具 {'、'.join(tool_names)} 的自由文本提案未能完成事实一致性审计，"
                    "因此没有执行，也没有修改状态。"
                ),
                "correction_hint": (
                    "保留玩家原意，重新提交更窄、更明确、只依赖当前消息与权威状态的提案；"
                    "不得用未经审计的文字绕过该工具。"
                ),
                "retryable": True,
            }
            history.append({"protocol_error": protocol_error})
            context.metadata[
                self._LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY
            ] = deepcopy(protocol_error)
            return False

        review_rows: list[dict[str, object]] = []
        for proposal, review in zip(semantic_proposals, reviews):
            tool_name = str(proposal.get("tool_name") or "").strip()
            row = {
                "tool_name": tool_name,
                "valid": review.valid,
                "category": review.category,
                "repair_mode": review.repair_mode,
                "unsupported_claims": list(review.unsupported_claims),
                "correction_hint": str(review.correction_hint or ""),
            }
            review_rows.append(row)
            if review.valid:
                continue
            step["protocol_error"] = "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
            if str(review.category or "").strip() in {
                "needs_player_clarification",
                "trait_rationale_unverified",
                "false_premise",
            }:
                step["tool_proposal_requires_clarification"] = True
            correction_hint = str(review.correction_hint or "").strip() or (
                "纠正玩家问题中的错误前提，或缩小到当前确实成立的事实；"
                "若缺少玩家本人选择或说明，先自然追问，不要替玩家补写。"
            )
            npc_repair_contract: dict[str, object] | None = None
            if tool_name == "decide_npc_response":
                rejection_count = self._semantic_tool_rejection_count(
                    history,
                    tool_name=tool_name,
                ) + 1
                repair_mode = str(review.repair_mode or "ordinary").strip()
                if (
                    repair_mode == "npc_fact_or_nonclaim"
                    or str(review.category or "").strip()
                    == "npc_knowledge_unsupported"
                ):
                    repair_mode = "npc_fact_or_nonclaim"
                npc_repair_contract = self._npc_response_repair_contract(
                    rejection_count=rejection_count,
                    repair_mode=repair_mode,
                    unsupported_claims=list(review.unsupported_claims),
                )
                correction_hint = (
                    correction_hint
                    + " 保留同一NPC与玩家本轮回应。若被拒绝的是NPC此前没有依据的新知识，"
                    "只有两条合法路径：其一，用fact_effects明确分类合理且不冲突的即兴内容为"
                    "objective、claim、rumor或lie；其二，删除该断言并改用"
                    "speech_act=admit_unknown、refuse、deflect或new_gate。若错误与NPC知识无关，"
                    "按前述具体提示修正，不要滥用fact_effects。不得只换一种说法再次提交同一断言。"
                )
                if rejection_count >= 2:
                    correction_hint += (
                        " 同类NPC提案已经连续被拒绝；若本轮不能提交完整且合规的fact_effects，"
                        "必须走不新增事实的回应路径，不得第三次改写被拒绝的情报。"
                    )
            protocol_error = {
                "error_code": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
                "tool_name": tool_name,
                "review_category": str(review.category or "").strip(),
                "repair_mode": str(review.repair_mode or "ordinary").strip(),
                "message": (
                    f"工具 {tool_name} 的拟议写入包含未被玩家原话、公开上下文或权威状态支持的内容；"
                    "工具没有执行，状态没有改变。"
                ),
                "unsupported_claims": list(review.unsupported_claims),
                "correction_hint": correction_hint,
                "retryable": True,
            }
            if npc_repair_contract is not None:
                protocol_error["npc_response_repair_contract"] = (
                    npc_repair_contract
                )
            history.append({"protocol_error": protocol_error})
            context.metadata[
                self._LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY
            ] = deepcopy(protocol_error)
            step["tool_proposal_grounding"] = review_rows
            return False
        step["tool_proposal_grounding"] = review_rows
        context.metadata.pop(
            self._LAST_SEMANTIC_PROTOCOL_ERROR_METADATA_KEY,
            None,
        )
        remember_semantic_tool_attestations(context, semantic_proposals)
        return True

    @staticmethod
    def _annotate_semantically_complete_proposals(
        receipts: list[GMToolReceipt],
        *,
        step: dict[str, object],
    ) -> None:
        """Carry a successful source-coverage review into receipt evidence.

        The structural integrity layer must not infer that category alternatives
        such as "地区、历史或威胁" are three independent proposals. The
        semantic preflight reviews the whole source message and proposed packet;
        this marker lets terminal validation require one complete persisted
        proposal without repeating that semantic judgment with regexes.
        """

        rows = step.get("tool_proposal_grounding")
        if not isinstance(rows, list):
            return
        proposal_reviewed = any(
            isinstance(row, dict)
            and row.get("valid") is True
            and str(row.get("tool_name") or "").strip()
            == "propose_session_zero_update"
            for row in rows
        )
        if not proposal_reviewed:
            return
        for receipt in receipts:
            if (
                receipt.ok
                and receipt.state_changed
                and receipt.tool_name == "propose_session_zero_update"
            ):
                receipt.result["semantic_source_complete"] = True

    def _handle_single_tool_action(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        ledger: GMToolCallLedger,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
        must_reply_on_failure: bool,
        mixed_message: bool = False,
    ) -> GMToolAgentOutcome | None:
        """Execute one validated tool proposal and apply receipt policy."""

        tool_name = str(decision.get("tool_name") or "").strip()
        call_event = ledger.execute(
            tool_name,
            decision.get("arguments"),
        )
        if call_event.protocol_error_code:
            step["protocol_error"] = call_event.protocol_error_code
            if call_event.abort_repeated_call_loop:
                self.last_trace = trace
                return GMToolAgentOutcome(
                    handled=True,
                    reply=self._receipt_policy.authoritative_reply(receipts),
                    receipts=receipts,
                    trace=trace,
                    reason="已阻止重复执行成功的写工具。",
                )
            return None
        receipt = call_event.receipt
        if receipt is None:
            return None
        self._mark_rule_resolution_followup(
            receipt,
            step=step,
            mixed_message=mixed_message,
        )
        self._mark_standalone_safety_confirmation(
            receipts,
            mixed_message=mixed_message,
        )
        step["receipt"] = receipt.to_dict()
        if call_event.abort_repeated_call_loop:
            return self._agent_output_retry_exhausted(
                receipt=receipt,
                receipts=receipts,
                trace=trace,
                must_reply_on_failure=(
                    must_reply_on_failure
                    or (ledger.mutating_call_attempted and not is_system_beat)
                ),
            )
        if receipt.error_code == "STALE_AGENT_REQUEST":
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                target="silent",
                mode="gm_agent_stale",
                stop_astrbot=True,
                reason="生成期间出现了新的桌面消息，已在写入前终止过期请求。",
            )
        receipt, signed_outcome = self._execute_python_signed_followups(
            source_receipt=receipt, context=context,
            ledger=ledger, receipts=receipts,
            trace=trace, step=step,
            is_system_beat=is_system_beat,
            must_reply_on_failure=must_reply_on_failure,
            mixed_message=mixed_message,
        )
        if signed_outcome is not None:
            return signed_outcome
        if self._receipt_policy.terminal_public_result_ready(receipt):
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply=self._receipt_policy.authoritative_reply(receipts),
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
                reason="只读权威工具已返回完整且锁定的公开结果，无需再次调用模型改写。",
            )
        if self._receipt_policy.heartbeat_public_change_committed(context, receipt):
            self.last_trace = trace
            return self._heartbeat_public_change_outcome(receipts, trace)
        public_outcome = self._terminal_public_change_outcome(receipt, receipts, trace)
        if public_outcome is not None:
            return public_outcome
        # A preparatory receipt may require one more authoritative tool call.
        # Never honor a model-proposed terminal_decision=silent while that
        # obligation is still pending; the next loop iteration will expose
        # only the permitted follow-up schemas and return a precise protocol
        # error if the model tries to finish early.
        if self._receipt_policy.required_followup_tools(receipts):
            return None
        terminal = str(decision.get("terminal_decision") or "").strip().lower()
        if not (receipt.ok and terminal == "silent"):
            return None
        silent_commit_allowed = self._mutations_can_commit_silently(
            receipts,
            context=context,
        )
        if not is_system_beat and receipt.state_changed and not silent_commit_allowed:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "PLAYER_STATE_CHANGE_REQUIRES_ACKNOWLEDGEMENT",
                        "message": "当前玩家请求已成功改变权威状态，不能无声结束。",
                        "correction_hint": self._STATE_CHANGE_ACKNOWLEDGEMENT_HINT,
                        "retryable": True,
                    }
                }
            )
            return None
        if (
            not is_system_beat
            and self._context_requires_reply(context)
            and not silent_commit_allowed
        ):
            history.append(
                {
                    "protocol_error": {
                        "error_code": "ADDRESSED_MESSAGE_REQUIRES_REPLY",
                        "message": "直接发给时悠的消息在工具成功后不能静默。",
                        "correction_hint": "根据成功回执给出一句自然回应。",
                        "retryable": True,
                    }
                }
            )
            return None
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if locked_reply:
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply=locked_reply,
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
            )
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            receipts=receipts,
            trace=trace,
            target="silent",
            mode="gm_agent_silent",
            stop_astrbot=True,
            reason=str(decision.get("reason") or "").strip(),
        )

    def _execute_python_signed_followups(
        self,
        *,
        source_receipt: GMToolReceipt,
        context: GMToolExecutionContext,
        ledger: GMToolCallLedger,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
        must_reply_on_failure: bool,
        mixed_message: bool,
        allow_terminal_when_resolved: bool = True,
    ) -> tuple[GMToolReceipt, GMToolAgentOutcome | None]:
        """Execute an exact follow-up packet signed by a Python tool.

        Ordinary ``required_followup_calls`` remain model-owned: many contain
        only stable identity fields while the GM still has to choose dialogue,
        an opportunity, or another semantic detail.  This fast path therefore
        accepts only calls whose producing Python handler explicitly marks
        ``python_auto_execute``.  The call runs immediately in the same message
        transaction, so no model can rewrite its arguments and the normal
        registry, freshness and rollback checks still apply.
        """

        terminal_when_resolved = bool(
            source_receipt.result.get("python_auto_followup_terminal") is True
        )
        current = source_receipt
        executed: list[dict[str, object]] = []
        for _ in range(8):
            required = self._receipt_policy.required_followup_tools(receipts)
            calls = self._receipt_policy.required_followup_calls(receipts)
            if not required or not calls:
                break
            call_names = {
                str(call.get("tool_name") or "").strip()
                for call in calls
                if str(call.get("tool_name") or "").strip()
            }
            if call_names != set(required) or any(
                call.get("python_auto_execute") is not True for call in calls
            ):
                break
            disallowed = sorted(
                call_names - self._PYTHON_AUTO_EXECUTE_TOOLS
            )
            if disallowed:
                step["python_signed_followup_rejected"] = {
                    "reason": "tool_not_allowlisted",
                    "tool_names": disallowed,
                }
                break
            # ``any`` with several candidates represents a choice, even when
            # each candidate is otherwise typed.  Python may execute several
            # packets only when the source receipt explicitly requires all.
            active_receipt = next(
                (
                    receipt
                    for receipt in reversed(receipts)
                    if receipt.ok
                    and receipt.result.get("required_followup_resolved") is not True
                    and "required_followup_tools" in receipt.result
                ),
                current,
            )
            mode = str(
                active_receipt.result.get("required_followup_mode") or "any"
            ).strip().lower()
            if len(calls) > 1 and mode != "all":
                break

            progressed = False
            for call in calls:
                tool_name = str(call.get("tool_name") or "").strip()
                arguments = call.get("arguments")
                if not tool_name or not isinstance(arguments, dict):
                    return current, None
                signed_arguments = dict(arguments)
                source_event = active_receipt.result.get("source_event")
                if not isinstance(source_event, dict):
                    source_event = source_receipt.result.get("source_event")
                if isinstance(source_event, dict):
                    source_event_id = str(
                        source_event.get("event_id") or ""
                    ).strip()
                    if source_event_id:
                        # The source belongs to the Python-signed authorization
                        # receipt.  Override rather than trust any stale value
                        # embedded in a generated packet.
                        signed_arguments["source_event_id"] = source_event_id
                event = ledger.execute(tool_name, signed_arguments)
                record = {
                    "tool_name": tool_name,
                    "arguments": dict(signed_arguments),
                    "ok": bool(event.receipt and event.receipt.ok),
                    "protocol_error": event.protocol_error_code,
                }
                executed.append(record)
                step["python_signed_followups"] = list(executed)
                if event.protocol_error_code:
                    step["protocol_error"] = event.protocol_error_code
                    return current, None
                child = event.receipt
                if child is None:
                    return current, None
                current = child
                progressed = True
                self._mark_rule_resolution_followup(
                    child,
                    step=step,
                    mixed_message=mixed_message,
                )
                self._mark_standalone_safety_confirmation(
                    receipts,
                    mixed_message=mixed_message,
                )
                if event.abort_repeated_call_loop:
                    return current, self._agent_output_retry_exhausted(
                        receipt=current,
                        receipts=receipts,
                        trace=trace,
                        must_reply_on_failure=(
                            must_reply_on_failure
                            or (ledger.mutating_call_attempted and not is_system_beat)
                        ),
                    )
                if current.error_code == "STALE_AGENT_REQUEST":
                    self.last_trace = trace
                    return current, GMToolAgentOutcome(
                        handled=True,
                        reply="",
                        receipts=receipts,
                        trace=trace,
                        target="silent",
                        mode="gm_agent_stale",
                        stop_astrbot=True,
                        reason="生成期间出现了新的桌面消息，已在写入前终止过期请求。",
                    )
                if not current.ok:
                    return current, None
            if not progressed:
                break

        if (
            executed
            and allow_terminal_when_resolved
            and terminal_when_resolved
            and not self._receipt_policy.required_followup_tools(receipts)
        ):
            reply = self._receipt_policy.authoritative_reply(receipts)
            if reply:
                self.last_trace = trace
                return current, GMToolAgentOutcome(
                    handled=True,
                    reply=reply,
                    receipts=receipts,
                    trace=trace,
                    target="fu_gm",
                    mode="gm_agent_tool",
                    reason=(
                        "Python签发的封闭后续已在同一事务内完成，"
                        "无需再次调用核心模型抄写参数。"
                    ),
                )
            if (
                not self._context_requires_reply(context)
                and self._mutations_can_commit_silently(
                    receipts,
                    context=context,
                )
            ):
                self.last_trace = trace
                return current, GMToolAgentOutcome(
                    handled=True,
                    reply="",
                    receipts=receipts,
                    trace=trace,
                    target="silent",
                    mode="gm_agent_silent_commit",
                    stop_astrbot=True,
                    reason=(
                        "Python签发的封闭后续已经完整提交；"
                        "玩家公开原话已包含结果，无需GM复述。"
                    ),
                )
        return current, None

    def _execute_batch_python_signed_followups(
        self,
        source_receipt: GMToolReceipt,
        batch_scope: GMBatchToolTransaction,
        batch_receipts: list[dict[str, object]],
        context: GMToolExecutionContext,
        ledger: GMToolCallLedger,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        step: dict[str, object],
        is_system_beat: bool,
        must_reply_on_failure: bool,
        mixed_message: bool,
        allow_terminal_when_resolved: bool,
    ) -> tuple[GMToolReceipt, GMToolAgentOutcome | None, bool]:
        """Run one batch item's Python-signed children in the same transaction."""

        receipt_start = len(receipts)
        current, outcome = self._execute_python_signed_followups(
            source_receipt=source_receipt,
            context=context,
            ledger=ledger,
            receipts=receipts,
            trace=trace,
            step=step,
            is_system_beat=is_system_beat,
            must_reply_on_failure=must_reply_on_failure,
            mixed_message=mixed_message,
            allow_terminal_when_resolved=allow_terminal_when_resolved,
        )
        batch_receipts.extend(
            child.to_dict() for child in receipts[receipt_start:]
        )
        if outcome is not None:
            batch_scope.commit()
            step["batch_receipts"] = batch_receipts
        return current, outcome, len(receipts) > receipt_start

    def _batch_intermediate_terminal_outcome(
        self,
        *,
        receipt: GMToolReceipt,
        may_continue_signed_batch: bool,
        context: GMToolExecutionContext,
        batch_scope: GMBatchToolTransaction,
        batch_receipts: list[dict[str, object]],
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        step: dict[str, object],
    ) -> GMToolAgentOutcome | None:
        """Publish a completed batch item only when no signed child remains."""

        if may_continue_signed_batch:
            return None
        if self._receipt_policy.terminal_public_result_ready(receipt):
            return self._commit_batch_terminal_outcome(
                batch_scope=batch_scope,
                batch_receipts=batch_receipts,
                receipts=receipts,
                trace=trace,
                step=step,
            )
        if self._receipt_policy.heartbeat_public_change_committed(context, receipt):
            batch_scope.commit()
            step["batch_receipts"] = batch_receipts
            self.last_trace = trace
            return self._heartbeat_public_change_outcome(receipts, trace)
        if self._receipt_policy.terminal_public_change_committed(
            receipt,
            terminal_public_tools=self._TERMINAL_PUBLIC_TOOLS,
        ):
            return self._commit_batch_terminal_outcome(
                batch_scope=batch_scope,
                batch_receipts=batch_receipts,
                receipts=receipts,
                trace=trace,
                step=step,
            )
        return None

    def _handle_batch_tool_action(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        ledger: GMToolCallLedger,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
        material_change_required: bool,
        is_system_beat: bool,
        mixed_message: bool = False,
    ) -> GMToolAgentOutcome | None:
        batch_receipts: list[dict[str, object]] = []
        batch_failed = False
        seen_batch_calls: set[str] = set()
        calls = self._prepare_batch_calls(
            decision=decision,
            observed_state=observed_state,
            ledger=ledger,
            history=history,
            step=step,
        )
        if calls is None:
            return None
        batch_scope = GMBatchToolTransaction.begin(
            registry=self.registry, context=context, ledger=ledger,
            receipts=receipts, history=history, calls=calls,
        )
        for batch_index, call in enumerate(calls, start=1):
            tool_name = str(call.get("tool_name") or "").strip()
            arguments = call.get("arguments")
            fingerprint = self._batch_call_fingerprint(tool_name, arguments, context)
            if fingerprint in seen_batch_calls:
                step.setdefault("skipped_duplicate_calls", []).append(
                    {
                        "batch_index": batch_index,
                        "tool_name": tool_name,
                        "reason": "同一批次已有执行语义相同的调用。",
                    }
                )
                continue
            seen_batch_calls.add(fingerprint)
            call_event = ledger.execute(
                tool_name, arguments, batch_index=batch_index,
            )
            if call_event.protocol_error_code:
                batch_failed = True
                if call_event.abort_repeated_call_loop:
                    batch_scope.rollback(batch_receipts, reason="批次触发了重复写入保护。")
                    step["batch_receipts"] = batch_receipts
                    self.last_trace = trace
                    return GMToolAgentOutcome(
                        handled=True,
                        reply=self._receipt_policy.authoritative_reply(receipts),
                        receipts=receipts,
                        trace=trace,
                        reason="已阻止重复执行成功的批量写工具。",
                    )
                break
            receipt = call_event.receipt
            if receipt is None:
                batch_failed = True
                break
            self._mark_rule_resolution_followup(
                receipt,
                step=step,
                mixed_message=mixed_message,
            )
            batch_receipts.append(receipt.to_dict())
            if call_event.abort_repeated_call_loop:
                batch_scope.rollback(
                    batch_receipts,
                    reason="同一工具的智能体输出连续无效，停止整个批次。",
                )
                step["batch_receipts"] = batch_receipts
                return self._agent_output_retry_exhausted(
                    receipt=receipt,
                    receipts=receipts,
                    trace=trace,
                    must_reply_on_failure=(
                        must_reply_on_failure
                        or (ledger.mutating_call_attempted and not is_system_beat)
                    ),
                )
            if receipt.error_code == "STALE_AGENT_REQUEST":
                batch_scope.rollback(batch_receipts, reason="生成期间出现了更新的桌面消息。")
                step["batch_receipts"] = batch_receipts
                self.last_trace = trace
                return GMToolAgentOutcome(
                    handled=True,
                    reply="",
                    receipts=receipts,
                    trace=trace,
                    target="silent",
                    mode="gm_agent_stale",
                    stop_astrbot=True,
                    reason="生成期间出现了新的桌面消息，已在写入前终止过期请求。",
                )
            is_last_batch_call = batch_index == len(calls)
            (
                receipt,
                signed_outcome,
                signed_followup_executed,
            ) = self._execute_batch_python_signed_followups(
                receipt, batch_scope, batch_receipts, context, ledger, receipts,
                trace, step, is_system_beat, must_reply_on_failure, mixed_message,
                allow_terminal_when_resolved=is_last_batch_call,
            )
            if signed_outcome is not None:
                return signed_outcome
            may_continue_signed_batch = bool(
                signed_followup_executed and not is_last_batch_call
            )
            terminal_outcome = self._batch_intermediate_terminal_outcome(
                receipt=receipt,
                may_continue_signed_batch=may_continue_signed_batch,
                context=context,
                batch_scope=batch_scope,
                batch_receipts=batch_receipts,
                receipts=receipts,
                trace=trace,
                step=step,
            )
            if terminal_outcome is not None:
                return terminal_outcome
            if not receipt.ok:
                batch_failed = True
                break
        step["batch_receipts"] = batch_receipts
        if batch_failed or ledger.required_retry_pending:
            batch_scope.rollback(batch_receipts, reason="批次中的调用失败或需要修正参数。")
            return None
        batch_scope.commit()
        self._mark_standalone_safety_confirmation(receipts, mixed_message=mixed_message)
        return self._resolve_batch_terminal(
            decision=decision,
            context=context,
            current_message=current_message,
            recent_context=recent_context,
            observed_state=observed_state,
            receipts=receipts,
            history=history,
            trace=trace,
            step=step,
            deadline=deadline,
            must_reply_on_failure=must_reply_on_failure,
            material_change_required=material_change_required,
            is_system_beat=is_system_beat,
        )

    def _prepare_batch_calls(
        self,
        *,
        decision: dict[str, object],
        observed_state: dict[str, object],
        ledger: GMToolCallLedger,
        history: list[dict[str, object]],
        step: dict[str, object],
    ) -> list[dict[str, object]] | None:
        """Schedule a batch and remove work already committed this message."""

        calls = self._schedule_batch_calls(
            decision=decision,
            observed_state=observed_state,
            step=step,
        )
        fresh_calls: list[dict[str, object]] = []
        for original_index, call in enumerate(calls, start=1):
            tool_name = str(call.get("tool_name") or "").strip()
            arguments = call.get("arguments")
            if not ledger.successful_write_already_recorded(tool_name, arguments):
                fresh_calls.append(call)
                continue
            step.setdefault("skipped_duplicate_calls", []).append(
                {
                    "batch_index": original_index,
                    "tool_name": tool_name,
                    "reason": (
                        "相同写入已在当前消息事务中成功，保留既有结果并继续执行批次中的新动作。"
                    ),
                }
            )
            history.append(
                {
                    "batch_duplicate_success_skipped": {
                        "tool_name": tool_name,
                        "message": (
                            "该写入已在当前消息事务中成功，不再重复执行；"
                            "继续处理本批次尚未完成的动作。"
                        ),
                    }
                }
            )
        if not fresh_calls:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "BATCH_CONTAINS_ONLY_COMPLETED_WRITES",
                        "message": "本批次中的写入都已在当前消息事务中成功。",
                        "correction_hint": (
                            "不要重发已成功写入；处理尚未满足的回执义务，"
                            "若已无义务则立即final。"
                        ),
                        "retryable": True,
                    }
                }
            )
            step["protocol_error"] = "BATCH_CONTAINS_ONLY_COMPLETED_WRITES"
            return None
        dependency_error = self._dependent_batch_error(fresh_calls)
        if dependency_error is not None:
            history.append(dependency_error)
            step["protocol_error"] = "DEPENDENT_TOOL_BATCH_REQUIRES_OBSERVATION"
            return None
        isolation_error = self._replace_state_batch_error(fresh_calls)
        if isolation_error is not None:
            history.append(isolation_error)
            step["protocol_error"] = "REPLACE_STATE_BATCH_MUST_BE_ISOLATED"
            return None
        return fresh_calls

    def _batch_call_fingerprint(
        self,
        tool_name: str,
        arguments: object,
        context: GMToolExecutionContext,
    ) -> str:
        normalized = self.registry.canonical_fingerprint_arguments(
            tool_name, arguments, context,
        )
        return self._protocol.call_fingerprint(tool_name, normalized)

    def _commit_batch_terminal_outcome(
        self,
        *,
        batch_scope: GMBatchToolTransaction,
        batch_receipts: list[dict[str, object]],
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        step: dict[str, object],
    ) -> GMToolAgentOutcome:
        batch_scope.commit()
        step["batch_receipts"] = batch_receipts
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=self._receipt_policy.authoritative_reply(receipts),
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool",
            reason="一个主要规则或场景事务已经提交，停止执行批次中的后续叙事动作。",
        )

    @staticmethod
    def _schedule_batch_calls(
        *,
        decision: dict[str, object],
        observed_state: dict[str, object],
        step: dict[str, object],
    ) -> list[dict[str, object]]:
        calls = [
            call for call in (decision.get("calls") or []) if isinstance(call, dict)
        ]
        calls = LLMGMToolAgent._collapse_redundant_location_projections(
            calls,
            step=step,
        )
        schedule = GMSceneBatchScheduler.schedule(
            calls,
            observed_state=observed_state,
        )
        if schedule.reordered:
            step["batch_schedule"] = {
                "reordered": True,
                "reason": schedule.reason,
                "original_order": list(schedule.original_order),
                "execution_order": list(schedule.execution_order),
            }
        return list(schedule.calls)

    @staticmethod
    def _collapse_redundant_location_projections(
        calls: list[dict[str, object]],
        *,
        step: dict[str, object],
    ) -> list[dict[str, object]]:
        """Normalize duplicate and dependent public-location projections.

        ``map_locations`` is the structured source of truth and automatically
        projects its public name and description into ``major_locations``.  A
        model may nevertheless submit both representations in one batch.  The
        duplicate is safe to remove only when operation, source, authority,
        visibility, name and value all agree; conflicting writes remain visible
        to the normal transaction validator instead of being guessed away.

        A public ``kingdoms`` write also creates or updates a basic map location.
        When the same player contribution includes map attributes for that
        polity, execute the kingdom write first and turn the matching map write
        into an update.  This is a dependency normalization, not a guessed
        merge: name, visibility, authority and source event must all agree.
        """

        def normalized_text(value: object) -> str:
            return " ".join(str(value or "").split())

        def projection_key(call: dict[str, object]) -> tuple[str, ...] | None:
            tool_name = str(call.get("tool_name") or "").strip()
            if tool_name not in {"create_world_setting", "update_world_setting"}:
                return None
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                return None
            name = normalized_text(arguments.get("name"))
            value = normalized_text(arguments.get("value"))
            if not name or not value:
                return None
            return (
                tool_name,
                name,
                value,
                str(arguments.get("visibility") or "public").strip(),
                str(arguments.get("authority") or "").strip(),
                str(arguments.get("source_event_id") or "").strip(),
            )

        structured_locations = {
            key
            for call in calls
            if isinstance(call.get("arguments"), dict)
            and str(call["arguments"].get("category") or "").strip()
            == "map_locations"
            and (key := projection_key(call)) is not None
        }
        retained: list[dict[str, object]] = []
        for index, call in enumerate(calls, start=1):
            arguments = call.get("arguments")
            category = (
                str(arguments.get("category") or "").strip()
                if isinstance(arguments, dict)
                else ""
            )
            key = projection_key(call)
            if category == "major_locations" and key in structured_locations:
                step.setdefault("skipped_projection_calls", []).append(
                    {
                        "batch_index": index,
                        "tool_name": str(call.get("tool_name") or "").strip(),
                        "category": category,
                        "name": str(arguments.get("name") or "").strip(),
                        "reason": (
                            "同批次的map_locations写入会自动同步同名"
                            "major_locations投影。"
                        ),
                    }
                )
                continue
            retained.append(
                {
                    **call,
                    "arguments": dict(arguments)
                    if isinstance(arguments, dict)
                    else arguments,
                }
            )

        def dependency_key(call: dict[str, object]) -> tuple[str, ...] | None:
            tool_name = str(call.get("tool_name") or "").strip()
            if tool_name not in {"create_world_setting", "update_world_setting"}:
                return None
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                return None
            if str(arguments.get("visibility") or "public").strip() != "public":
                return None
            name = normalized_text(arguments.get("name"))
            authority = str(arguments.get("authority") or "").strip()
            source_event_id = str(arguments.get("source_event_id") or "").strip()
            if not name or not authority:
                return None
            return (name, "public", authority, source_event_id)

        kingdom_indices: dict[tuple[str, ...], list[int]] = {}
        map_indices: dict[tuple[str, ...], list[int]] = {}
        for index, call in enumerate(retained):
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                continue
            key = dependency_key(call)
            if key is None:
                continue
            category = str(arguments.get("category") or "").strip()
            if category == "kingdoms":
                kingdom_indices.setdefault(key, []).append(index)
            elif category == "map_locations":
                map_indices.setdefault(key, []).append(index)

        used_kingdom_indices: set[int] = set()
        for key, candidate_map_indices in map_indices.items():
            candidate_kingdom_indices = kingdom_indices.get(key, [])
            if len(candidate_map_indices) != 1 or len(candidate_kingdom_indices) != 1:
                continue
            map_index = candidate_map_indices[0]
            kingdom_index = candidate_kingdom_indices[0]
            if kingdom_index in used_kingdom_indices:
                continue
            used_kingdom_indices.add(kingdom_index)

            kingdom_call = retained[kingdom_index]
            map_call = retained[map_index]
            map_arguments = dict(map_call.get("arguments") or {})
            original_map_tool = str(map_call.get("tool_name") or "").strip()
            map_arguments.pop("expected_revision", None)
            rewritten_map_call = {
                **map_call,
                "tool_name": "update_world_setting",
                "arguments": map_arguments,
            }

            first_index = min(kingdom_index, map_index)
            second_index = max(kingdom_index, map_index)
            retained[first_index] = kingdom_call
            retained[second_index] = rewritten_map_call

            step.setdefault("rewritten_projection_calls", []).append(
                {
                    "name": key[0],
                    "kingdom_batch_index": kingdom_index + 1,
                    "map_batch_index": map_index + 1,
                    "original_map_tool": original_map_tool,
                    "rewritten_map_tool": "update_world_setting",
                    "reason": (
                        "同批次的公开kingdoms写入会先建立同名基础地图地点；"
                        "随后用update_world_setting补充地图属性。"
                    ),
                }
            )
        return retained

    @classmethod
    def _mark_rule_resolution_followup(
        cls,
        receipt: GMToolReceipt,
        *,
        step: dict[str, object],
        mixed_message: bool,
    ) -> None:
        """Keep selected rule results open for natural table-facing prose."""

        if not (
            receipt.tool_name in {"resolve_rule_window", "resolve_gm_opportunity"}
            and receipt.ok
            and receipt.state_changed
            and receipt.lock_public_reply
        ):
            return
        action_type = str(receipt.result.get("action_type") or "").strip()
        if action_type.casefold() == "triggeropportunity":
            committed = receipt.result.get("committed_action")
            committed = committed if isinstance(committed, dict) else {}
            parameters = committed.get("parameters")
            parameters = parameters if isinstance(parameters, dict) else {}
            effect = str(
                receipt.result.get("opportunity_effect")
                or receipt.result.get("effect")
                or receipt.result.get("choice")
                or parameters.get("effect")
                or parameters.get("opportunity")
                or ""
            ).strip()
            if effect.casefold() in cls._NATURAL_OPPORTUNITY_EFFECTS:
                receipt.lock_public_reply = False
                receipt.result["natural_resolution_pending"] = True
                step["continued_for_natural_resolution"] = True
        if mixed_message:
            receipt.lock_public_reply = False
            receipt.result["mixed_message_followup_pending"] = True
            step["continued_for_mixed_message"] = True

    @staticmethod
    def _mark_standalone_safety_confirmation(
        receipts: list[GMToolReceipt],
        *,
        mixed_message: bool,
    ) -> None:
        """安全边界单独成句时只确认记录，不复述敏感内容。

        若同一条消息还包含独立问题，仍让模型继续回答，避免锁定回执
        吞掉玩家的另一项请求。只有本事务中所有已提交写入都是安全边界
        时，才把既有的简短工具回执设为最终公开文本。
        """

        if mixed_message:
            return
        committed = [
            receipt
            for receipt in receipts
            if receipt.ok and receipt.state_changed
        ]
        if not committed or any(
            receipt.tool_name != "record_safety_boundary"
            for receipt in committed
        ):
            return
        for receipt in committed:
            receipt.lock_public_reply = True

    def _replace_state_batch_error(
        self,
        calls: list[dict[str, object]],
    ) -> dict[str, object] | None:
        mutating_calls = [
            str(call.get("tool_name") or "").strip()
            for call in calls
            if self.registry.side_effect(
                str(call.get("tool_name") or "").strip()
            )
            not in {"", "read"}
        ]
        replacement_calls = [
            name
            for name in mutating_calls
            if self.registry.side_effect(name) == "replace_state"
        ]
        if not replacement_calls or len(mutating_calls) <= 1:
            return None
        return {
            "protocol_error": {
                "error_code": "REPLACE_STATE_BATCH_MUST_BE_ISOLATED",
                "message": (
                    "读取、创建或删除战役会替换完整权威状态，"
                    "不能与其他写工具在同一批次执行。"
                ),
                "correction_hint": (
                    f"先单独调用 {replacement_calls[0]}；"
                    "获得新状态摘要后，再在下一次迭代决定其他写操作。"
                ),
                "retryable": True,
            }
        }

    @staticmethod
    def _dependent_batch_error(
        calls: list[dict[str, object]],
    ) -> dict[str, object] | None:
        names = [
            str(call.get("tool_name") or "").strip()
            for call in calls
            if isinstance(call, dict)
        ]
        if "update_hero_draft" not in names or "confirm_hero_draft" not in names:
            return None
        return {
            "protocol_error": {
                "error_code": "DEPENDENT_TOOL_BATCH_REQUIRES_OBSERVATION",
                "message": (
                    "角色草稿更新与确认不能放在同一批次；确认必须读取更新后的"
                    "ready与实际缺项，批次失败也不应抹掉玩家本轮的有效资料。"
                ),
                "correction_hint": (
                    "先单独调用update_hero_draft。收到成功回执后，只有ready=true才在"
                    "下一轮调用confirm_hero_draft；否则保留更新并向玩家询问回执中的实际缺项。"
                ),
                "retryable": True,
            }
        }

    def _resolve_batch_terminal(
        self,
        *,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
        material_change_required: bool,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        terminal = str(decision.get("terminal_decision") or "").strip().lower()
        if (
            terminal in {"final", "ask_user", "silent", "external"}
            and material_change_required
            and not self._material_change_silence_allowed(
                context=context,
                receipts=receipts,
                terminal=terminal,
                is_system_beat=is_system_beat,
            )
            and not any(
                self._receipt_policy.public_material_change_committed(receipt)
                for receipt in receipts
            )
        ):
            history.append(self._protocol.material_change_error())
            return None
        if terminal in {"final", "ask_user"}:
            return self._handle_batch_reply(
                terminal=terminal,
                decision=decision,
                context=context,
                current_message=current_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                must_reply_on_failure=must_reply_on_failure,
            )
        if terminal in {"silent", "external"}:
            return self._handle_batch_silence(
                terminal=terminal,
                decision=decision,
                context=context,
                current_message=current_message,
                recent_context=recent_context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                trace=trace,
                step=step,
                deadline=deadline,
                must_reply_on_failure=must_reply_on_failure,
                is_system_beat=is_system_beat,
            )
        return None

    @staticmethod
    def _scene_change_authority_rejected(
        receipts: list[GMToolReceipt],
    ) -> bool:
        """主动节拍没有可信触发时允许把本拍安全收束为静默。"""

        return any(
            not receipt.ok
            and str(receipt.error_code or "").startswith("SCENE_CHANGE_")
            for receipt in receipts
        )

    def _material_change_silence_allowed(
        self,
        *,
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        terminal: str,
        is_system_beat: bool,
    ) -> bool:
        """Allow a proactive beat to stop when no trusted change is due.

        ``heartbeat_require_material_change`` describes the preferred outcome,
        not permission to invent one.  If the scheduler supplied no exact due
        scene-change authority, a model that correctly decides to stay silent
        must be able to finish in one iteration instead of being trapped in a
        MATERIAL_CHANGE_REQUIRED retry loop.
        """

        return bool(
            is_system_beat
            and terminal in {"silent", "external"}
            and (
                self._scene_change_authority_rejected(receipts)
                or not SceneChangeAuthorityPolicy.has_pending_system_beat_authority(
                    context
                )
            )
        )

    def _agent_output_retry_exhausted(
        self,
        *,
        receipt: GMToolReceipt,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        must_reply_on_failure: bool,
    ) -> GMToolAgentOutcome:
        self.last_error = f"工具 {receipt.tool_name} 连续三次未能通过类型校验或完成事务。"
        self.last_trace = trace
        return self._failure_policy.tool_retry_exhausted(
            receipts=receipts,
            trace=trace,
            must_reply=must_reply_on_failure,
            error=self.last_error,
        )

    def _handle_batch_reply(
        self,
        *,
        terminal: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
    ) -> GMToolAgentOutcome | None:
        reply_parts = self._post_tool_reply_parts(
            decision=decision,
            receipts=receipts,
            current_message=current_message,
            history=history,
            step=step,
        )
        if reply_parts is None:
            return None
        resolution_reply, independent_reply = reply_parts
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if (
            terminal == "final"
            and not locked_reply
            and self._mutations_can_commit_silently(
                receipts,
                context=context,
            )
        ):
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply="",
                receipts=receipts,
                trace=trace,
                target="silent",
                mode="gm_agent_silent_commit",
                stop_astrbot=True,
                reason=(
                    "玩家原消息已经完整公开了本地行动；"
                    "工具仅登记行动轮，无需由GM改写复述。"
                ),
                terminal_action="silent",
            )
        reply, reply_parts = self._compose_post_tool_public_reply(
            action=terminal,
            decision=decision,
            receipts=receipts,
            resolution_reply=resolution_reply,
            independent_reply=independent_reply,
            step=step,
        )
        if not reply:
            self._record_terminal_reply_required(
                action=terminal,
                receipts=receipts,
                history=history,
                step=step,
            )
            return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
            reply_parts=reply_parts,
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool",
            reason=str(decision.get("reason") or "").strip(),
            terminal_action=terminal,
        )

    def _handle_batch_silence(
        self,
        *,
        terminal: str,
        decision: dict[str, object],
        context: GMToolExecutionContext,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        trace: list[dict[str, object]],
        step: dict[str, object],
        deadline: float,
        must_reply_on_failure: bool,
        is_system_beat: bool,
    ) -> GMToolAgentOutcome | None:
        silent_commit_allowed = (
            terminal == "silent"
            and self._mutations_can_commit_silently(
                receipts,
                context=context,
            )
        )
        if (
            not is_system_beat
            and any(receipt.ok and receipt.state_changed for receipt in receipts)
            and not silent_commit_allowed
        ):
            history.append(
                {
                    "protocol_error": {
                        "error_code": "PLAYER_STATE_CHANGE_REQUIRES_ACKNOWLEDGEMENT",
                        "message": "当前玩家请求已成功改变权威状态，不能无声结束。",
                        "correction_hint": self._STATE_CHANGE_ACKNOWLEDGEMENT_HINT,
                        "retryable": True,
                    }
                }
            )
            return None
        if (
            not is_system_beat
            and self._context_requires_reply(context)
            and not silent_commit_allowed
        ):
            history.append(
                {
                    "protocol_error": {
                        "error_code": "ADDRESSED_MESSAGE_REQUIRES_REPLY",
                        "message": "直接发给时悠的消息在工具批次后不能静默或转交。",
                        "correction_hint": "使用terminal_decision=final并给出自然回复。",
                        "retryable": True,
                    }
                }
            )
            return None
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if locked_reply:
            self.last_trace = trace
            return GMToolAgentOutcome(
                handled=True,
                reply=locked_reply,
                receipts=receipts,
                trace=trace,
                target="fu_gm",
                mode="gm_agent_tool",
            )
        if terminal == "external" and receipts:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "STATE_CHANGE_CANNOT_ROUTE_EXTERNAL",
                        "message": "工具批次已经执行，不能再转交外部机器人。",
                        "correction_hint": "改用terminal_decision=final或silent。",
                        "retryable": True,
                    }
                }
            )
            return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            receipts=receipts,
            trace=trace,
            target="silent" if terminal == "silent" else "astrbot",
            mode="gm_agent_silent" if terminal == "silent" else "gm_agent_external",
            stop_astrbot=terminal == "silent",
            terminal_action=terminal,
        )

    def _enforce_receipt_followup(
        self,
        *,
        decision: dict[str, object],
        action: str,
        context: GMToolExecutionContext,
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
        trace: list[dict[str, object]],
        clarification_authorized: bool = False,
    ) -> tuple[bool, GMToolAgentOutcome | None]:
        """Keep receipt-authorized continuations out of the main agent loop."""

        required = self._receipt_policy.required_followup_tools(receipts)
        allowed = self._receipt_policy.allowed_followup_tools(receipts)
        integrity_repairs = self._pending_message_integrity_repair_tools(context)
        if (
            required
            and action == "ask_user"
            and clarification_authorized
            and self._required_followup_is_readonly(receipts)
        ):
            # 能力发现本身不改变游戏状态。若模型随后提交的具体工具被
            # 语义审计判定为假前提或确实缺少玩家选择，应允许它只追问
            # 一次，而不能让只读回执把ask_user永久挡在循环外。
            step["readonly_followup_interrupted_for_clarification"] = True
            return False, None
        if required and action not in {"call_tool", "call_tools"}:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "REQUIRED_FOLLOWUP_PENDING",
                        "message": "上一条成功回执要求先完成获准的后续工具，本轮事务尚未结束。",
                        "correction_hint": (
                            "只能继续调用以下工具之一："
                            + "、".join(sorted(required))
                            + "；不得提前final、ask_user、silent或external。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return True, None
        if required and action in {"call_tool", "call_tools"}:
            requested_calls = (
                [
                    {
                        "tool_name": str(
                            decision.get("tool_name") or ""
                        ).strip(),
                        "arguments": (
                            dict(decision.get("arguments") or {})
                            if isinstance(decision.get("arguments"), dict)
                            else {}
                        ),
                    }
                ]
                if action == "call_tool"
                else [
                    {
                        "tool_name": str(call.get("tool_name") or "").strip(),
                        "arguments": (
                            dict(call.get("arguments") or {})
                            if isinstance(call.get("arguments"), dict)
                            else {}
                        ),
                    }
                    for call in list(decision.get("calls") or [])
                    if isinstance(call, dict)
                ]
            )
            permitted_during_followup = set(required) | integrity_repairs
            outside_required = [
                call
                for call in requested_calls
                if call["tool_name"] not in permitted_during_followup
            ]
            if outside_required:
                history.append(
                    {
                        "protocol_error": {
                            "error_code": "REQUIRED_FOLLOWUP_TOOL_MISMATCH",
                            "message": "上一条准备操作尚未完成，不能先调用其他工具。",
                            "correction_hint": (
                                "先完成Python签发的当前事务工具："
                                + "、".join(sorted(permitted_during_followup))
                                + "。其中语义完整性修复可以先执行；随后仍须完成"
                                "required_followup_calls。"
                            ),
                            "retryable": True,
                        }
                    }
                )
                step["protocol_error"] = "REQUIRED_FOLLOWUP_TOOL_MISMATCH"
                return True, None
            mismatched = [
                call
                for call in requested_calls
                if call["tool_name"] in required
                and not self._receipt_policy.followup_call_matches(
                    receipts,
                    tool_name=str(call["tool_name"]),
                    arguments=dict(call["arguments"]),
                )
            ]
            if mismatched:
                history.append(
                    {
                        "protocol_error": {
                            "error_code": "REQUIRED_FOLLOWUP_ARGUMENT_MISMATCH",
                            "message": (
                                "后续工具名称正确，但NPC、窗口或条件标识"
                                "与尚未完成的义务不一致。"
                            ),
                            "correction_hint": (
                                "逐字沿用required_followup_calls中的稳定标识；"
                                "自然语言答复、机会选择和细节仍由你根据当前状态决定。"
                            ),
                            "required_followup_calls": (
                                self._receipt_policy.required_followup_calls(
                                    receipts
                                )
                            ),
                            "retryable": True,
                        }
                    }
                )
                step["protocol_error"] = (
                    "REQUIRED_FOLLOWUP_ARGUMENT_MISMATCH"
                )
                return True, None
            # required_followup_tools是当前事务的强制下一步，已经通过名称
            # 与稳定参数校验后必须放行；不能继续落入可选后续白名单，后者
            # 只负责没有强制义务时的公开回执收尾。
            return False, None
        if allowed is None or action not in {"call_tool", "call_tools"}:
            return False, None
        requested = (
            [str(decision.get("tool_name") or "").strip()]
            if action == "call_tool"
            else [
                str(call.get("tool_name") or "").strip()
                for call in list(decision.get("calls") or [])
                if isinstance(call, dict)
            ]
        )
        if all(name and name in (set(allowed) | integrity_repairs) for name in requested):
            return False, None
        step["protocol_error"] = "PUBLIC_RECEIPT_FOLLOWUP_NOT_ALLOWED"
        self.last_trace = trace
        return False, GMToolAgentOutcome(
            handled=True,
            reply=self._receipt_policy.authoritative_reply(receipts),
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool",
            reason="公开NPC事务只允许显式授权的后续能力；已安全停止额外操作。",
        )

    @staticmethod
    def _required_followup_is_readonly(receipts: list[GMToolReceipt]) -> bool:
        """判断当前强制后续是否仅由未写入状态的准备回执产生。"""

        for receipt in reversed(receipts):
            if not receipt.ok:
                continue
            if receipt.result.get("required_followup_resolved") is True:
                return False
            if "required_followup_tools" not in receipt.result:
                if receipt.state_changed:
                    return False
                continue
            return not receipt.state_changed
        return False


    def _available_tool_schemas(
        self,
        context: GMToolExecutionContext,
        *,
        receipts: list[GMToolReceipt] | None = None,
        required_retry_tool: str = "",
    ) -> list[dict[str, object]]:
        retry_tool = str(required_retry_tool or "").strip()
        receipt_list = list(receipts or [])
        required = self._receipt_policy.required_followup_tools(receipt_list)
        required_set = set(required or set())
        integrity_repairs = self._pending_message_integrity_repair_tools(context)
        if retry_tool:
            redirected = (
                self.registry.schemas({retry_tool})
                if retry_tool in (required_set | integrity_repairs)
                else self._capability_policy.schemas_for_names(
                    self.registry,
                    context,
                    {retry_tool},
                )
            )
            readable = [
                schema
                for schema in self._capability_policy.schemas(self.registry, context)
                if str(schema.get("side_effect") or "") == "read"
                and self._tool_is_permitted(
                    str(schema.get("name") or ""),
                    context,
                )
            ]
            by_name = {
                str(schema.get("name") or ""): schema
                for schema in [*redirected, *readable]
            }
            return list(by_name.values())
        if required:
            # 场景准备工具可以通过权威回执要求一个不在节拍初始能力集内的
            # 事务内后续；只有该回执能临时暴露这项精确能力。
            return self.registry.schemas(required_set | integrity_repairs)
        allowed = required or self._receipt_policy.allowed_followup_tools(receipt_list)
        if allowed is None:
            # Small custom registries used by extensions and unit tests retain
            # the legacy direct schema surface. FU-GM's full registry exposes a
            # discovery capability and therefore uses a bounded catalog:
            # system beats receive their already narrow trusted set, ordinary
            # messages receive only meta-tools plus domains granted during this
            # one agent transaction.
            if GMCapabilityBroker.DISCOVERY_TOOL not in self.registry._tools:
                # A narrow extension/test registry has no capability broker to
                # grant its custom schemas later. Expose that deliberately
                # bounded registry directly for ordinary player messages;
                # otherwise every custom tool is registered but permanently
                # invisible to the model. System beats still use their trusted
                # minimal scope and must never inherit unrelated extension
                # writes merely because the registry is small.
                if not context.metadata.get("system_gm_beat_request"):
                    return self.registry.schemas()
                return self._capability_policy.schemas(self.registry, context)
            if not context.metadata.get("gm_dynamic_capabilities_enabled"):
                return self._capability_policy.schemas(self.registry, context)
            phase_tools = set(
                self._capability_policy.phase_tool_names(
                    self.registry,
                    context,
                )
                or set()
            )
            names = GMCapabilityBroker.initial_tool_names(
                registry=self.registry,
                context=context,
                phase_tools=phase_tools,
            )
            semantic_names = {
                str(item or "").strip()
                for item in list(
                    context.metadata.get("gm_semantic_tool_names") or []
                )
                if str(item or "").strip()
            }
            if context.gate_status == "session_zero" and semantic_names:
                # The first request used the conservative Session Zero hot set.
                # Once its LLM-authored interpretation is frozen, retries use
                # only the semantic CRUD family plus permanent meta-tools.
                names.update(semantic_names & phase_tools)
            else:
                names.update(
                    GMCapabilityBroker.granted_tool_names(context) & phase_tools
                )
            schemas = self._capability_policy.schemas_for_names(
                self.registry,
                context,
                names,
            )
            return self._merge_tool_schemas(
                schemas,
                self.registry.schemas(integrity_repairs),
            )
        schemas = self._capability_policy.schemas_for_names(
            self.registry,
            context,
            set(allowed),
        )
        return self._merge_tool_schemas(
            schemas,
            self.registry.schemas(integrity_repairs),
        )

    @staticmethod
    def _merge_tool_schemas(
        *schema_groups: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        by_name: dict[str, dict[str, object]] = {}
        for schemas in schema_groups:
            for schema in schemas:
                name = str(schema.get("name") or "").strip()
                if name:
                    by_name[name] = schema
        return list(by_name.values())

    def _tool_is_permitted(
        self,
        tool_name: str,
        context: GMToolExecutionContext,
    ) -> bool:
        """Enforce the same trusted scope used to build ``available_tools``."""

        clean_name = str(tool_name or "").strip()
        if clean_name not in self.registry._tools:
            # Let the registry return its precise UNKNOWN_TOOL receipt.
            return True
        managed = self._capability_policy.managed_tool_names()
        if clean_name not in managed:
            if context.metadata.get("system_gm_beat_request"):
                # 系统节拍没有玩家在场授权扩展能力。尤其线上群聊续接的
                # 可信工具集为空，不能靠猜测一个扩展工具名绕过它。
                return False
            # Standalone extensions and narrow unit-test registries may add
            # tools outside FU-GM's built-in policy surface.
            return True
        phase_tools = set(
            self._capability_policy.phase_tool_names(
                self.registry,
                context,
            )
            or set()
        )
        followup = context.metadata.get(
            self._receipt_policy.REQUIRED_FOLLOWUP_CONTEXT_KEY
        )
        receipt_required_tools = (
            {
                str(item or "").strip()
                for item in list(followup.get("required_tools") or [])
                if str(item or "").strip()
            }
            if isinstance(followup, dict)
            else set()
        )
        integrity_repairs = self._pending_message_integrity_repair_tools(context)
        if (
            clean_name not in phase_tools
            and clean_name not in receipt_required_tools
            and clean_name not in integrity_repairs
        ):
            return False
        if (
            GMCapabilityBroker.DISCOVERY_TOOL not in self.registry._tools
            or not context.metadata.get("gm_dynamic_capabilities_enabled")
            or context.metadata.get("system_gm_beat_request")
        ):
            return True
        initial = GMCapabilityBroker.initial_tool_names(
            registry=self.registry,
            context=context,
            phase_tools=phase_tools,
        )
        if clean_name in initial:
            return True
        if clean_name in GMCapabilityBroker.granted_tool_names(context):
            return True
        if clean_name in receipt_required_tools:
            return True
        if clean_name in integrity_repairs:
            return True
        if (
            clean_name in phase_tools
            and self.registry.allows_addressed_dynamic_grant(clean_name)
            and not context.metadata.get("system_gm_beat_request")
            and bool(
                context.directly_addressed
                or context.is_private
                or context.metadata.get("_semantic_gm_addressed")
            )
        ):
            # Preserve the core capability invariant: a managed tool may only
            # execute after its full schema was visible to the model.  A model
            # that correctly names a public read omitted by narrow routing gets
            # it granted for the next iteration, but this first attempt remains
            # a non-executing protocol retry.
            GMCapabilityBroker.grant(context, {clean_name})
            context.metadata["gm_addressed_dynamic_read_grants"] = sorted(
                GMCapabilityBroker.granted_tool_names(context)
            )
            return False
        return False

    def _deadline_outcome(
        self,
        deadline: float,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
        must_decide: bool,
        must_reply: bool,
    ) -> GMToolAgentOutcome | None:
        if time.monotonic() < deadline:
            return None
        self.last_error = "GM工具事务已超过共享截止时间。"
        self.last_trace = trace
        return self._failure_policy.provider_failure(
            receipts=receipts,
            trace=trace,
            error=self.last_error,
            must_decide=must_decide,
            must_reply=must_reply,
        )

    def _terminal_public_change_outcome(
        self,
        receipt: GMToolReceipt,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
    ) -> GMToolAgentOutcome | None:
        if not self._receipt_policy.terminal_public_change_committed(
            receipt,
            terminal_public_tools=self._TERMINAL_PUBLIC_TOOLS,
        ):
            return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=self._receipt_policy.authoritative_reply(receipts),
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool",
            reason="一个主要规则或场景事务已经提交，停止重复结算当前消息。",
        )

    def _system_prompt(
        self,
        context: GMToolExecutionContext,
        *,
        observed_state: dict[str, object] | None = None,
        has_receipts: bool = False,
    ) -> str:
        if (
            context.metadata.get("system_gm_beat_request")
            and context.metadata.get("heartbeat_persona_chat_only")
            and str(context.metadata.get("heartbeat_action") or "")
            == "adventure_table_nudge"
        ):
            persona = self.gm_personality_prompt
            return (
                persona + "\n\n" + TABLE_CHAT_HEARTBEAT_SYSTEM_PROMPT
                if persona
                else TABLE_CHAT_HEARTBEAT_SYSTEM_PROMPT
            )
        if context.metadata.get("system_gm_beat_request"):
            base_prompt = self._HEARTBEAT_SYSTEM_PROMPT
        elif has_receipts:
            base_prompt = self._POST_TOOL_SYSTEM_PROMPT
        else:
            runtime = (observed_state or {}).get("runtime")
            runtime = runtime if isinstance(runtime, dict) else {}
            conflict = runtime.get("conflict")
            conflict = conflict if isinstance(conflict, dict) else {}
            base_prompt = build_initial_gm_system_prompt(
                gate_status=context.gate_status,
                conflict_active=bool(conflict.get("active")),
            )

        layers = [CORE_AGENT_SYSTEM_PREFIX]
        if self.gm_personality_prompt:
            layers.append(self.gm_personality_prompt)
        layers.extend((CORE_PUBLIC_EXPRESSION_CONTRACT, base_prompt))
        return "\n\n".join(layers)
    @classmethod
    def _heartbeat_public_change_outcome(
        cls,
        receipts: list[GMToolReceipt],
        trace: list[dict[str, object]],
    ) -> GMToolAgentOutcome:
        return GMToolAgentOutcome(
            handled=True,
            reply=cls._receipt_policy.locked_public_reply(receipts),
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool",
            reason="一次主动节拍已经提交一个公开局面变化，停止继续推进。",
        )
