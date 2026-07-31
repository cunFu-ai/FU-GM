from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class SceneCastCoordinator:
    """Compose scene rosters without discarding prepared or established cast."""

    @classmethod
    def compose(
        cls,
        player_characters: Iterable[object],
        *,
        opportunity: Any | None = None,
        established: Iterable[object] = (),
    ) -> list[str]:
        return cls._dedupe(
            [
                *player_characters,
                *established,
                *cls.opportunity_npcs(opportunity),
            ]
        )

    @classmethod
    def opportunity_npcs(cls, opportunity: Any | None) -> list[str]:
        if opportunity is None:
            return []
        return cls._dedupe(
            [
                *list(getattr(opportunity, "required_npc_names", []) or []),
                *list(getattr(opportunity, "npc_names", []) or []),
            ]
        )

    @staticmethod
    def _dedupe(values: Iterable[object]) -> list[str]:
        result: list[str] = []
        for value in values:
            name = str(value or "").strip()
            if name and name not in result:
                result.append(name)
        return result
