from __future__ import annotations

import json
import uuid
from typing import Any


class NPCResponseWindowManager:
    """Persist NPC questions explicitly emitted by a typed NPC decision.

    This component never reads free-form chat and never calls a model. The GM
    agent decides which existing window a player answered; the NPC decision
    plan decides whether its own response opens a new window. This manager only
    validates identifiers, ownership and typed response items. Public prose is
    never used as a transaction key.
    """

    @classmethod
    def pending(cls, frame: Any | None) -> list[dict[str, Any]]:
        if frame is None:
            return []
        result: list[dict[str, Any]] = []
        for item in getattr(frame, "pending_npc_questions", []):
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("status") or "open").strip().lower() != "open"
                or str(item.get("kind") or "") != "player_response"
            ):
                continue
            cls._migrate_record(item)
            result.append(item)
        return result

    @classmethod
    def remaining_items(cls, item: dict[str, Any] | None) -> list[dict[str, str]]:
        if not isinstance(item, dict):
            return []
        cls._migrate_record(item)
        answered = set(cls.ids(item.get("answered_item_ids")))
        return [
            required
            for required in cls.required_items(item.get("required_items"))
            if required["item_id"] not in answered
        ]

    @classmethod
    def open_request(
        cls,
        frame: Any | None,
        *,
        npc: str,
        summary: str,
        required_items: list[dict[str, str]],
        addressed_actor: str = "",
        scene: Any | None = None,
    ) -> dict[str, Any] | None:
        clean_npc = cls.clean(npc)
        items = cls.required_items(required_items)[:6]
        if frame is None or not clean_npc or not items:
            return None
        actor = cls.clean(addressed_actor)
        for existing in cls.pending(frame):
            if (
                cls.same_name(clean_npc, str(existing.get("npc") or ""))
                and actor == cls.clean(existing.get("addressed_actor"))
                and items == cls.required_items(existing.get("required_items"))
            ):
                return existing
        record = {
            "question_id": (
                f"{getattr(frame, 'scene_key', 'scene')}-player-response-"
                f"{uuid.uuid4().hex[:10]}"
            ),
            "npc": clean_npc,
            "addressed_actor": actor,
            "kind": "player_response",
            "summary": cls.clean(summary)[:300]
            or "、".join(item["prompt"] for item in items)[:300],
            "required_items": json.dumps(items, ensure_ascii=False),
            "answered_item_ids": "[]",
            "response_items": "[]",
            "source": "typed_npc_decision",
            "status": "open",
        }
        getattr(frame, "pending_npc_questions").append(record)
        cls._trim_closed_history(frame)
        if scene is not None:
            scene.pending_npc_questions = [
                dict(item)
                for item in getattr(frame, "pending_npc_questions", [])
                if str(item.get("status") or "open") == "open"
            ]
        return record

    @classmethod
    def record_player_response(
        cls,
        frame: Any | None,
        *,
        question_id: str,
        actor: str,
        response_items: list[dict[str, str]],
        evidence: str = "",
    ) -> dict[str, object] | None:
        clean_id = cls.clean(question_id)
        clean_actor = cls.clean(actor)
        if frame is None or not clean_id:
            return None
        item = next(
            (
                candidate
                for candidate in cls.pending(frame)
                if cls.clean(candidate.get("question_id")) == clean_id
            ),
            None,
        )
        if item is None:
            return None
        cls._migrate_record(item)
        required_actor = cls.clean(item.get("addressed_actor"))
        if required_actor and not cls.same_name(clean_actor, required_actor):
            return None
        required = cls.required_items(item.get("required_items"))
        required_ids = [entry["item_id"] for entry in required]
        normalized_items: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for response in response_items:
            if not isinstance(response, dict):
                return None
            item_id = cls.clean(response.get("item_id"))
            kind = cls.clean(response.get("kind")).lower()
            if (
                not item_id
                or item_id in seen_ids
                or item_id not in required_ids
                or kind not in {"answer", "refuse", "cannot_answer"}
            ):
                return None
            seen_ids.add(item_id)
            normalized_items.append({"item_id": item_id, "kind": kind})
        if not normalized_items:
            return None
        already = set(cls.ids(item.get("answered_item_ids")))
        already.update(response["item_id"] for response in normalized_items)
        ordered = [item_id for item_id in required_ids if item_id in already]
        item["answered_item_ids"] = json.dumps(ordered, ensure_ascii=False)
        prior_responses = cls.response_items(item.get("response_items"))
        response_by_id = {
            response["item_id"]: response for response in prior_responses
        }
        for response in normalized_items:
            response_by_id[response["item_id"]] = response
        item["response_items"] = json.dumps(
            [
                response_by_id[item_id]
                for item_id in required_ids
                if item_id in response_by_id
            ],
            ensure_ascii=False,
        )
        item["last_response_evidence"] = cls.clean(evidence)[:500]
        item["last_response_actor"] = clean_actor
        complete = bool(required) and len(ordered) == len(required)
        if complete:
            item["status"] = "resolved"
            item["resolved_by"] = clean_actor
        return {
            "question_id": clean_id,
            "response_items": normalized_items,
            "answered_item_ids": [
                response["item_id"] for response in normalized_items
            ],
            "complete": complete,
            "evidence": cls.clean(evidence)[:500],
        }

    @classmethod
    def link_condition(
        cls,
        frame: Any | None,
        *,
        question_id: str,
        condition_id: str,
        scene: Any | None = None,
    ) -> bool:
        clean_question_id = cls.clean(question_id)
        clean_condition_id = cls.clean(condition_id)
        if frame is None or not clean_question_id or not clean_condition_id:
            return False
        question = next(
            (
                item
                for item in list(getattr(frame, "pending_npc_questions", []) or [])
                if cls.clean(item.get("question_id")) == clean_question_id
            ),
            None,
        )
        condition = next(
            (
                item
                for item in list(getattr(frame, "open_conditions", []) or [])
                if cls.clean(item.get("condition_id")) == clean_condition_id
            ),
            None,
        )
        if question is None or condition is None:
            return False
        question_npc = cls.clean(question.get("npc"))
        condition_npc = cls.clean(condition.get("npc"))
        if (
            question_npc
            and condition_npc
            and not cls.same_name(question_npc, condition_npc)
        ):
            return False
        question["condition_id"] = clean_condition_id
        condition["linked_question_id"] = clean_question_id
        if scene is not None:
            for scene_condition in list(getattr(scene, "open_conditions", []) or []):
                if cls.clean(scene_condition.get("condition_id")) == clean_condition_id:
                    scene_condition["linked_question_id"] = clean_question_id
                    break
        return True

    @classmethod
    def resolve_linked_condition_request(
        cls,
        frame: Any | None,
        *,
        condition_id: str,
        npc: str,
        actor: str = "",
        public_evidence: str = "",
    ) -> list[dict[str, object]]:
        clean_condition_id = cls.clean(condition_id)
        clean_npc = cls.clean(npc)
        if frame is None or not clean_condition_id:
            return []
        condition = next(
            (
                item
                for item in list(getattr(frame, "open_conditions", []) or [])
                if cls.clean(item.get("condition_id")) == clean_condition_id
            ),
            None,
        )
        linked_id = cls.clean((condition or {}).get("linked_question_id"))
        resolved: list[dict[str, object]] = []
        for question in list(getattr(frame, "pending_npc_questions", []) or []):
            question_id = cls.clean(question.get("question_id"))
            if not (
                (linked_id and question_id == linked_id)
                or cls.clean(question.get("condition_id")) == clean_condition_id
            ):
                continue
            question_npc = cls.clean(question.get("npc"))
            if clean_npc and question_npc and not cls.same_name(clean_npc, question_npc):
                continue
            if cls.clean(question.get("status") or "open").lower() != "open":
                continue
            cls._migrate_record(question)
            required = cls.required_items(question.get("required_items"))
            required_ids = [item["item_id"] for item in required]
            question["answered_item_ids"] = json.dumps(
                required_ids,
                ensure_ascii=False,
            )
            question["response_items"] = json.dumps(
                [
                    {"item_id": item_id, "kind": "answer"}
                    for item_id in required_ids
                ],
                ensure_ascii=False,
            )
            question["status"] = "resolved"
            question["resolved_by"] = cls.clean(actor) or clean_npc
            question["resolution_kind"] = "linked_condition_fulfilled"
            question["resolution_evidence"] = cls.clean(public_evidence)[:500]
            resolved.append(
                {
                    "question_id": question_id,
                    "answered_item_ids": required_ids,
                    "complete": True,
                    "resolution_kind": "linked_condition_fulfilled",
                }
            )
        return resolved

    @classmethod
    def supersede_for_conflict(
        cls,
        frame: Any | None,
        *,
        scene: Any | None = None,
    ) -> list[str]:
        """Close conversational requests when the scene becomes a conflict.

        A formal conflict replaces the immediate question-and-answer exchange;
        leaving an old request open would make the heartbeat scheduler wait for
        dialogue while the turn tracker waits for the NPC.  Records are kept as
        history so the conversation can be resumed after the conflict if it is
        still relevant.
        """

        if frame is None:
            return []
        superseded: list[str] = []
        for question in list(getattr(frame, "pending_npc_questions", []) or []):
            if cls.clean(question.get("status") or "open").lower() != "open":
                continue
            question_id = cls.clean(question.get("question_id"))
            question["status"] = "superseded"
            question["resolution_kind"] = "conflict_started"
            if question_id:
                superseded.append(question_id)
        if scene is not None:
            scene.pending_npc_questions = []
        cls._trim_closed_history(frame)
        return superseded

    @classmethod
    def public_question(cls, item: dict[str, Any]) -> dict[str, object]:
        cls._migrate_record(item)
        return {
            "question_id": str(item.get("question_id") or ""),
            "npc": str(item.get("npc") or ""),
            "addressed_actor": str(item.get("addressed_actor") or ""),
            "summary": str(item.get("summary") or ""),
            "required_items": cls.required_items(item.get("required_items")),
            "answered_item_ids": cls.ids(item.get("answered_item_ids")),
            "remaining_items": cls.remaining_items(item),
            "response_items": cls.response_items(item.get("response_items")),
            "condition_id": str(item.get("condition_id") or ""),
        }

    @classmethod
    def _trim_closed_history(cls, frame: Any, *, limit: int = 24) -> None:
        records = list(getattr(frame, "pending_npc_questions", []) or [])
        if len(records) <= limit:
            return
        open_records = [
            item
            for item in records
            if cls.clean(item.get("status") or "open").lower() == "open"
        ]
        closed_records = [item for item in records if item not in open_records]
        keep_closed = max(0, limit - len(open_records))
        frame.pending_npc_questions = [
            *(closed_records[-keep_closed:] if keep_closed else []),
            *open_records,
        ]

    @staticmethod
    def ids(value: object) -> list[str]:
        if isinstance(value, list):
            return NPCResponseWindowManager.unique(value)
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
        return NPCResponseWindowManager.unique(
            parsed if isinstance(parsed, list) else []
        )

    @staticmethod
    def required_items(value: object) -> list[dict[str, str]]:
        if isinstance(value, list):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(parsed, list):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            item_id = NPCResponseWindowManager.clean(item.get("item_id"))
            prompt = NPCResponseWindowManager.clean(item.get("prompt"))
            if not item_id or not prompt or item_id in seen:
                continue
            seen.add(item_id)
            result.append({"item_id": item_id, "prompt": prompt})
        return result

    @staticmethod
    def response_items(value: object) -> list[dict[str, str]]:
        if isinstance(value, list):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(parsed, list):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            item_id = NPCResponseWindowManager.clean(item.get("item_id"))
            kind = NPCResponseWindowManager.clean(item.get("kind")).lower()
            if (
                not item_id
                or item_id in seen
                or kind not in {"answer", "refuse", "cannot_answer"}
            ):
                continue
            seen.add(item_id)
            result.append({"item_id": item_id, "kind": kind})
        return result

    @classmethod
    def _migrate_record(cls, item: dict[str, Any]) -> None:
        """Convert one pre-ID save record, then remove the obsolete fields."""

        if "required_items" not in item:
            old_parts = cls._legacy_parts(item.get("required_parts"))
            migrated = [
                {"item_id": f"legacy_{index + 1}", "prompt": prompt}
                for index, prompt in enumerate(old_parts[:6])
            ]
            item["required_items"] = json.dumps(migrated, ensure_ascii=False)
            old_answered = set(cls._legacy_parts(item.get("answered_parts")))
            item["answered_item_ids"] = json.dumps(
                [
                    entry["item_id"]
                    for entry in migrated
                    if entry["prompt"] in old_answered
                ],
                ensure_ascii=False,
            )
            old_responses = cls._legacy_response_items(item.get("response_items"))
            response_by_prompt = {
                response["part"]: response["kind"] for response in old_responses
            }
            item["response_items"] = json.dumps(
                [
                    {
                        "item_id": entry["item_id"],
                        "kind": response_by_prompt[entry["prompt"]],
                    }
                    for entry in migrated
                    if entry["prompt"] in response_by_prompt
                ],
                ensure_ascii=False,
            )
        item.pop("required_parts", None)
        item.pop("answered_parts", None)

    @staticmethod
    def _legacy_parts(value: object) -> list[str]:
        if isinstance(value, list):
            return NPCResponseWindowManager.unique(value)
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
        return NPCResponseWindowManager.unique(
            parsed if isinstance(parsed, list) else []
        )

    @staticmethod
    def _legacy_response_items(value: object) -> list[dict[str, str]]:
        if isinstance(value, list):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(parsed, list):
            return []
        result: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            part = NPCResponseWindowManager.clean(item.get("part"))
            kind = NPCResponseWindowManager.clean(item.get("kind")).lower()
            if part and kind in {"answer", "refuse", "cannot_answer"}:
                result.append({"part": part, "kind": kind})
        return result

    @staticmethod
    def clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def same_name(left: str, right: str) -> bool:
        clean_left = "".join(str(left or "").split())
        clean_right = "".join(str(right or "").split())
        return bool(clean_left and clean_right and clean_left == clean_right)

    @staticmethod
    def unique(values: Any) -> list[str]:
        result: list[str] = []
        for value in values or []:
            clean = " ".join(str(value or "").split()).strip()
            if clean and clean not in result:
                result.append(clean)
        return result
