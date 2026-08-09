from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from fu_gm.gm_tool_contracts import (
    GMNarrativeEvent,
    GMToolExecutionContext,
    GMToolReceipt,
)


class TableWorkingBriefManager:
    """Maintain a provenance-safe handoff between consecutive GM turns.

    The brief never interprets natural language. It preserves declarations as
    declarations and copies authoritative outcomes only from successful typed
    tool receipts. This makes it useful to the next model call without allowing
    a lossy router summary to become campaign truth.
    """

    VERSION = 1
    SOURCE_LIMIT = 24
    TRANSACTION_LIMIT = 24
    FACT_LIMIT = 32

    @classmethod
    def observe(
        cls,
        frame: object | None,
        context: GMToolExecutionContext,
        receipts: Iterable[GMToolReceipt],
        *,
        target: str,
        public_reply: str,
    ) -> dict[str, object]:
        if frame is None:
            return {}
        brief = cls._ensure(frame)
        source_events = cls._current_events(context)
        changed = False
        for event in source_events:
            changed = cls._upsert_source_event(brief, event) or changed

        successful_receipts = [
            receipt for receipt in receipts if receipt.ok and receipt.state_changed
        ]
        narrative_events = [
            event
            for receipt in successful_receipts
            for event in receipt.narrative_events
            if isinstance(event, GMNarrativeEvent) and event.meaningful
        ]
        for event in narrative_events:
            changed = cls._append_transaction(brief, event) or changed

        resolved_ids = {
            event.source_event_id
            for event in narrative_events
            if event.source_event_id
        }
        receipt_tools_by_source: dict[str, list[str]] = {}
        for receipt in successful_receipts:
            source = receipt.result.get("source_event")
            if not isinstance(source, dict):
                continue
            event_id = str(source.get("event_id") or "").strip()
            if event_id:
                receipt_tools_by_source.setdefault(event_id, []).append(
                    receipt.tool_name
                )

        fallback_status = (
            "gm_replied_without_state_change"
            if target == "fu_gm" and str(public_reply or "").strip()
            else "observed_table_talk"
            if target == "silent"
            else "delegated"
        )
        for item in brief["source_events"]:
            event_id = str(item.get("event_id") or "")
            if event_id not in {str(event.get("event_id") or "") for event in source_events}:
                continue
            next_status = "tool_committed" if event_id in resolved_ids else fallback_status
            if item.get("status") != next_status:
                item["status"] = next_status
                changed = True
            tools = list(dict.fromkeys(receipt_tools_by_source.get(event_id, [])))
            if tools and item.get("tool_names") != tools:
                item["tool_names"] = tools
                changed = True

        clean_reply = str(public_reply or "").strip()
        if target == "fu_gm" and clean_reply:
            excerpt = clean_reply[:1000]
            if brief.get("last_public_reply") != excerpt:
                brief["last_public_reply"] = excerpt
                changed = True
        if changed:
            brief["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {
            "changed": changed,
            "source_event_count": len(source_events),
            "narrative_event_count": len(narrative_events),
            "confirmed_fact_count": len(brief["fact_evidence"]),
        }

    @classmethod
    def snapshot(cls, frame: object | None) -> dict[str, object]:
        if frame is None:
            return {"active": False, "version": cls.VERSION}
        brief = cls._ensure(frame)
        return {
            "active": True,
            "version": int(brief.get("version") or cls.VERSION),
            "source_events": [dict(item) for item in brief["source_events"][-12:]],
            "committed_transactions": [
                dict(item) for item in brief["committed_transactions"][-12:]
            ],
            "fact_evidence": [dict(item) for item in brief["fact_evidence"][-16:]],
            "last_authoritative_outcome": str(
                brief.get("last_authoritative_outcome") or ""
            ),
            "last_public_reply": str(brief.get("last_public_reply") or ""),
            "updated_at": str(brief.get("updated_at") or ""),
            "interpretation_rule": (
                "source_events中的text只是玩家原话或声明，不代表行动已经成功；"
                "只有committed_transactions.outcome与fact_evidence是工具确认结果。"
            ),
        }

    @classmethod
    def model_snapshot(
        cls,
        frame: object | None,
        *,
        include_last_public_reply: bool = True,
    ) -> dict[str, object]:
        """Return the brief's table facts without scheduler instructions.

        ``snapshot`` remains the complete audit representation.  The model
        view deliberately removes synthetic heartbeat requests: they explain
        why the scheduler woke the GM, but they are neither table dialogue nor
        story evidence and can otherwise be echoed on later turns.
        """

        raw = cls.snapshot(frame)
        if not raw.get("active"):
            return raw

        source_events = [
            {
                key: item.get(key)
                for key in ("event_id", "speaker", "text", "status")
                if item.get(key) not in (None, "", [], {})
            }
            for item in list(raw.get("source_events") or [])
            if isinstance(item, dict)
            and not cls._is_internal_system_event(item)
        ]
        transactions: list[dict[str, object]] = []
        for item in list(raw.get("committed_transactions") or []):
            if not isinstance(item, dict):
                continue
            internal = cls._is_internal_system_event(item)
            compact = {
                key: item.get(key)
                for key in (
                    "event_type",
                    "tool_name",
                    "status",
                    "source_event_id",
                    "source_speaker",
                    "declaration",
                    "outcome",
                    "public_facts",
                )
                if item.get(key) not in (None, "", [], {})
            }
            if internal:
                compact.pop("source_speaker", None)
                compact.pop("declaration", None)
                compact.pop("source_event_id", None)
            if not compact.get("outcome") and not compact.get("public_facts"):
                if internal:
                    continue
            transactions.append(compact)

        result: dict[str, object] = {
            "active": True,
            "version": int(raw.get("version") or cls.VERSION),
        }
        facts = list(raw.get("fact_evidence") or [])[-12:]
        last_outcome = str(raw.get("last_authoritative_outcome") or "")
        last_reply = str(raw.get("last_public_reply") or "")
        if source_events:
            result["source_events"] = source_events[-8:]
        if transactions:
            result["committed_transactions"] = transactions[-8:]
        if facts:
            result["fact_evidence"] = facts
        if last_outcome:
            result["last_authoritative_outcome"] = last_outcome
        if include_last_public_reply and last_reply:
            result["last_public_reply"] = last_reply
        return result

    @classmethod
    def normalize(cls, frame: object | None) -> None:
        if frame is None:
            return
        cls._ensure(frame)

    @classmethod
    def _ensure(cls, frame: object) -> dict[str, object]:
        raw = getattr(frame, "working_brief", None)
        brief = raw if isinstance(raw, dict) else {}
        normalized: dict[str, object] = {
            "version": cls.VERSION,
            "source_events": cls._dict_list(brief.get("source_events"))[
                -cls.SOURCE_LIMIT :
            ],
            "committed_transactions": cls._dict_list(
                brief.get("committed_transactions")
            )[-cls.TRANSACTION_LIMIT :],
            "fact_evidence": cls._dict_list(brief.get("fact_evidence"))[
                -cls.FACT_LIMIT :
            ],
            "last_authoritative_outcome": str(
                brief.get("last_authoritative_outcome") or ""
            ),
            "last_public_reply": str(brief.get("last_public_reply") or ""),
            "updated_at": str(brief.get("updated_at") or ""),
        }
        if raw != normalized:
            setattr(frame, "working_brief", normalized)
        return normalized

    @staticmethod
    def _dict_list(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @classmethod
    def _current_events(
        cls,
        context: GMToolExecutionContext,
    ) -> list[dict[str, object]]:
        if context.metadata.get("system_gm_beat_request"):
            return []
        raw = context.metadata.get("current_turn_events")
        events = (
            [dict(item) for item in raw if isinstance(item, dict)]
            if isinstance(raw, list)
            else []
        )
        if not events:
            events = [
                {
                    "event_id": str(context.metadata.get("source_event_id") or ""),
                    "message_id": str(context.metadata.get("source_message_id") or ""),
                    "speaker": context.speaker,
                    "speaker_id": str(context.metadata.get("source_speaker_id") or ""),
                    "text": str(context.metadata.get("current_message") or ""),
                }
            ]
        return [
            {
                "event_id": str(item.get("event_id") or ""),
                "message_id": str(item.get("message_id") or ""),
                "speaker": str(item.get("speaker") or context.speaker),
                "speaker_id": str(item.get("speaker_id") or ""),
                "text": str(item.get("text") or "")[:800],
                "created_at": str(item.get("created_at") or ""),
            }
            for item in events
            if str(item.get("text") or "").strip()
        ]

    @staticmethod
    def _is_internal_system_event(item: dict[str, object]) -> bool:
        speaker = str(
            item.get("speaker") or item.get("source_speaker") or ""
        ).strip()
        text = str(item.get("text") or item.get("declaration") or "").strip()
        return speaker.startswith("系统") or text.startswith("系统GM主动节拍请求")

    @classmethod
    def _upsert_source_event(
        cls,
        brief: dict[str, object],
        event: dict[str, object],
    ) -> bool:
        entries = brief["source_events"]
        assert isinstance(entries, list)
        event_id = str(event.get("event_id") or "")
        message_id = str(event.get("message_id") or "")
        existing = next(
            (
                item
                for item in entries
                if (event_id and str(item.get("event_id") or "") == event_id)
                or (message_id and str(item.get("message_id") or "") == message_id)
            ),
            None,
        )
        if existing is not None:
            return False
        entries.append({**event, "status": "observed", "tool_names": []})
        del entries[:-cls.SOURCE_LIMIT]
        return True

    @classmethod
    def _append_transaction(
        cls,
        brief: dict[str, object],
        event: GMNarrativeEvent,
    ) -> bool:
        entries = brief["committed_transactions"]
        facts = brief["fact_evidence"]
        assert isinstance(entries, list)
        assert isinstance(facts, list)
        declaration = event.declaration[:800]
        if cls._is_internal_system_event(
            {
                "source_speaker": event.source_speaker,
                "declaration": declaration,
            }
        ):
            declaration = ""
        record = {
            "event_type": event.event_type,
            "tool_name": event.tool_name,
            "status": event.status,
            "source_event_id": event.source_event_id,
            "source_message_id": event.source_message_id,
            "source_speaker": event.source_speaker,
            "declaration": declaration,
            "outcome": event.outcome[:800],
            "public_facts": list(event.public_facts),
        }
        signature = (
            record["tool_name"],
            record["source_event_id"],
            record["outcome"],
            tuple(record["public_facts"]),
        )
        if any(
            (
                item.get("tool_name"),
                item.get("source_event_id"),
                item.get("outcome"),
                tuple(item.get("public_facts") or []),
            )
            == signature
            for item in entries
        ):
            return False
        entries.append(record)
        del entries[:-cls.TRANSACTION_LIMIT]
        if event.outcome:
            brief["last_authoritative_outcome"] = event.outcome[:800]
        for fact in event.public_facts:
            clean = str(fact or "").strip()
            if not clean or any(item.get("text") == clean for item in facts):
                continue
            facts.append(
                {
                    "text": clean,
                    "source_event_id": event.source_event_id,
                    "source_speaker": event.source_speaker,
                    "tool_name": event.tool_name,
                }
            )
        del facts[:-cls.FACT_LIMIT]
        return True


__all__ = ["TableWorkingBriefManager"]
