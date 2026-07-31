from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from fu_gm.components.skill_lifecycle_coordinator import SkillLifecycleOutcome


@dataclass
class SkillLifecycleEventBatch:
    records: list[dict[str, object]] = field(default_factory=list)
    windows: list[dict[str, object]] = field(default_factory=list)


class SkillLifecycleEventBuffer:
    """Request-local skill events awaiting attachment to a resolution.

    Character resource listeners can fire deep inside a rules transaction.
    Keeping their output on the interceptor instance lets concurrent messages
    contaminate each other.  Context-local batches preserve the existing
    synchronous API while making nested and concurrent resolutions isolated.
    """

    def __init__(self) -> None:
        self._current: ContextVar[SkillLifecycleEventBatch | None] = ContextVar(
            "fu_gm_skill_lifecycle_event_batch",
            default=None,
        )

    @contextmanager
    def transaction(self) -> Iterator[SkillLifecycleEventBatch]:
        batch = SkillLifecycleEventBatch()
        token = self._current.set(batch)
        try:
            yield batch
        finally:
            self._current.reset(token)

    def capture(self, outcome: SkillLifecycleOutcome) -> None:
        batch = self._current.get()
        if batch is None:
            # Lifecycle callbacks are expected inside a rules transaction, but
            # a detached administrative resource edit should still be safe.
            batch = SkillLifecycleEventBatch()
            self._current.set(batch)
        batch.records.extend(dict(record) for record in outcome.records)
        batch.windows.extend(dict(window) for window in outcome.windows)

    def drain(self) -> SkillLifecycleEventBatch:
        batch = self._current.get()
        if batch is None:
            return SkillLifecycleEventBatch()
        drained = SkillLifecycleEventBatch(
            records=list(batch.records),
            windows=list(batch.windows),
        )
        batch.records.clear()
        batch.windows.clear()
        return drained
