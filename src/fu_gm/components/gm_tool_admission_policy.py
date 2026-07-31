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
        }
    )

    def __init__(self, host: Any) -> None:
        self.host = host

    def __call__(
        self,
        definition: GMToolDefinition,
        _arguments: dict[str, object],
        context: GMToolExecutionContext,
    ) -> GMToolReceipt | None:
        supervisor = getattr(self.host, "gm_supervisor", None)
        if supervisor is not None:
            circuit_error = supervisor.admission_error(definition, context)
            if circuit_error is not None:
                return circuit_error
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
        return GMToolReceipt.failure(
            definition.name,
            "BLOCKING_DECISION_PENDING",
            "当前有必须先由对应玩家或GM处理的待决选择，不能先推进其他规则状态。",
            (
                "读取pending_decisions：玩家窗口使用resolve_rule_window，"
                "GM大失败机会使用resolve_gm_opportunity；"
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
