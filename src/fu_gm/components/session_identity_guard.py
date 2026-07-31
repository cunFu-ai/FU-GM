from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from fu_gm.models import SessionDramaticContract


@dataclass(frozen=True)
class SessionIdentityAssessment:
    """How clearly one session differs from its recent neighbours."""

    distinct: bool = True
    closest_session: int = 0
    similarity: float = 0.0
    differing_axes: list[str] = field(default_factory=list)
    repeated_axes: list[str] = field(default_factory=list)
    repair_instruction: str = ""


class SessionIdentityGuard:
    """Detect palette-swapped episodes before they reach the table.

    Continuing an arc may keep the same location or focus thread.  What must
    change is the playable identity: the opening disruption, central dilemma,
    signature image, or climax form.  At least two of those axes should feel
    materially different from each of the last three sessions.
    """

    _AXES = {
        "开场扰动": "opening_disruption",
        "标志画面": "signature_image",
        "核心两难": "dilemma",
        "高潮形式": "climax_type",
        "本场焦点": "focus_thread",
    }

    def assess(
        self,
        contract: SessionDramaticContract,
        recent: list[SessionDramaticContract],
    ) -> SessionIdentityAssessment:
        if not recent:
            return SessionIdentityAssessment()

        closest: tuple[float, SessionDramaticContract, list[str], list[str]] | None = None
        for previous in recent[-3:]:
            differing: list[str] = []
            repeated: list[str] = []
            similarities: list[float] = []
            for label, field_name in self._AXES.items():
                similarity = self._similarity(
                    getattr(contract, field_name, ""),
                    getattr(previous, field_name, ""),
                )
                similarities.append(similarity)
                if similarity < 0.58:
                    differing.append(label)
                elif similarity >= 0.78:
                    repeated.append(label)
            overall = sum(similarities) / max(1, len(similarities))
            candidate = (overall, previous, differing, repeated)
            if closest is None or candidate[0] > closest[0]:
                closest = candidate

        assert closest is not None
        similarity, previous, differing, repeated = closest
        distinct = bool(len(differing) >= 2 and similarity < 0.74)
        instruction = ""
        if not distinct:
            instruction = (
                f"当前方案与第{previous.session_number:02d}场过于相似。"
                f"重复轴：{'、'.join(repeated) or '整体结构'}。"
                "保留世界事实、当前地点和尚未解决的公开后果，但至少重做以下两项："
                "开场正在发生的具体扰动、可触碰的标志画面、两种都合理但代价不同的选择、高潮形式。"
                "不得只替换人名地名或添加形容词。"
            )
        return SessionIdentityAssessment(
            distinct=distinct,
            closest_session=previous.session_number,
            similarity=round(similarity, 4),
            differing_axes=differing,
            repeated_axes=repeated,
            repair_instruction=instruction,
        )

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        left_clean = cls._normalize(left)
        right_clean = cls._normalize(right)
        if not left_clean and not right_clean:
            return 1.0
        if not left_clean or not right_clean:
            return 0.0
        sequence = SequenceMatcher(None, left_clean, right_clean).ratio()
        left_pairs = cls._pairs(left_clean)
        right_pairs = cls._pairs(right_clean)
        overlap = (
            len(left_pairs & right_pairs) / len(left_pairs | right_pairs)
            if left_pairs and right_pairs
            else 0.0
        )
        return max(sequence, overlap)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    @staticmethod
    def _pairs(value: str) -> set[str]:
        return {value[index : index + 2] for index in range(max(0, len(value) - 1))}
