from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


class NPCStatementBoundary:
    """Reject scene prose that transfers one NPC's public words to another.

    Scene openings are allowed to improvise new dialogue, but an NPC cannot
    repeat a distinctive restriction, promise, or disclosure that the table
    already heard from somebody else.  This validator uses only public speech
    recorded in the NPC ledger; goals and secrets never participate.
    """

    _GENERIC_OVERLAPS = (
        "我已经说清楚",
        "条件已经说清楚",
        "我不知道",
        "我不能告诉你",
        "我可以告诉你",
        "现在就说",
        "你们可以",
        "我会回应",
        "先听我说",
    )

    @classmethod
    def violation(
        cls,
        metadata: dict[str, Any] | None,
        ledger: list[dict[str, Any]] | None,
    ) -> str:
        if not isinstance(metadata, dict) or not ledger:
            return ""
        speakers = metadata.get("npc_speakers")
        if not isinstance(speakers, list):
            return ""

        records = [cls._record(item) for item in ledger if isinstance(item, dict)]
        records = [item for item in records if item[0] and item[2]]
        if len(records) < 2:
            return ""

        for speaker in speakers:
            if not isinstance(speaker, dict):
                continue
            npc = str(speaker.get("npc") or "").strip()
            statement = str(speaker.get("public_statement") or "").strip()
            if not npc or not statement:
                continue
            owner = cls._resolve_owner(npc, records)
            own_statements = owner[2] if owner is not None else []
            own_match = max(
                (cls._overlap(statement, known) for known in own_statements),
                default=(0.0, 0, ""),
            )
            for record in records:
                if owner is not None and record[0] == owner[0]:
                    continue
                best_other = max(
                    (cls._overlap(statement, known) for known in record[2]),
                    default=(0.0, 0, ""),
                )
                score, size, fragment = best_other
                if not cls._is_distinctive_match(score, size, fragment):
                    continue
                if own_match[0] >= score - 0.08 and own_match[1] >= size - 1:
                    continue
                return (
                    f"NPC台词归属冲突：【{npc}】的候选台词复用了"
                    f"【{record[0]}】已经公开的内容“{fragment[:28]}”。"
                    "请让每名NPC只延续自己的立场，或改用不含台词的现场描写。"
                )
        return ""

    @classmethod
    def _record(
        cls,
        item: dict[str, Any],
    ) -> tuple[str, set[str], list[str]]:
        name = str(item.get("npc") or item.get("name") or "").strip()
        aliases = {
            cls._compact(name),
            cls._compact(item.get("public_identity")),
            *(cls._compact(alias) for alias in item.get("aliases", []) or []),
        }
        statements = [
            " ".join(str(value or "").split()).strip()
            for value in item.get("statements", []) or []
            if " ".join(str(value or "").split()).strip()
        ]
        return name, {alias for alias in aliases if alias}, statements

    @classmethod
    def _resolve_owner(
        cls,
        npc: str,
        records: list[tuple[str, set[str], list[str]]],
    ) -> tuple[str, set[str], list[str]] | None:
        compact = cls._compact(npc)
        exact = next((record for record in records if compact in record[1]), None)
        if exact is not None:
            return exact
        return next(
            (
                record
                for record in records
                if any(
                    min(len(compact), len(alias)) >= 4
                    and (compact in alias or alias in compact)
                    for alias in record[1]
                )
            ),
            None,
        )

    @classmethod
    def _overlap(cls, candidate: str, known: str) -> tuple[float, int, str]:
        left = cls._semantic_compact(candidate)
        right = cls._semantic_compact(known)
        if not left or not right:
            return 0.0, 0, ""
        match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
        size = int(match.size)
        fragment = left[match.a : match.a + size]
        score = float(size) / max(1, min(len(left), len(right)))
        return score, size, fragment

    @classmethod
    def _is_distinctive_match(cls, score: float, size: int, fragment: str) -> bool:
        if size < 8 or score < 0.45:
            return False
        if any(fragment in cls._compact(item) for item in cls._GENERIC_OVERLAPS):
            return False
        return True

    @staticmethod
    def _compact(value: Any) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    @classmethod
    def _semantic_compact(cls, value: Any) -> str:
        """Normalize a few high-risk deontic paraphrases before comparison."""

        text = cls._compact(value)
        for source, target in (
            ("别让他们", "不能"),
            ("别让她", "不能"),
            ("别让他", "不能"),
            ("不能让他们", "不能"),
            ("不能让她", "不能"),
            ("不能让他", "不能"),
            ("不许他们", "不能"),
            ("不许她", "不能"),
            ("不许他", "不能"),
            ("不得", "不能"),
            ("不准", "不能"),
            ("别再", "不能再"),
            ("站回", "站"),
            ("站在", "站"),
        ):
            text = text.replace(source, target)
        return text
