from __future__ import annotations

from typing import Any

from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolReceipt,
)


class GMToolDecisionAdmissionPolicy:
    """Prevent unrelated writes from overtaking a blocking player choice."""

    _ALLOWED_DURING_BLOCKING = frozenset(
        {
            "resolve_rule_window",
            "resolve_gm_opportunity",
            "save_campaign",
            "load_campaign",
            "create_campaign",
            "delete_save",
            "pause_session",
            "set_player_attendance",
            "record_safety_boundary",
            "set_session_zero_nudge_preference",
            "pause_session_zero_nudges",
            "roll_dice",
            "delegate_background_task",
            "list_background_tasks",
            "get_background_task",
            "cancel_background_task",
            "resume_background_task",
        }
    )
    _SAME_EVENT_PASSIVE_RESPONSE_TOOLS = frozenset(
        {
            "decide_npc_response",
            "decide_collective_response",
        }
    )
    _PASSIVE_RESPONSE_TAGS = frozenset(
        {
            "direct_answer",
            "fact",
            "nonverbal",
        }
    )

    def __init__(self, host: Any) -> None:
        self.host = host

    def __call__(
        self,
        definition: GMToolDefinition,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> GMToolReceipt | None:
        supervisor = getattr(self.host, "gm_supervisor", None)
        if supervisor is not None:
            circuit_error = supervisor.admission_error(definition, context)
            if circuit_error is not None:
                return circuit_error
        if definition.name == "perform_check_action":
            followup = context.metadata.get(
                "_gm_agent_required_followup_context"
            )
            followup_state = followup if isinstance(followup, dict) else {}
            required = {
                str(item or "").strip()
                for item in list(
                    followup_state.get("required_tools") or []
                )
                if str(item or "").strip()
            }
            if "perform_check_action" not in required:
                return GMToolReceipt.failure(
                    definition.name,
                    "CHECK_DECLARATION_REQUIRED",
                    "普通玩家检定必须先建立待掷声明，不能在同一句中直接掷骰。",
                    (
                        "改用declare_check_action确定中文属性、难度等级和后台结果契约；"
                        "玩家确认后由check_roll_confirmation窗口继续。"
                    ),
                )
        if definition.name == "resolve_rule_window":
            same_message_error = self._same_message_window_resolution_error(
                arguments,
                context,
            )
            if same_message_error is not None:
                return same_message_error
        if (
            definition.side_effect == "read"
            or definition.name in self._ALLOWED_DURING_BLOCKING
        ):
            return None
        runtime = self.host._runtime(context.campaign_id)
        windows = runtime.app.interceptor.decision_window_manager.pending(
            blocking_only=True
        )
        if not windows:
            return None
        if self._is_same_event_passive_response(
            definition.name,
            arguments,
            context,
            windows,
        ):
            return None
        if self._is_foreign_roll_confirmation_npc_response(
            definition.name,
            context,
            windows,
        ):
            return None
        return GMToolReceipt.failure(
            definition.name,
            "BLOCKING_DECISION_PENDING",
            "当前有必须先由对应玩家或GM处理的待决选择，不能先推进其他规则状态。",
            (
                "读取pending_decisions：玩家窗口使用resolve_rule_window，"
                "由GM操控的机会使用resolve_gm_opportunity；"
                "若当前发言者不是合法回应者，就等待对应玩家。"
            ),
            result={
                "pending_windows": [
                    {
                        "window_id": window.window_id,
                        "kind": window.kind,
                        "owner": window.owner,
                        "allowed_responders": list(window.allowed_responders),
                    }
                    for window in windows
                ]
            },
        )

    def _is_foreign_roll_confirmation_npc_response(
        self,
        tool_name: str,
        context: GMToolExecutionContext,
        windows: list[object],
    ) -> bool:
        """Let another player finish a direct NPC exchange while a roll waits.

        A check-roll confirmation has not produced a speculative rules result or
        snapshot yet. Resolving another player's direct NPC conversation therefore
        does not answer, cancel or mutate the first player's decision window. More
        consequential tool classes remain blocked.
        """

        if tool_name not in self._SAME_EVENT_PASSIVE_RESPONSE_TOOLS or not windows:
            return False
        if any(
            str(getattr(window, "kind", "") or "")
            != "check_roll_confirmation"
            for window in windows
        ):
            return False
        runtime = self.host._runtime(context.campaign_id)
        control_map_provider = getattr(
            self.host,
            "_player_character_control_map",
            None,
        )
        if not callable(control_map_provider):
            return False
        controlled = {
            str(item or "").strip()
            for item in list(
                control_map_provider(runtime).get(context.speaker, [])
            )
            if str(item or "").strip()
        }
        if not controlled:
            return False
        owners = {
            str(getattr(window, "owner", "") or "").strip()
            for window in windows
            if str(getattr(window, "owner", "") or "").strip()
        }
        return bool(owners and controlled.isdisjoint(owners))

    def _same_message_window_resolution_error(
        self,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> GMToolReceipt | None:
        """A GM may not manufacture the player's answer to a fresh window."""

        source_event_id = str(
            context.metadata.get("source_event_id") or ""
        ).strip()
        window_id = str(arguments.get("window_id") or "").strip()
        if not source_event_id or not window_id:
            return None
        runtime = self.host._runtime(context.campaign_id)
        window = runtime.app.interceptor.decision_window_manager.get(window_id)
        if window is None:
            return None
        payload = window.payload if isinstance(window.payload, dict) else {}
        if str(payload.get("source_event_id") or "").strip() != source_event_id:
            return None
        return GMToolReceipt.failure(
            "resolve_rule_window",
            "PLAYER_CONFIRMATION_REQUIRES_NEW_MESSAGE",
            "这个待决窗口刚由当前消息建立，不能由GM替玩家在同一事务中回答。",
            (
                "停止当前工具循环并把窗口提示发给玩家；"
                "只有allowed_responders之后发来的新消息才能调用resolve_rule_window。"
            ),
            result={
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "allowed_responders": list(window.allowed_responders),
            },
        )

    @classmethod
    def _is_same_event_passive_response(
        cls,
        tool_name: str,
        arguments: dict[str, object],
        context: GMToolExecutionContext,
        windows: list[object],
    ) -> bool:
        """Allow one non-escalating reply caused before a same-message window.

        A compound player message can both speak to an NPC and declare an
        uncertain action.  If the check tool happens to run first, its
        confirmation window must not erase the NPC's already-triggered simple
        acknowledgement.  The exception is deliberately narrow: it only
        applies to windows created by this exact source event and cannot open a
        gate, promise, request, movement, or another rules transaction.
        """

        if tool_name not in cls._SAME_EVENT_PASSIVE_RESPONSE_TOOLS:
            return False
        source_event_id = str(
            context.metadata.get("source_event_id") or ""
        ).strip()
        if not source_event_id or not windows:
            return False
        for window in windows:
            payload = getattr(window, "payload", {})
            payload = payload if isinstance(payload, dict) else {}
            if str(payload.get("source_event_id") or "").strip() != source_event_id:
                return False

        forbidden_fields = (
            "condition_id",
            "pending_question_id",
            "commitment_id",
            "position_note",
            "introduced_npcs",
            "response_addressee",
        )
        if any(arguments.get(field) not in (None, "", [], {}) for field in forbidden_fields):
            return False
        if arguments.get("join_current_focus") is True:
            return False
        for field in (
            "condition_outcome",
            "proposal_outcome",
            "commitment_outcome",
            "promise_kind",
        ):
            value = str(arguments.get(field) or "none").strip().lower()
            if value not in {"", "none"}:
                return False
        for segment in list(arguments.get("public_segments") or []):
            if not isinstance(segment, dict):
                continue
            tags = {
                str(tag or "").strip()
                for tag in list(segment.get("tags") or [])
                if str(tag or "").strip()
            }
            if tags - cls._PASSIVE_RESPONSE_TAGS:
                return False
        return True
