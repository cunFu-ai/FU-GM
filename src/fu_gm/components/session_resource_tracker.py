from __future__ import annotations

from fu_gm.components.character_manager import CharacterManager
from fu_gm.models import Character, SessionEpisodeProgress


class SessionResourceTracker:
    """Measure resource pressure from committed character state.

    The snapshot lives on ``SessionEpisodeProgress`` so save/load in the
    middle of a session does not erase costs already paid.  We count actual
    decreases between authoritative states rather than trying to infer costs
    from player wording.
    """

    _CURRENT_FIELDS = ("hp", "mp", "inventory_points", "fabula_points")

    def __init__(self, characters: CharacterManager) -> None:
        self.characters = characters

    def begin(self, progress: SessionEpisodeProgress) -> None:
        if not progress.resource_snapshot:
            progress.resource_snapshot = self._snapshot()

    def observe(self, progress: SessionEpisodeProgress) -> None:
        current = self._snapshot()
        if not progress.resource_snapshot:
            progress.resource_snapshot = current
            return

        normalized_spend = 0.0
        pc_count = max(1, len(current))
        for name, values in current.items():
            previous = progress.resource_snapshot.get(name)
            if previous is None:
                continue
            character = self.characters.get(name)
            for field_name in self._CURRENT_FIELDS:
                before = int(previous.get(field_name, values.get(field_name, 0)) or 0)
                after = int(values.get(field_name, 0) or 0)
                if after >= before:
                    continue
                progress.resource_spend_events += 1
                normalized_spend += (before - after) / self._capacity(character, field_name)

        if normalized_spend:
            progress.resource_pressure_ratio = min(
                1.0,
                float(progress.resource_pressure_ratio) + normalized_spend / pc_count,
            )
        progress.resource_snapshot = current

    def record_change(
        self,
        progress: SessionEpisodeProgress,
        *,
        character_name: str,
        field_name: str,
        before: int,
        after: int,
    ) -> None:
        """Record a resource event immediately, including spend-and-recover actions."""

        if field_name not in self._CURRENT_FIELDS:
            return
        if not self.characters.exists(character_name):
            return
        character = self.characters.get(character_name)
        if "pc" not in character.traits:
            return
        self.begin(progress)
        if after < before:
            progress.resource_spend_events += 1
            pc_count = max(1, len(progress.resource_snapshot))
            progress.resource_pressure_ratio = min(
                1.0,
                float(progress.resource_pressure_ratio)
                + ((before - after) / self._capacity(character, field_name)) / pc_count,
            )
        progress.resource_snapshot.setdefault(character_name, {})[field_name] = int(after)

    def _snapshot(self) -> dict[str, dict[str, int]]:
        return {
            character.name: {
                field_name: int(getattr(character, field_name, 0) or 0)
                for field_name in self._CURRENT_FIELDS
            }
            for character in self.characters.all()
            if "pc" in character.traits
        }

    @staticmethod
    def _capacity(character: Character, field_name: str) -> int:
        if field_name == "hp":
            return max(1, int(character.max_hp or 0))
        if field_name == "mp":
            return max(1, int(character.max_mp or 0))
        if field_name == "inventory_points":
            return max(1, int(character.max_inventory_points or 6))
        return max(3, int(character.fabula_points or 0))
