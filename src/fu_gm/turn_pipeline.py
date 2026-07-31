from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fu_gm.models import ActionResolution


@dataclass(frozen=True)
class TurnReplyContext:
    recent_chat: str
    prior_public_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnReplyStage:
    name: str
    transform: Callable[[str, ActionResolution, TurnReplyContext], str]


class TurnReplyPipeline:
    """Apply named public-reply policies in one observable order.

    Rule resolution remains immutable here.  A stage may only transform the
    player-facing string, which prevents prose cleanup from leaking back into
    combat, clocks, or persisted decisions.
    """

    def __init__(self, stages: list[TurnReplyStage] | None = None) -> None:
        self._stages = list(stages or [])

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self._stages)

    def run(
        self,
        reply: str,
        resolution: ActionResolution,
        context: TurnReplyContext,
    ) -> tuple[str, list[str]]:
        current = str(reply or "")
        changed: list[str] = []
        for stage in self._stages:
            before = current
            current = str(stage.transform(current, resolution, context) or "")
            if current != before:
                changed.append(stage.name)
        return current, changed
