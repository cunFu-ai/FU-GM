from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Protocol

from fu_gm.models import Action


class StructuredTurnHost(Protocol):
    expressor: Any
    interceptor: Any

    def build_panel(self, recent_chat: str) -> Any: ...

    def _with_pending_conflict_assists(self, action: Action) -> Action: ...

    def _settle_bound_scene_condition(self, resolution: Any) -> None: ...

    def _auto_advance_conflict_turn(self, action: Action, resolution: Any) -> None: ...

    def _auto_advance_free_scene_action(
        self,
        action: Action,
        resolution: Any,
        *,
        actor_hint: str = "",
    ) -> None: ...

    def _complete_resolved_player_turn(self, **kwargs: Any) -> str: ...


class StructuredTurnExecutor:
    """Execute an already-semantic, typed player action exactly once.

    The GM tool agent owns intent and actor identity. This executor deliberately
    skips natural-language interpretation entirely; the hard-rule
    interceptor remains the sole validator of turn order and mechanics.
    """

    def __init__(self, host: StructuredTurnHost) -> None:
        self.host = host

    def execute(
        self,
        action: Action,
        *,
        player_message: str,
        recent_public_context: str = "",
        speaker: str = "",
        route_decision: dict[str, object] | None = None,
    ) -> str:
        clean_player_message = str(player_message or "").strip()
        clean_speaker = str(speaker or "").strip()
        current_public_line = (
            f"{clean_speaker}: {clean_player_message}"
            if clean_speaker and clean_player_message
            else clean_player_message
        )
        recent_chat = "\n".join(
            item
            for item in (
                str(recent_public_context or "").strip(),
                current_public_line,
            )
            if item
        )
        total_started = time.monotonic()
        span: dict[str, object] = {
            "kind": "structured_player_turn",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "input_chars": len(str(recent_chat)),
            "action_type": action.action_type.value,
            "core_gm_authority": "gm_tool_agent",
            "expressor": self.host.expressor.__class__.__name__,
        }
        route = dict(route_decision) if isinstance(route_decision, dict) else {}
        route.setdefault("actor", str(action.parameters.get("actor") or "").strip())

        phase_started = time.monotonic()
        panel = self.host.build_panel(recent_chat)
        span["build_panel_ms"] = int((time.monotonic() - phase_started) * 1000)

        phase_started = time.monotonic()
        action = self.host._with_pending_conflict_assists(action)
        resolution = self.host.interceptor.resolve(action)
        self.host._settle_bound_scene_condition(resolution)
        committed_source = resolution.payload.get("committed_source_action")
        effective_action = committed_source if isinstance(committed_source, Action) else action
        self.host._auto_advance_conflict_turn(effective_action, resolution)
        self.host._auto_advance_free_scene_action(
            effective_action,
            resolution,
            actor_hint=str(action.parameters.get("actor") or "").strip(),
        )
        span["rules_ms"] = int((time.monotonic() - phase_started) * 1000)

        # _complete_resolved_player_turn owns the one and only post-check NPC
        # follow-up, commit, expression, publication and audit sequence.
        return self.host._complete_resolved_player_turn(
            player_message=clean_player_message,
            recent_chat=recent_chat,
            route_decision=route,
            panel=panel,
            action=action,
            resolution=resolution,
            recovery=[],
            span=span,
            total_started=total_started,
        )
