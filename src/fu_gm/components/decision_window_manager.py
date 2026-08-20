from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fu_gm.components.world_state import WorldState
from fu_gm.models import DecisionWindow, DecisionWindowStatus


class DecisionWindowManager:
    """Owns the lifecycle of choices that must survive turns and save/load.

    Rule components create windows; integrations and the orchestrator inspect
    them; only the matching responder can resolve them.  Resolved windows stay
    in the audit trail until pruning, while only ``pending`` windows affect play.
    """

    def __init__(self, world_state: WorldState) -> None:
        self.world_state = world_state

    PLAYER_RESPONSE_KINDS = {
        "check_roll_confirmation",
        "reactive_check",
        "zero_hp",
        "npc_fate",
        "critical_opportunity",
        "opportunity_parameter",
        "spell_parameter",
        "skill_judgement",
        "acceleration_benefit",
        "immediate_attack",
        "initiative_support",
    }

    def create(
        self,
        *,
        kind: str,
        owner: str,
        prompt: str = "",
        options: list[dict[str, object]] | None = None,
        scope_kind: str = "scene",
        scope_id: str = "",
        blocking: bool = False,
        allowed_responders: list[str] | None = None,
        action_type: str = "",
        transaction_id: str = "",
        resume_point: str = "",
        payload: dict[str, object] | None = None,
        dedupe_key: str = "",
    ) -> DecisionWindow:
        clean_owner = str(owner or "").strip()
        clean_kind = str(kind or "decision").strip()
        clean_dedupe = str(dedupe_key or "").strip()
        if clean_dedupe:
            existing = self.find_pending(dedupe_key=clean_dedupe)
            if existing is not None:
                existing.prompt = str(prompt or existing.prompt)
                existing.options = list(options or existing.options)
                existing.blocking = bool(blocking)
                existing.action_type = str(action_type or existing.action_type)
                existing.transaction_id = str(transaction_id or existing.transaction_id)
                existing.resume_point = str(resume_point or existing.resume_point)
                existing.scope_kind = str(scope_kind or existing.scope_kind)
                existing.scope_id = str(scope_id or existing.scope_id)
                if allowed_responders is not None:
                    existing.allowed_responders = list(allowed_responders)
                existing.payload.update(dict(payload or {}))
                return existing

        window = DecisionWindow(
            window_id=str(uuid4()),
            kind=clean_kind,
            owner=clean_owner,
            prompt=str(prompt or ""),
            options=list(options or []),
            scope_kind=str(scope_kind or "scene"),
            scope_id=str(scope_id or ""),
            blocking=bool(blocking),
            allowed_responders=list(allowed_responders or ([clean_owner] if clean_owner else [])),
            action_type=str(action_type or ""),
            transaction_id=str(transaction_id or ""),
            resume_point=str(resume_point or ""),
            payload=dict(payload or {}),
            dedupe_key=clean_dedupe,
            created_at=self._now(),
        )
        self.world_state.decision_windows[window.window_id] = window
        return window

    def pending(
        self,
        *,
        kind: str = "",
        owner: str = "",
        blocking_only: bool = False,
        scope_kind: str = "",
        scope_id: str = "",
    ) -> list[DecisionWindow]:
        windows: list[DecisionWindow] = []
        for window in self.world_state.decision_windows.values():
            if window.status != DecisionWindowStatus.PENDING:
                continue
            if kind and window.kind != kind:
                continue
            if owner and window.owner != owner:
                continue
            if blocking_only and not window.blocking:
                continue
            if scope_kind and window.scope_kind != scope_kind:
                continue
            if scope_id and window.scope_id != scope_id:
                continue
            windows.append(window)
        return sorted(windows, key=lambda item: (item.created_at, item.window_id))

    def find_pending(
        self,
        *,
        window_id: str = "",
        kind: str = "",
        owner: str = "",
        dedupe_key: str = "",
    ) -> DecisionWindow | None:
        if window_id:
            window = self.world_state.decision_windows.get(window_id)
            if window is not None and window.status == DecisionWindowStatus.PENDING:
                return window
            return None
        for window in self.pending(kind=kind, owner=owner):
            if dedupe_key and window.dedupe_key != dedupe_key:
                continue
            return window
        return None

    def get(self, window_id: str) -> DecisionWindow | None:
        """Return one persisted window regardless of lifecycle status."""

        return self.world_state.decision_windows.get(str(window_id or "").strip())

    def has_blocking(self, *, owner: str = "") -> bool:
        return bool(self.pending(owner=owner, blocking_only=True))

    def awaiting_player_response(self, *, owner: str = "") -> list[DecisionWindow]:
        """Choices a heartbeat must not talk over.

        Result-changing post-check choices remain blocking until the check's
        controller explicitly invokes one or accepts the rolled result. Table
        conversation may continue, but no new rules transaction may overtake
        the unsettled check.
        """

        windows = [
            window
            for window in self.pending(owner=owner)
            if window.blocking or window.kind in self.PLAYER_RESPONSE_KINDS
        ]
        return sorted(windows, key=self._response_order_key)

    def resolve(
        self,
        *,
        window_id: str = "",
        kind: str = "",
        owner: str = "",
        responder: str = "",
        resolution: dict[str, object] | None = None,
    ) -> DecisionWindow:
        window = self.find_pending(window_id=window_id, kind=kind, owner=owner)
        if window is None:
            raise ValueError("没有找到可处理的待决窗口。")
        clean_responder = str(responder or owner or "").strip()
        if window.allowed_responders and clean_responder not in window.allowed_responders:
            raise ValueError(f"{clean_responder or '当前玩家'} 不能替【{window.owner}】处理这个选择。")
        window.status = DecisionWindowStatus.RESOLVED
        window.resolved_at = self._now()
        window.resolution = dict(resolution or {})
        return window

    def settle_selection(
        self,
        *,
        window_id: str,
        responder: str,
        resolution: dict[str, object] | None = None,
        sibling_ids: list[str] | None = None,
        sibling_reason: str = "another_choice_selected",
        allow_superseded: bool = False,
    ) -> DecisionWindow:
        """Resolve one selected choice and expire its alternatives atomically.

        A check replay may supersede its source windows before the replayed
        result returns. ``allow_superseded`` records that successfully selected
        source as resolved without touching windows created by the new result.
        """

        clean_id = str(window_id or "").strip()
        window = self.get(clean_id)
        if window is None:
            raise ValueError("没有找到可处理的待决窗口。")
        clean_responder = str(responder or window.owner or "").strip()
        if window.allowed_responders and clean_responder not in window.allowed_responders:
            raise ValueError(f"{clean_responder or '当前玩家'} 不能替【{window.owner}】处理这个选择。")
        if window.status != DecisionWindowStatus.PENDING:
            prior_reason = str(window.resolution.get("reason") or "")
            can_settle = allow_superseded and prior_reason == "new_check_replaced_previous_window"
            if not can_settle:
                raise ValueError("这个待决窗口已经结束，不能重复处理。")

        now = self._now()
        window.status = DecisionWindowStatus.RESOLVED
        window.resolved_at = now
        window.resolution = dict(resolution or {})

        for sibling_id in sibling_ids or []:
            clean_sibling_id = str(sibling_id or "").strip()
            if not clean_sibling_id or clean_sibling_id == clean_id:
                continue
            sibling = self.get(clean_sibling_id)
            if sibling is None:
                continue
            prior_reason = str(sibling.resolution.get("reason") or "")
            if sibling.status == DecisionWindowStatus.PENDING or (
                allow_superseded and prior_reason == "new_check_replaced_previous_window"
            ):
                sibling.status = DecisionWindowStatus.EXPIRED
                sibling.resolved_at = now
                sibling.resolution = {"reason": sibling_reason}
        return window

    def cancel_matching(
        self,
        *,
        kind: str = "",
        owner: str = "",
        scope_kind: str = "",
        scope_id: str = "",
        reason: str = "",
        status: DecisionWindowStatus = DecisionWindowStatus.CANCELLED,
    ) -> list[DecisionWindow]:
        cancelled: list[DecisionWindow] = []
        for window in self.pending(kind=kind, owner=owner, scope_kind=scope_kind, scope_id=scope_id):
            window.status = status
            window.resolved_at = self._now()
            window.resolution = {"reason": reason} if reason else {}
            cancelled.append(window)
        return cancelled

    def cancel_nonblocking(
        self,
        *,
        kinds: set[str] | None = None,
        owner: str = "",
        reason: str = "",
    ) -> list[DecisionWindow]:
        """Expire optional choices once play has moved to a new transaction."""

        allowed_kinds = {str(item).strip() for item in (kinds or set()) if str(item).strip()}
        cancelled: list[DecisionWindow] = []
        for window in self.pending(owner=owner):
            if window.blocking:
                continue
            if allowed_kinds and window.kind not in allowed_kinds:
                continue
            window.status = DecisionWindowStatus.EXPIRED
            window.resolved_at = self._now()
            window.resolution = {"reason": reason} if reason else {}
            cancelled.append(window)
        return cancelled

    def expire_scope(self, scope_kind: str, scope_id: str, *, reason: str = "scope_ended") -> list[DecisionWindow]:
        return self.cancel_matching(
            scope_kind=scope_kind,
            scope_id=scope_id,
            reason=reason,
            status=DecisionWindowStatus.EXPIRED,
        )

    def expire_ephemeral(self, *, reason: str = "runtime_resumed") -> list[DecisionWindow]:
        """Expire optional rights whose rollback journal exists only in memory."""

        expired: list[DecisionWindow] = []
        for window in self.pending():
            if not bool(window.payload.get("ephemeral_same_runtime")):
                continue
            window.status = DecisionWindowStatus.EXPIRED
            window.resolved_at = self._now()
            window.resolution = {"reason": reason}
            expired.append(window)
        return expired

    def public_summary(
        self,
        *,
        include_suppressed: bool = False,
    ) -> list[dict[str, object]]:
        return [
            {
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "prompt": window.prompt,
                "options": list(window.options),
                "blocking": window.blocking,
                "action_type": window.action_type,
                "transaction_id": window.transaction_id,
                "resume_point": window.resume_point,
                "label": str(window.payload.get("label") or ""),
                "roll_success": window.payload.get("roll_success"),
                "allowed_responders": list(window.allowed_responders),
                "allowed_speakers": self._allowed_speakers(window),
                "scope_kind": window.scope_kind,
                "scope_id": window.scope_id,
                "response_priority": self._response_priority(window),
            }
            for window in sorted(self.pending(), key=self._response_order_key)
            if include_suppressed
            or not bool(window.payload.get("suppress_public_prompt"))
        ]

    @staticmethod
    def _response_priority(window: DecisionWindow) -> int:
        """Order interdependent choices without changing their rule outcome.

        A supporter using ``予以信任`` can replace the checked character's
        dice before that character accepts or rerolls the provisional result.
        The supporter's choice therefore has to reach the table first even
        though the checked character's own windows were created earlier.
        Other decisions retain chronological order unless a rules component
        supplies an explicit ``response_priority`` in its private payload.
        """

        explicit = window.payload.get("response_priority")
        if explicit is not None:
            try:
                return int(explicit)
            except (TypeError, ValueError):
                pass
        label = str(window.payload.get("label") or window.payload.get("skill") or "")
        if window.kind == "skill_parameter" and label == "予以信任":
            return 10
        return 100

    @classmethod
    def _response_order_key(cls, window: DecisionWindow) -> tuple[int, str, str]:
        return (cls._response_priority(window), window.created_at, window.window_id)

    def _allowed_speakers(self, window: DecisionWindow) -> list[str]:
        responders = set(window.allowed_responders or ([window.owner] if window.owner else []))
        speakers = set(responders)
        for key, draft in self.world_state.world_profile.hero_drafts.items():
            hero_name = str(draft.hero_name or key or "").strip()
            if hero_name not in responders:
                continue
            for value in (key, draft.player_name, draft.hero_name):
                clean = str(value or "").strip()
                if clean:
                    speakers.add(clean)
        return sorted(speakers)

    def prune(self, *, keep_resolved: int = 100) -> None:
        terminal = [
            window
            for window in self.world_state.decision_windows.values()
            if window.status != DecisionWindowStatus.PENDING
        ]
        if len(terminal) <= keep_resolved:
            return
        terminal.sort(key=lambda item: (item.resolved_at or item.created_at, item.window_id))
        for window in terminal[: len(terminal) - keep_resolved]:
            self.world_state.decision_windows.pop(window.window_id, None)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
