from __future__ import annotations

import json
import re
import threading
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fu_gm.campaign_paths import safe_campaign_path_segment
from fu_gm.conversation.events import MessageEvent
from fu_gm.conversation.reply import ReplyEnvelope


class ReplyLedger:
    """Auditable message/reply ledger with lightweight follow-up tracking."""

    def __init__(self, root: str | Path = "data/campaigns", *, max_in_memory: int = 1000) -> None:
        self.root = Path(root)
        self.max_in_memory = max(50, int(max_in_memory))
        self._events: dict[str, MessageEvent] = {}
        self._event_order: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=self.max_in_memory))
        self._envelopes: dict[str, ReplyEnvelope] = {}
        self._envelope_order: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=self.max_in_memory))
        self._reply_by_event: dict[str, str] = {}
        self._replies_by_event: dict[str, list[str]] = defaultdict(list)
        self._outcomes: dict[str, str] = {}
        self._reply_deliveries: dict[str, dict[str, Any]] = {}
        self._followup_pairs: set[tuple[str, str]] = set()
        self._loaded_campaigns: set[str] = set()
        self._pending_records: deque[tuple[str, dict[str, Any]]] = deque()
        self.last_persistence_error = ""
        self.last_persistence_operation = ""
        self._lock = threading.RLock()

    def register_event(self, event: MessageEvent) -> MessageEvent:
        with self._lock:
            self._ensure_campaign_loaded_locked(event.campaign_id)
            if event.event_id in self._events:
                return self._events[event.event_id]
            self._flush_pending_locked()
            self._append_critical_record_locked(
                event.campaign_id,
                {"record_type": "message_event", "data": event.to_dict()},
                operation="register_event",
            )
            self._events[event.event_id] = event
            self._event_order[event.scope_key].append(event.event_id)
            self._observe_followup(event)
            return event

    def record_reply(self, envelope: ReplyEnvelope) -> ReplyEnvelope:
        with self._lock:
            self._ensure_campaign_loaded_locked(envelope.campaign_id)
            if envelope.envelope_id in self._envelopes:
                return self._envelopes[envelope.envelope_id]
            self._append_or_defer_locked(
                envelope.campaign_id,
                {"record_type": "reply_envelope", "data": envelope.to_dict()},
                operation="record_reply",
            )
            self._envelopes[envelope.envelope_id] = envelope
            scope = self._scope(envelope.campaign_id, envelope.session_id, envelope.channel_id)
            self._envelope_order[scope].append(envelope.envelope_id)
            if envelope.target_event_id:
                self._reply_by_event[envelope.target_event_id] = envelope.envelope_id
                event_replies = self._replies_by_event[envelope.target_event_id]
                if envelope.envelope_id not in event_replies:
                    event_replies.append(envelope.envelope_id)
                self._outcomes[envelope.target_event_id] = "replied"
            return envelope

    def mark_outcome(self, event: MessageEvent, outcome: str, *, reason: str = "") -> None:
        normalized = str(outcome or "").strip() or "observed"
        with self._lock:
            self._ensure_campaign_loaded_locked(event.campaign_id)
            if self._outcomes.get(event.event_id) == "replied" and normalized != "replied":
                return
            self._append_or_defer_locked(
                event.campaign_id,
                {
                    "record_type": "message_outcome",
                    "data": {"event_id": event.event_id, "outcome": normalized, "reason": reason},
                },
                operation="mark_outcome",
            )
            self._outcomes[event.event_id] = normalized

    def persistence_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": not bool(self.last_persistence_error),
                "error": self.last_persistence_error,
                "operation": self.last_persistence_operation,
                "pending_records": len(self._pending_records),
            }

    def purge_campaign(self, campaign_id: str) -> None:
        """Forget deleted campaign records without recreating its ledger file."""

        clean_campaign = str(campaign_id or "").strip()
        if not clean_campaign:
            return
        with self._lock:
            event_ids = {
                event_id
                for event_id, event in self._events.items()
                if event.campaign_id == clean_campaign
            }
            envelope_ids = {
                envelope_id
                for envelope_id, envelope in self._envelopes.items()
                if envelope.campaign_id == clean_campaign
            }
            for event_id in event_ids:
                self._events.pop(event_id, None)
                self._outcomes.pop(event_id, None)
                self._reply_by_event.pop(event_id, None)
                self._replies_by_event.pop(event_id, None)
            for envelope_id in envelope_ids:
                self._envelopes.pop(envelope_id, None)
                self._reply_deliveries.pop(envelope_id, None)
            self._event_order = defaultdict(
                lambda: deque(maxlen=self.max_in_memory),
                {
                    key: values
                    for key, values in self._event_order.items()
                    if not key.startswith(f"{clean_campaign}::")
                },
            )
            self._envelope_order = defaultdict(
                lambda: deque(maxlen=self.max_in_memory),
                {
                    key: values
                    for key, values in self._envelope_order.items()
                    if not key.startswith(f"{clean_campaign}::")
                },
            )
            self._followup_pairs = {
                pair
                for pair in self._followup_pairs
                if pair[0] not in event_ids and pair[1] not in event_ids
            }
            self._pending_records = deque(
                (stored_campaign, record)
                for stored_campaign, record in self._pending_records
                if stored_campaign != clean_campaign
            )
            self._loaded_campaigns.discard(clean_campaign)

    def has_replied_to(self, event_id: str) -> bool:
        with self._lock:
            return self._outcomes.get(event_id) == "replied"

    def has_event(self, event_id: str, *, campaign_id: str = "") -> bool:
        with self._lock:
            if campaign_id:
                self._ensure_campaign_loaded_locked(campaign_id)
            return event_id in self._events

    def outcome_for_event(self, event_id: str) -> str:
        with self._lock:
            return self._outcomes.get(event_id, "")

    def latest_reply_for_event(self, event_id: str) -> ReplyEnvelope | None:
        with self._lock:
            envelope_id = self._reply_by_event.get(event_id, "")
            return self._envelopes.get(envelope_id)

    def replies_for_event(self, event_id: str) -> list[ReplyEnvelope]:
        """Return every visible reply to one platform event in send order."""

        with self._lock:
            return [
                self._envelopes[envelope_id]
                for envelope_id in self._replies_by_event.get(event_id, ())
                if envelope_id in self._envelopes
            ]

    def confirm_reply_delivery(
        self,
        envelope_id: str,
        *,
        campaign_id: str = "",
        platform: str = "",
        delivered_at: str = "",
    ) -> dict[str, Any]:
        """Persist platform acknowledgement for one generated reply envelope."""

        clean_id = str(envelope_id or "").strip()
        with self._lock:
            if campaign_id:
                self._ensure_campaign_loaded_locked(str(campaign_id))
            envelope = self._envelopes.get(clean_id)
            if envelope is None and not campaign_id:
                for path in self.root.glob("*/conversation/reply_ledger.jsonl"):
                    self._ensure_campaign_loaded_locked(path.parent.parent.name)
                    envelope = self._envelopes.get(clean_id)
                    if envelope is not None:
                        break
            if envelope is None:
                return {"ok": False, "envelope_id": clean_id, "error": "未找到回复信封。"}
            existing = self._reply_deliveries.get(clean_id)
            if existing is not None:
                return {**existing, "already_confirmed": True}
            result = {
                "ok": True,
                "envelope_id": clean_id,
                "campaign_id": envelope.campaign_id,
                "session_id": envelope.session_id,
                "channel_id": envelope.channel_id,
                "platform": str(platform or "astrbot").strip() or "astrbot",
                "delivery_status": "delivered",
                "delivered_at": str(delivered_at or "").strip(),
            }
            self._append_critical_record_locked(
                envelope.campaign_id,
                {"record_type": "reply_delivery", "data": result},
                operation="confirm_reply_delivery",
            )
            self._reply_deliveries[clean_id] = result
            return result

    def reply_delivery(self, envelope_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._reply_deliveries.get(str(envelope_id or "").strip(), {}))

    def recent_envelopes(
        self,
        campaign_id: str,
        session_id: str,
        channel_id: str = "",
        *,
        limit: int = 20,
    ) -> list[ReplyEnvelope]:
        scope = self._scope(campaign_id, session_id, channel_id)
        with self._lock:
            self._ensure_campaign_loaded_locked(campaign_id)
            ids = list(self._envelope_order.get(scope, ()))
            return [self._envelopes[item] for item in ids[-max(0, limit) :] if item in self._envelopes]

    def recent_events(
        self,
        campaign_id: str,
        session_id: str,
        channel_id: str = "",
        *,
        limit: int = 20,
    ) -> list[MessageEvent]:
        """Return recent incoming events with their trusted platform ids."""

        scope = self._scope(campaign_id, session_id, channel_id)
        with self._lock:
            self._ensure_campaign_loaded_locked(campaign_id)
            ids = list(self._event_order.get(scope, ()))
            return [
                self._events[item]
                for item in ids[-max(0, limit) :]
                if item in self._events
            ]

    def snapshot(self, campaign_id: str, session_id: str, channel_id: str = "") -> dict[str, Any]:
        scope = self._scope(campaign_id, session_id, channel_id)
        with self._lock:
            self._ensure_campaign_loaded_locked(campaign_id)
            event_ids = list(self._event_order.get(scope, ()))
            envelope_ids = list(self._envelope_order.get(scope, ()))
            outcomes: dict[str, int] = defaultdict(int)
            for event_id in event_ids:
                outcomes[self._outcomes.get(event_id, "pending")] += 1
            envelope_id_set = set(envelope_ids)
            followup_count = sum(1 for envelope_id, _event_id in self._followup_pairs if envelope_id in envelope_id_set)
            direct_address_count = sum(
                1
                for event_id in event_ids
                if event_id in self._events and self._events[event_id].directly_addresses_gm
            )
            proactive_reply_count = sum(
                1
                for envelope_id in envelope_ids
                if envelope_id in self._envelopes and not self._envelopes[envelope_id].target_event_id
            )
            quoted_reply_count = sum(
                1
                for envelope_id in envelope_ids
                if envelope_id in self._envelopes and self._envelopes[envelope_id].quote
            )
            return {
                "scope": scope,
                "message_count": len(event_ids),
                "reply_count": len(envelope_ids),
                "direct_address_count": direct_address_count,
                "player_followup_count": followup_count,
                "proactive_reply_count": proactive_reply_count,
                "quoted_reply_count": quoted_reply_count,
                "quoted_reply_ratio": round(
                    quoted_reply_count / len(envelope_ids), 3
                )
                if envelope_ids
                else 0.0,
                "delivered_reply_count": sum(
                    1 for envelope_id in envelope_ids if envelope_id in self._reply_deliveries
                ),
                "outcomes": dict(outcomes),
                "recent_targets": [
                    {
                        "target_event_id": envelope.target_event_id,
                        "target_message_id": envelope.target_message_id,
                        "target_speaker": envelope.target_speaker,
                        "delivery_mode": envelope.delivery.mode,
                        "quote_message_id": envelope.delivery.quote_message_id,
                        "kind": envelope.kind,
                    }
                    for envelope in (
                        self._envelopes[item]
                        for item in envelope_ids[-10:]
                        if item in self._envelopes
                    )
                ],
            }

    def path_for(self, campaign_id: str) -> Path:
        return self._ledger_path(campaign_id)

    def _ensure_campaign_loaded_locked(self, campaign_id: str) -> None:
        clean_campaign = str(campaign_id or "default")
        if clean_campaign in self._loaded_campaigns:
            return
        paths = [self._ledger_path(clean_campaign)]
        legacy_path = self._legacy_ledger_path(clean_campaign)
        if (
            legacy_path != paths[0]
            and legacy_path.exists()
            and self._ledger_belongs_to_campaign(legacy_path, clean_campaign)
        ):
            paths.append(legacy_path)
        if not any(path.exists() for path in paths):
            self._loaded_campaigns.add(clean_campaign)
            return
        for path in paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                self._set_persistence_failure("load_ledger", exc)
                raise
            for line in lines:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = str(record.get("record_type") or "")
                data = record.get("data")
                if not isinstance(data, dict):
                    continue
                if record_type == "message_event":
                    event = MessageEvent.from_dict(data)
                    if not event.event_id or event.event_id in self._events:
                        continue
                    self._events[event.event_id] = event
                    self._event_order[event.scope_key].append(event.event_id)
                    continue
                if record_type == "reply_envelope":
                    envelope = ReplyEnvelope.from_dict(data)
                    if not envelope.envelope_id or envelope.envelope_id in self._envelopes:
                        continue
                    self._envelopes[envelope.envelope_id] = envelope
                    scope = self._scope(
                        envelope.campaign_id,
                        envelope.session_id,
                        envelope.channel_id,
                    )
                    self._envelope_order[scope].append(envelope.envelope_id)
                    if envelope.target_event_id:
                        self._reply_by_event[envelope.target_event_id] = envelope.envelope_id
                        event_replies = self._replies_by_event[
                            envelope.target_event_id
                        ]
                        if envelope.envelope_id not in event_replies:
                            event_replies.append(envelope.envelope_id)
                        self._outcomes[envelope.target_event_id] = "replied"
                    continue
                if record_type == "message_outcome":
                    event_id = str(data.get("event_id") or "")
                    outcome = str(data.get("outcome") or "")
                    if event_id and outcome and self._outcomes.get(event_id) != "replied":
                        self._outcomes[event_id] = outcome
                    continue
                if record_type == "reply_delivery":
                    envelope_id = str(data.get("envelope_id") or "")
                    if envelope_id:
                        self._reply_deliveries[envelope_id] = dict(data)
                    continue
                if record_type == "reply_followup":
                    envelope_id = str(data.get("envelope_id") or "")
                    event_id = str(data.get("followup_event_id") or "")
                    if envelope_id and event_id:
                        self._followup_pairs.add((envelope_id, event_id))
        self._loaded_campaigns.add(clean_campaign)
        self._clear_persistence_failure_if_recovered()

    def _observe_followup(self, event: MessageEvent) -> None:
        envelope_ids = list(self._envelope_order.get(event.scope_key, ()))
        for envelope_id in reversed(envelope_ids):
            envelope = self._envelopes.get(envelope_id)
            if envelope is None or envelope.target_event_id == event.event_id:
                continue
            if not self._same_speaker(event, envelope):
                continue
            pair = (envelope_id, event.event_id)
            if pair in self._followup_pairs:
                return
            self._append_or_defer_locked(
                event.campaign_id,
                {
                    "record_type": "reply_followup",
                    "data": {
                        "envelope_id": envelope_id,
                        "followup_event_id": event.event_id,
                        "speaker": event.speaker,
                        "text": event.text[:1000],
                    },
                },
                operation="record_followup",
            )
            self._followup_pairs.add(pair)
            return

    def _append_critical_record_locked(
        self,
        campaign_id: str,
        record: dict[str, Any],
        *,
        operation: str,
    ) -> None:
        try:
            self._append_record(campaign_id, record)
        except Exception as exc:
            self._set_persistence_failure(operation, exc)
            raise
        self._clear_persistence_failure_if_recovered()

    def _append_or_defer_locked(
        self,
        campaign_id: str,
        record: dict[str, Any],
        *,
        operation: str,
    ) -> None:
        if self._pending_records:
            self._pending_records.append((campaign_id, record))
            return
        try:
            self._append_record(campaign_id, record)
        except Exception as exc:
            self._pending_records.append((campaign_id, record))
            self._set_persistence_failure(operation, exc)
            return
        self._clear_persistence_failure_if_recovered()

    def _flush_pending_locked(self) -> None:
        while self._pending_records:
            campaign_id, record = self._pending_records[0]
            try:
                self._append_record(campaign_id, record)
            except Exception as exc:
                self._set_persistence_failure("flush_pending", exc)
                raise
            self._pending_records.popleft()
        self._clear_persistence_failure_if_recovered()

    def _set_persistence_failure(self, operation: str, error: Exception) -> None:
        self.last_persistence_operation = str(operation or "")
        self.last_persistence_error = str(error)[:500]

    def _clear_persistence_failure_if_recovered(self) -> None:
        if self._pending_records:
            return
        self.last_persistence_operation = ""
        self.last_persistence_error = ""

    @staticmethod
    def _same_speaker(event: MessageEvent, envelope: ReplyEnvelope) -> bool:
        if event.speaker_id and envelope.target_speaker_id:
            return event.speaker_id == envelope.target_speaker_id
        return bool(event.speaker and event.speaker == envelope.target_speaker)

    def _append_record(self, campaign_id: str, record: dict[str, Any]) -> None:
        path = self._ledger_path(campaign_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=self._json_default) + "\n")

    def _ledger_path(self, campaign_id: str) -> Path:
        return self.root / self._safe_name(campaign_id or "default") / "conversation" / "reply_ledger.jsonl"

    def _legacy_ledger_path(self, campaign_id: str) -> Path:
        legacy_name = self._legacy_safe_name(campaign_id or "default")
        return self.root / legacy_name / "conversation" / "reply_ledger.jsonl"

    @staticmethod
    def _ledger_belongs_to_campaign(path: Path, campaign_id: str) -> bool:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        expected = str(campaign_id or "default")
        for line in lines:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            data = record.get("data") if isinstance(record, dict) else None
            if not isinstance(data, dict):
                continue
            recorded = str(data.get("campaign_id") or "")
            if recorded:
                return recorded == expected
        return False

    @staticmethod
    def _scope(campaign_id: str, session_id: str, channel_id: str) -> str:
        return "::".join((campaign_id, session_id, channel_id))

    @staticmethod
    def _safe_name(value: str) -> str:
        return safe_campaign_path_segment(value)

    @staticmethod
    def _legacy_safe_name(value: str) -> str:
        cleaned = re.sub(r"[\\/:*?\"<>|#\s]+", "_", str(value).strip()).strip("._")
        return cleaned or "default"

    @staticmethod
    def _json_default(value: Any) -> Any:
        try:
            return asdict(value)
        except TypeError:
            return str(value)
