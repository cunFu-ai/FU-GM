from __future__ import annotations

import re
from difflib import SequenceMatcher

from fu_gm.models import SessionEpisodeProgress


class EpisodeMomentumTracker:
    """Track table stagnation from authoritative outcomes, not elapsed time."""

    @classmethod
    def observe_player_action(
        cls,
        progress: SessionEpisodeProgress,
        *,
        action_summary: str,
        material_change: bool,
    ) -> None:
        signature = cls._normalize(action_summary)
        if material_change:
            progress.stagnant_player_turns = 0
        else:
            repeated = any(
                cls._similar(signature, prior)
                for prior in progress.recent_action_signatures[-2:]
            )
            progress.stagnant_player_turns += 2 if repeated else 1
        progress.max_stagnant_player_turns = max(
            progress.max_stagnant_player_turns,
            progress.stagnant_player_turns,
        )
        if signature:
            progress.recent_action_signatures.append(signature[:240])
            if len(progress.recent_action_signatures) > 6:
                del progress.recent_action_signatures[:-6]

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    @staticmethod
    def _similar(left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left in right or right in left:
            return True
        if SequenceMatcher(None, left, right).ratio() >= 0.56:
            return True
        left_pairs = {left[index : index + 2] for index in range(max(0, len(left) - 1))}
        right_pairs = {right[index : index + 2] for index in range(max(0, len(right) - 1))}
        return bool(
            left_pairs
            and right_pairs
            and len(left_pairs & right_pairs) / len(left_pairs | right_pairs) >= 0.42
        )
