from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import Action, ActionResolution, PendingCheckBatch, RollOutcome


class CheckBatchManager:
    """Persist multi-actor checks until every participant's roll is final.

    Individual rolls still use the ordinary post-check transaction. This manager
    only groups their final outcomes and prevents the shared consequence from
    being committed while a reroll or opportunity remains unresolved.
    """

    def __init__(
        self,
        world_state: WorldState,
        decisions: DecisionWindowManager,
    ) -> None:
        self.world_state = world_state
        self.decisions = decisions

    def begin(
        self,
        *,
        kind: str,
        source_action: Action,
        actor_order: list[str],
        roles: dict[str, str] | None = None,
        batch_id: str = "",
    ) -> PendingCheckBatch:
        clean_actors = list(
            dict.fromkeys(str(actor or "").strip() for actor in actor_order if str(actor or "").strip())
        )
        if not clean_actors:
            raise ValueError("多人检定至少需要一名参与者。")
        existing = self.pending(kind=kind)
        if existing:
            raise ValueError(f"已有尚未定稿的【{kind}】多人检定。")
        batch = PendingCheckBatch(
            batch_id=str(batch_id or uuid4()),
            kind=str(kind or "team_check").strip(),
            source_action_type=source_action.action_type.value,
            source_parameters=deepcopy(source_action.parameters),
            actor_order=clean_actors,
            roles={
                str(actor): str(role)
                for actor, role in dict(roles or {}).items()
                if str(actor).strip()
            },
            created_at=self._now(),
        )
        self.world_state.pending_check_batches[batch.batch_id] = batch
        return batch

    def get(self, batch_id: str) -> PendingCheckBatch | None:
        return self.world_state.pending_check_batches.get(str(batch_id or "").strip())

    def pending(self, *, kind: str = "") -> list[PendingCheckBatch]:
        batches = [
            batch
            for batch in self.world_state.pending_check_batches.values()
            if batch.status == "pending" and (not kind or batch.kind == kind)
        ]
        return sorted(batches, key=lambda item: (item.created_at, item.batch_id))

    def observe_resolution(self, resolution: ActionResolution) -> PendingCheckBatch | None:
        """Record a final sub-roll; provisional rolls never enter the batch."""

        source_action = resolution.payload.get("committed_source_action")
        if not isinstance(source_action, Action):
            source_action = resolution.action
        batch_id = str(source_action.parameters.get("_check_batch_id") or "").strip()
        if not batch_id or resolution.payload.get("check_result_provisional"):
            return None
        batch = self.get(batch_id)
        if batch is None or batch.status != "pending":
            return None
        outcome = resolution.payload.get("roll")
        if not isinstance(outcome, RollOutcome):
            return None
        actor = str(outcome.actor or "").strip()
        if actor not in batch.actor_order:
            return None
        batch.rolls[actor] = deepcopy(outcome)
        return batch

    def ready(self, batch: PendingCheckBatch) -> bool:
        if batch.status != "pending":
            return False
        if any(actor not in batch.rolls for actor in batch.actor_order):
            return False
        return not self.has_blocking_window(batch)

    def has_blocking_window(self, batch: PendingCheckBatch) -> bool:
        return any(
            window.blocking and window.transaction_id == batch.batch_id
            for window in self.decisions.pending()
        )

    def next_actor(self, batch: PendingCheckBatch) -> str:
        if batch.status != "pending" or self.has_blocking_window(batch):
            return ""
        return next(
            (
                actor
                for actor in batch.actor_order
                if actor not in batch.rolls
            ),
            "",
        )

    def reset_round(self, batch: PendingCheckBatch) -> None:
        current = self.get(batch.batch_id)
        if current is None or current.status != "pending":
            raise ValueError("这个多人检定已经结束或不存在。")
        if current.rolls:
            current.roll_history.append(deepcopy(current.rolls))
        current.rolls.clear()

    def ready_batches(self) -> list[PendingCheckBatch]:
        return [batch for batch in self.pending() if self.ready(batch)]

    def complete(
        self,
        batch: PendingCheckBatch,
        *,
        result: dict[str, object],
    ) -> PendingCheckBatch:
        current = self.get(batch.batch_id)
        if current is None or current.status != "pending":
            raise ValueError("这个多人检定已经结束或不存在。")
        current.status = "completed"
        current.result = deepcopy(result)
        current.completed_at = self._now()
        self.world_state.pending_check_batches.pop(current.batch_id, None)
        self.world_state.check_batch_history.append(current)
        if len(self.world_state.check_batch_history) > 100:
            del self.world_state.check_batch_history[:-100]
        return current

    def cancel(self, batch_id: str, *, reason: str) -> PendingCheckBatch | None:
        batch = self.get(batch_id)
        if batch is None:
            return None
        batch.status = "cancelled"
        batch.result = {"reason": str(reason or "cancelled")}
        batch.completed_at = self._now()
        self.world_state.pending_check_batches.pop(batch.batch_id, None)
        self.world_state.check_batch_history.append(batch)
        return batch

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
