from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class SessionLedger:
    """Tracks the resource spending used by end-of-session XP settlement."""

    session_id: str = ""
    active: bool = False
    settled: bool = False
    participating_pcs: set[str] = field(default_factory=set)
    fabula_spent: int = 0
    ultima_spent: int = 0
    entries: list[dict[str, object]] = field(default_factory=list)
    fulfilled_promises: list[dict[str, str]] = field(default_factory=list)

    def start(self, session_id: str, *, participating_pcs: list[str] | None = None) -> None:
        clean_id = str(session_id or "default")
        if self.active and self.session_id == clean_id and not self.settled:
            self.participating_pcs.update(participating_pcs or [])
            return
        self.session_id = clean_id
        self.active = True
        self.settled = False
        self.participating_pcs = set(participating_pcs or [])
        self.fabula_spent = 0
        self.ultima_spent = 0
        self.entries = []
        self.fulfilled_promises = []

    def record_resource_change(self, name: str, resource: str, before: int, after: int) -> None:
        if not self.active or self.settled:
            return
        if resource != "fabula_points" or after >= before:
            return
        amount = before - after
        self.fabula_spent += amount
        self.participating_pcs.add(name)
        self.entries.append({"kind": "fabula_spent", "actor": name, "amount": amount})

    def record_ultima_spent(self, name: str, amount: int, reason: str = "") -> None:
        if not self.active or self.settled or amount <= 0:
            return
        self.ultima_spent += amount
        self.entries.append({"kind": "ultima_spent", "actor": name, "amount": amount, "reason": reason})

    def mark_participant(self, character_name: str) -> None:
        if self.active and character_name:
            self.participating_pcs.add(character_name)

    def record_fulfilled_promise(self, condition: dict[str, object]) -> dict[str, str] | None:
        """Keep an NPC concession authoritative across scene boundaries."""

        if not self.active or self.settled:
            return None
        record = {
            "condition_id": str(condition.get("condition_id") or "").strip(),
            "npc": str(condition.get("npc") or "").strip(),
            "condition": str(condition.get("condition") or "").strip(),
            "promised_result": str(condition.get("promised_result") or "").strip(),
            "promise_key": str(condition.get("promise_key") or "").strip(),
            "promise_kind": str(condition.get("promise_kind") or "").strip(),
            "promise_subject": str(condition.get("promise_subject") or "").strip(),
            "status": "resolved",
        }
        if not record["npc"] or not record["promised_result"]:
            return None
        existing = self.find_fulfilled_promise(
            npc=record["npc"],
            promise_key=record["promise_key"],
            promise_subject=record["promise_subject"],
            promised_result=record["promised_result"],
        )
        if existing is not None:
            return existing
        self.fulfilled_promises.append(record)
        self.entries.append(
            {
                "kind": "fulfilled_promise",
                "npc": record["npc"],
                "promised_result": record["promised_result"],
                "promise_key": record["promise_key"],
                "promise_subject": record["promise_subject"],
            }
        )
        return record

    def find_fulfilled_promise(
        self,
        *,
        npc: str,
        promise_key: str = "",
        promise_subject: str = "",
        promised_result: str = "",
    ) -> dict[str, str] | None:
        clean_npc = str(npc or "").strip()
        clean_key = str(promise_key or "").strip()
        clean_subject = self._normalize(promise_subject)
        clean_result = self._normalize(promised_result)
        if not clean_npc:
            return None
        for record in reversed(self.fulfilled_promises):
            if str(record.get("npc") or "").strip() != clean_npc:
                continue
            existing_key = str(record.get("promise_key") or "").strip()
            existing_subject = self._normalize(record.get("promise_subject"))
            existing_result = self._normalize(record.get("promised_result"))
            if clean_key and existing_key and clean_key == existing_key:
                return record
            if clean_subject and existing_subject and (
                clean_subject == existing_subject
                or clean_subject in existing_subject
                or existing_subject in clean_subject
            ):
                return record
            if clean_result and existing_result and (
                clean_result in existing_result or existing_result in clean_result
            ):
                return record
        return None

    @staticmethod
    def _normalize(value: object) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()

    def finish(self) -> None:
        self.settled = True
        self.active = False

    def to_snapshot(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "active": self.active,
            "settled": self.settled,
            "participating_pcs": sorted(self.participating_pcs),
            "fabula_spent": self.fabula_spent,
            "ultima_spent": self.ultima_spent,
            "entries": list(self.entries),
            "fulfilled_promises": [dict(item) for item in self.fulfilled_promises],
        }

    def apply_snapshot(self, data: dict[str, object] | None) -> None:
        data = data or {}
        self.session_id = str(data.get("session_id") or "")
        self.active = bool(data.get("active", False))
        self.settled = bool(data.get("settled", False))
        self.participating_pcs = {str(name) for name in data.get("participating_pcs", []) if str(name)}
        self.fabula_spent = max(0, int(data.get("fabula_spent", 0) or 0))
        self.ultima_spent = max(0, int(data.get("ultima_spent", 0) or 0))
        self.entries = [dict(item) for item in data.get("entries", []) if isinstance(item, dict)]
        self.fulfilled_promises = [
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("fulfilled_promises", [])
            if isinstance(item, dict)
        ]
