from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any, Callable

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolFreshnessGuard,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_tool_execution import GMToolCallLedger
from fu_gm.gm_tool_receipts import GMToolReceiptPolicy
from fu_gm.gm_tool_protocol import (
    GMToolDecisionProtocolError,
    GMToolProtocol,
)
from fu_gm.components.gm_agent_decision_requester import (
    GMToolAgentDecisionRequester,
)
from fu_gm.components.gm_agent_capability_policy import (
    GMToolAgentCapabilityPolicy,
)
from fu_gm.components.gm_agent_failure_policy import GMToolAgentFailurePolicy
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.components.gm_supervisor import GMCapabilityBroker
from fu_gm.components.gm_agent_prompts import (
    CORE_GM_SYSTEM_PROMPT,
    HEARTBEAT_SYSTEM_PROMPT,
    POST_TOOL_SYSTEM_PROMPT,
    build_initial_gm_system_prompt,
)
from fu_gm.components.gm_batch_tool_transaction import GMBatchToolTransaction
from fu_gm.components.gm_message_tool_transaction import (
    GMMessageToolTransaction,
)
from fu_gm.components.gm_message_envelope import GMMessageEnvelopeBuilder
from fu_gm.gm_persona import GMPersonaProfile, persona_mode_for_context
from fu_gm.llm_client import ChatMessage


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
            "start_session",
            "pause_session",
            "end_session",
            "start_scene",
            "transition_scene",
            "end_scene",
            "start_conflict",
            "end_conflict",
            "commit_scene_response",
            "introduce_npc",
            "perform_check_action",
            "perform_character_action",
			"perform_scene_action",
			"perform_in_scene_action",
			"commit_story_item_action",
			"move_group_within_scene",
			"move_scene_group",
			"pass_in_scene_action",
			"perform_ritual_project_action",
            "resolve_rule_window",
            "resolve_gm_opportunity",
            "run_current_npc_turn",
            "create_clock",
            "change_clock",
            "close_clock",
            "travel_party",
            "award_stage_reward",
            "generate_world_map_preview",
            "get_world_map_status",
            "edit_world_map",
        }
    )
    _receipt_policy = GMToolReceiptPolicy
    _protocol = GMToolProtocol
    _failure_policy = GMToolAgentFailurePolicy
    _capability_policy = GMToolAgentCapabilityPolicy


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
        self.gm_persona = GMPersonaProfile.from_markdown(
            gm_personality_prompt,
            source="core_agent",
        )
        self._decision_requester = GMToolAgentDecisionRequester(
            client,
            model=self.model,
            repair_model=protocol_repair_model or self.model,
            protocol=self._protocol,
            parse_retries=self.parse_retries,
            max_output_tokens=self.max_output_tokens,
        )
        self.last_error = ""
        self.last_trace: list[dict[str, object]] = []

    def run(
        self,
        message: str,
        *,
        recent_context: str,
        context: GMToolExecutionContext,
        state_summary: dict[str, object],
        state_summary_provider: Callable[[], dict[str, object]] | None = None,
        freshness_guard: GMToolFreshnessGuard | None = None,
        side_effect_lock: Any | None = None,
    ) -> GMToolAgentOutcome:
        message_transaction = GMMessageToolTransaction.begin(
            registry=self.registry,
            context=context,
            state_summary=state_summary,
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
            )
        except Exception:
            message_transaction.rollback()
            raise
        return self._finalize_message_transaction(
            outcome,
            context=context,
            transaction=message_transaction,
        )

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
    ) -> GMToolAgentOutcome:
        clean_message = str(message or "").strip()
        if not clean_message or clean_message.startswith("/"):
            return GMToolAgentOutcome(handled=False)
        deadline = time.monotonic() + self.timeout_seconds
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
        trace: list[dict[str, object]] = []
        self.last_error = ""
        self.last_trace = []
        is_system_beat = bool(context.metadata.get("system_gm_beat_request"))
        must_decide = bool(
            context.directly_addressed
            or context.is_private
            or context.gate_status in {"pre_session", "session_zero", "adventure", "paused"}
            or context.metadata.get("forced_route_mode")
            or is_system_beat
        )
        must_reply_on_failure = bool(not is_system_beat and (
            context.directly_addressed
            or context.is_private
            or context.metadata.get("force_gm_reply")
        ))
        material_change_required = bool(
            context.metadata.get("heartbeat_require_material_change")
        )

        for iteration in range(1, self.max_iterations + 1):
            deadline_outcome = self._deadline_outcome(
                deadline, receipts, trace, must_decide, must_reply_on_failure
            )
            if deadline_outcome is not None:
                return deadline_outcome
            observed_state = self._refresh_observed_state(
                state_summary,
                state_summary_provider=state_summary_provider,
                iteration=iteration,
                trace=trace,
            )
            deadline_outcome = self._deadline_outcome(
                deadline, receipts, trace, must_decide, must_reply_on_failure
            )
            if deadline_outcome is not None:
                return deadline_outcome
            messages = self._build_decision_messages(
                current_message=clean_message,
                recent_context=recent_context,
                context=context,
                observed_state=observed_state,
                receipts=receipts,
                history=history,
                required_retry_tool=ledger.required_retry_tool,
            )
            try:
                decision = self._decision_requester.request(
                    messages,
                    iteration=iteration,
                    deadline=deadline,
                    trace=trace,
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
                        "error": str(exc)[:300],
                    }
                )
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self.last_trace = trace
                return self._failure_policy.provider_failure(
                    receipts=receipts,
                    trace=trace,
                    error=self.last_error,
                    must_decide=must_decide,
                    must_reply=must_reply_on_failure,
                )

            step = self._decision_trace_step(decision, iteration=iteration)
            trace.append(step)
            action = str(decision.get("decision") or "").strip().lower()
            retry_followup, followup_outcome = self._enforce_receipt_followup(
                decision=decision,
                action=action,
                receipts=receipts,
                history=history,
                step=step,
                trace=trace,
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
                )
            else:
                history.append(self._protocol.invalid_decision_error())
                outcome = None
            if outcome is not None:
                return outcome

        self.last_trace = trace
        return self._failure_policy.exhausted(
            receipts=receipts,
            trace=trace,
            must_decide=must_decide,
            must_reply=must_reply_on_failure,
        )

    def _finalize_message_transaction(
        self,
        outcome: GMToolAgentOutcome,
        *,
        context: GMToolExecutionContext,
        transaction: GMMessageToolTransaction,
    ) -> GMToolAgentOutcome:
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
            and (str(outcome.reply or "").strip() or has_reply_media)
        )
        silently_deliverable = bool(
            not publicly_deliverable
            and self._mutations_can_commit_silently(
                outcome.receipts,
                context=context,
            )
        )
        recovered = self._receipt_policy.state_change_recovered(outcome.receipts)
        if recovered and (publicly_deliverable or silently_deliverable):
            commit_error = transaction.commit()
            if not commit_error:
                if silently_deliverable:
                    outcome.reply = ""
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
        must_reply = bool(
            not is_system_beat
            and (
                context.directly_addressed
                or context.is_private
                or context.metadata.get("force_gm_reply")
            )
        )
        outcome.reply = (
            "刚才这句没有完整处理完，团里没有留下改动。麻烦再说一次。"
            if must_reply
            else ""
        )
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
        return bool(
            mutations
            and not context.directly_addressed
            and not context.is_private
            and not context.metadata.get("force_gm_reply")
            and all(
                receipt.result.get("silent_commit_allowed") is True
                for receipt in mutations
            )
        )

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
                    "error": str(exc)[:300],
                }
            )
        return observed_state

    def _build_decision_messages(
        self,
        *,
        current_message: str,
        recent_context: str,
        context: GMToolExecutionContext,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        required_retry_tool: str = "",
    ) -> list[ChatMessage]:
        request_context_keys = (
            "forced_route_mode",
            "system_gm_beat_request",
            "heartbeat_action",
            "heartbeat_instruction",
            "heartbeat_force",
            "heartbeat_require_material_change",
            "heartbeat_require_consequence",
            "heartbeat_require_local_change",
            "heartbeat_require_local_resolution",
            "heartbeat_idle_episode",
            "heartbeat_session_zero_target",
            "heartbeat_supervisor_alerts",
            "inspection_focus",
        )
        request_context = {
            key: context.metadata[key]
            for key in request_context_keys
            if key in context.metadata
        }
        request_context.update(
            GMMessageEnvelopeBuilder.model_request_context(context.metadata)
        )
        request = {
            "current_message": current_message,
            "recent_public_context": str(recent_context or "")[-3000:],
            "session": {
                "campaign_id": context.campaign_id,
                "session_id": context.session_id,
                "speaker": context.speaker,
                "gate_status": context.gate_status,
                "is_private": context.is_private,
                "directly_addressed": context.directly_addressed,
            },
            "request_context": request_context,
            "current_state_summary": observed_state,
            "available_tools": self._available_tool_schemas(
                context,
                receipts=receipts,
                required_retry_tool=required_retry_tool,
            ),
            "history": history,
        }
        return [
            ChatMessage(
                role="system",
                content=self._system_prompt(
                    context,
                    observed_state=observed_state,
                    has_receipts=bool(receipts),
                ),
            ),
            ChatMessage(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]

    def _decision_trace_step(
        self,
        decision: dict[str, object],
        *,
        iteration: int,
    ) -> dict[str, object]:
        step: dict[str, object] = {
            "iteration": iteration,
            "decision": str(decision.get("decision") or ""),
            "audience": str(decision.get("audience") or ""),
            "tool_name": str(decision.get("tool_name") or ""),
            "reason": str(decision.get("reason") or "")[:300],
        }
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

        if material_change_required and not any(
            receipt.ok and receipt.state_changed for receipt in receipts
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
        if not is_system_beat and not receipts and not audience:
            history.append(
                {
                    "protocol_error": {
                        "error_code": "MESSAGE_AUDIENCE_REQUIRED_FOR_SILENCE",
                        "message": "静默或转交前必须先判断当前消息的实际受众。",
                        "correction_hint": (
                            "结合recent_public_context解析称呼、代词和省略主语，"
                            "在audience中填写gm、players、table或external后重新决策。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return None
        if not is_system_beat and audience == "gm":
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
        if not is_system_beat and (context.directly_addressed or context.is_private):
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
        silent_commit_allowed = (
            action == "silent"
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
                        "correction_hint": (
                            "根据成功回执给出一句与本次结果直接相关的自然确认；"
                            "不要附送下一步清单或重复完整状态。"
                        ),
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
        audience = str(decision.get("audience") or "").strip().lower()
        if (
            not is_system_beat
            and not receipts
            and not context.directly_addressed
            and not context.is_private
            and audience in {"players", "table"}
        ):
            history.append(
                {
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
            )
            return None
        locked_reply = self._receipt_policy.locked_public_reply(receipts)
        if (
            action == "final"
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
        reply = str(decision.get("reply") or "").strip()
        if action == "final" and receipts and not receipts[-1].ok:
            # A model cannot turn a rejected transaction into a public fact.
            # The failure receipt remains private retry feedback; if retries
            # are exhausted, the failure policy decides whether the table
            # needs a generic clarification or silence.
            reply = ""
        if locked_reply:
            reply = locked_reply
        if not reply:
            reply = self._receipt_policy.receipt_fallback(receipts)
        if not reply:
            return None
        if not receipts and self._protocol.is_exact_player_echo(reply, current_message):
            if not is_system_beat and (context.directly_addressed or context.is_private):
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
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
            receipts=receipts,
            trace=trace,
            target="fu_gm",
            mode="gm_agent_tool" if receipts else "gm_agent_reply",
            reason=str(decision.get("reason") or "").strip(),
            terminal_action=action,
        )

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
        step["receipt"] = receipt.to_dict()
        if call_event.abort_repeated_call_loop:
            return self._agent_output_retry_exhausted(
                receipt=receipt,
                receipts=receipts,
                trace=trace,
                must_reply_on_failure=must_reply_on_failure,
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
                        "correction_hint": (
                            "根据成功回执给出一句与本次结果直接相关的自然确认；"
                            "不要附送下一步清单或重复完整状态。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return None
        if not is_system_beat and (context.directly_addressed or context.is_private):
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
    ) -> GMToolAgentOutcome | None:
        """Execute a bounded batch and resolve its optional terminal choice."""

        batch_receipts: list[dict[str, object]] = []
        batch_failed = False
        seen_batch_calls: set[str] = set()
        calls = [
            call for call in (decision.get("calls") or []) if isinstance(call, dict)
        ]
        dependency_error = self._dependent_batch_error(calls)
        if dependency_error is not None:
            history.append(dependency_error)
            step["protocol_error"] = "DEPENDENT_TOOL_BATCH_REQUIRES_OBSERVATION"
            return None
        isolation_error = self._replace_state_batch_error(calls)
        if isolation_error is not None:
            history.append(isolation_error)
            step["protocol_error"] = "REPLACE_STATE_BATCH_MUST_BE_ISOLATED"
            return None
        batch_scope = GMBatchToolTransaction.begin(
            registry=self.registry, context=context, ledger=ledger,
            receipts=receipts, history=history, calls=calls,
        )

        for batch_index, call in enumerate(calls, start=1):
            tool_name = str(call.get("tool_name") or "").strip()
            arguments = call.get("arguments")
            fingerprint = self._protocol.call_fingerprint(tool_name, arguments)
            if fingerprint in seen_batch_calls:
                step.setdefault("skipped_duplicate_calls", []).append(
                    {
                        "batch_index": batch_index,
                        "tool_name": tool_name,
                        "reason": "同一批次已有完全相同的调用。",
                    }
                )
                continue
            seen_batch_calls.add(fingerprint)
            call_event = ledger.execute(
                tool_name,
                arguments,
                batch_index=batch_index,
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
                    must_reply_on_failure=must_reply_on_failure,
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
            if self._receipt_policy.heartbeat_public_change_committed(context, receipt):
                batch_scope.commit()
                step["batch_receipts"] = batch_receipts
                self.last_trace = trace
                return self._heartbeat_public_change_outcome(receipts, trace)
            if self._receipt_policy.terminal_public_change_committed(
                receipt,
                terminal_public_tools=self._TERMINAL_PUBLIC_TOOLS,
            ):
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
            if not receipt.ok:
                batch_failed = True
                break
        step["batch_receipts"] = batch_receipts
        if batch_failed or ledger.required_retry_pending:
            batch_scope.rollback(batch_receipts, reason="批次中的调用失败或需要修正参数。")
            return None
        batch_scope.commit()
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
            and not any(receipt.ok and receipt.state_changed for receipt in receipts)
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
        reply = str(decision.get("reply") or "").strip()
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
        if locked_reply:
            reply = locked_reply
        if not reply:
            reply = self._receipt_policy.receipt_fallback(receipts)
        if not reply:
            return None
        self.last_trace = trace
        return GMToolAgentOutcome(
            handled=True,
            reply=reply,
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
                        "correction_hint": (
                            "使用terminal_decision=final，并根据成功回执给出一句自然确认；"
                            "不要附送下一步清单。"
                        ),
                        "retryable": True,
                    }
                }
            )
            return None
        if not is_system_beat and (context.directly_addressed or context.is_private):
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
        receipts: list[GMToolReceipt],
        history: list[dict[str, object]],
        step: dict[str, object],
        trace: list[dict[str, object]],
    ) -> tuple[bool, GMToolAgentOutcome | None]:
        """Keep receipt-authorized continuations out of the main agent loop."""

        required = self._receipt_policy.required_followup_tools(receipts)
        allowed = self._receipt_policy.allowed_followup_tools(receipts)
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
        if all(name and name in allowed for name in requested):
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


    def _available_tool_schemas(
        self,
        context: GMToolExecutionContext,
        *,
        receipts: list[GMToolReceipt] | None = None,
        required_retry_tool: str = "",
    ) -> list[dict[str, object]]:
        retry_tool = str(required_retry_tool or "").strip()
        if retry_tool:
            redirected = self._capability_policy.schemas_for_names(
                self.registry,
                context,
                {retry_tool},
            )
            readable = [
                schema
                for schema in self._capability_policy.schemas(self.registry, context)
                if str(schema.get("side_effect") or "") == "read"
            ]
            by_name = {
                str(schema.get("name") or ""): schema
                for schema in [*redirected, *readable]
            }
            return list(by_name.values())
        receipt_list = list(receipts or [])
        required = self._receipt_policy.required_followup_tools(receipt_list)
        allowed = required or self._receipt_policy.allowed_followup_tools(receipt_list)
        if allowed is None:
            # Small custom registries used by extensions and unit tests retain
            # the legacy direct schema surface. FU-GM's full registry exposes a
            # discovery capability and therefore uses a bounded catalog:
            # system beats receive their already narrow trusted set, ordinary
            # messages receive only meta-tools plus domains granted during this
            # one agent transaction.
            if (
                GMCapabilityBroker.DISCOVERY_TOOL not in self.registry._tools
                or not context.metadata.get(
                    "gm_dynamic_capabilities_enabled"
                )
            ):
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
            names.update(
                GMCapabilityBroker.granted_tool_names(context) & phase_tools
            )
            return self._capability_policy.schemas_for_names(
                self.registry,
                context,
                names,
            )
        return self._capability_policy.schemas_for_names(
            self.registry,
            context,
            set(allowed),
        )

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
        if clean_name not in phase_tools:
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
        followup = context.metadata.get(
            self._receipt_policy.REQUIRED_FOLLOWUP_CONTEXT_KEY
        )
        if isinstance(followup, dict) and clean_name in {
            str(item or "").strip()
            for item in list(followup.get("required_tools") or [])
        }:
            return True
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

        mode, overlays = persona_mode_for_context(
            gate_status=context.gate_status,
            metadata=context.metadata,
            state_summary=observed_state,
        )
        if has_receipts:
            overlays = (*overlays, "post_tool")
        persona = self.gm_persona.prompt_block(
            mode,
            overlays=overlays,
            include_examples=not has_receipts,
        )
        if not persona:
            return base_prompt
        return (
            base_prompt
            + "\n\n"
            + persona
            + "\n\n人格只约束公开表达与桌边参与方式；本层仍须严格遵守上述工具协议，并且只输出一个JSON对象。"
        )
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
