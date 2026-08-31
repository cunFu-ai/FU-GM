from __future__ import annotations

from typing import Any, Iterable

from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolPacingEvent,
    GMToolReceipt,
)


class GMToolPacingObserver:
    """Commit typed tool evidence to the active episode exactly once.

    Domain tools may perform several authoritative writes for one incoming
    message.  Aggregating their evidence here preserves the table's unit of
    time: one player message is at most one meaningful player turn.
    """

    _TEXT_FIELDS = (
        "consequence",
        "local_payoff",
        "reveal",
        "climax",
        "opposition_move",
        "callback_to_previous",
    )

    def observe(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        receipts: Iterable[GMToolReceipt],
    ) -> dict[str, object]:
        receipt_list = list(receipts)
        events = [
            event
            for receipt in receipt_list
            if receipt.ok
            for event in receipt.pacing_events
            if isinstance(event, GMToolPacingEvent) and event.meaningful
        ]
        events.extend(self._action_round_pressure_events(receipt_list))
        if not events:
            return {}

        merged = self._merge(events, context=context)
        progress = runtime.app.campaign_pacing_manager.observe_turn(**merged)
        tracker = getattr(runtime.app, "session_episode_tracker", None)
        resource_tracker = getattr(tracker, "resource_tracker", None)
        if resource_tracker is not None:
            resource_tracker.observe(progress)
        return {
            "event_count": len(events),
            "player_action": bool(merged["player_action"]),
            "session_number": int(getattr(progress, "session_number", 0) or 0),
            "meaningful_turns": int(getattr(progress, "meaningful_turns", 0) or 0),
            "stage": str(getattr(progress, "stage", "") or ""),
            "closure_ready": bool(getattr(progress, "closure_ready", False)),
            "merged_event": {
                key: value
                for key, value in merged.items()
                if value not in ("", False, None)
            },
        }

    @classmethod
    def _action_round_pressure_events(
        cls,
        receipts: Iterable[GMToolReceipt],
    ) -> list[GMToolPacingEvent]:
        """Recover pressure fulfillment emitted by direct GM tool actions."""

        events: list[GMToolPacingEvent] = []
        seen: set[tuple[str, str]] = set()
        for receipt in receipts:
            if not receipt.ok:
                continue
            action_round = receipt.result.get("action_round")
            if not isinstance(action_round, dict):
                continue
            settled = action_round.get("settled_pressure_clocks")
            if not isinstance(settled, list):
                continue
            for item in settled:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("clock_name") or "").strip()
                consequence = str(item.get("consequence") or "").strip()
                key = (name, consequence)
                if not name or key in seen:
                    continue
                seen.add(key)
                public_change = consequence or f"命刻【{name}】的威胁已经兑现。"
                events.append(
                    GMToolPacingEvent(
                        consequence=public_change,
                        reversal=True,
                        opposition_move=public_change,
                        public_image=public_change,
                        local_question_changed=True,
                    )
                )
        return events

    @classmethod
    def _merge(
        cls,
        events: list[GMToolPacingEvent],
        *,
        context: GMToolExecutionContext,
    ) -> dict[str, object]:
        player_action = any(event.player_action for event in events)
        action_summary = cls._first(
            event.action_summary for event in events if event.player_action
        )
        if player_action and not action_summary:
            action_summary = str(context.metadata.get("current_message") or "").strip()

        merged: dict[str, object] = {
            "player_action": player_action,
            "action_summary": action_summary[:500],
            "public_image": cls._first(event.public_image for event in events)[:500],
            "reversal": any(event.reversal for event in events),
            "local_question_changed": any(
                event.local_question_changed for event in events
            ),
            "local_question_resolved": any(
                event.local_question_resolved for event in events
            ),
            "scene_resolved": any(event.scene_resolved for event in events),
            "session_question_resolved": any(
                event.session_question_resolved for event in events
            ),
            "session_close_requested": any(
                event.session_close_requested for event in events
            ),
            "deliberate_cliffhanger": any(
                event.deliberate_cliffhanger for event in events
            ),
            "signature_image_evolved": any(
                event.signature_image_evolved for event in events
            ) or bool(
                context.metadata.get("heartbeat_require_signature_image_evolution")
                and cls._first(event.public_image for event in events)
            ),
            "opening_signature_realized": cls._first(
                event.opening_signature_realized for event in events
            )[:500],
            "awaits_player_response": any(
                event.awaits_player_response for event in events
            ),
            "closure_payoff": any(event.closure_payoff for event in events),
            "next_session_hook": cls._first(
                event.next_session_hook for event in events
            )[:500],
            "gm_beat_purpose": cls._first(
                event.gm_beat_purpose for event in events
            )[:80],
        }
        for field_name in cls._TEXT_FIELDS:
            merged[field_name] = cls._join_unique(
                getattr(event, field_name) for event in events
            )[:500]
        return merged

    @staticmethod
    def _first(values: Iterable[str]) -> str:
        return next(
            (
                " ".join(str(value or "").split()).strip()
                for value in values
                if " ".join(str(value or "").split()).strip()
            ),
            "",
        )

    @staticmethod
    def _join_unique(values: Iterable[str]) -> str:
        result: list[str] = []
        for value in values:
            clean = " ".join(str(value or "").split()).strip()
            if clean and clean not in result:
                result.append(clean)
        return "；".join(result)


__all__ = ["GMToolPacingObserver"]
