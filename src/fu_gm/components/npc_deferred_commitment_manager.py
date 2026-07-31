from __future__ import annotations

import re
from typing import Any


class NPCDeferredCommitmentManager:
    """Track finite public actions an NPC promises to perform shortly.

    These commitments are not player-facing bargains.  A guard promising to
    report to the chair and return with an answer creates an obligation for the
    GM/NPC, not another condition the heroes must satisfy.  Structured speech
    plan fields are authoritative; a deliberately narrow prose fallback keeps
    older providers and loaded campaigns compatible.
    """

    _ACTION = re.compile(
        r"(?:我|本人)(?:现在就|这就|马上|立刻|会|先|这便|便)?"
        r"[^。！？；;]{0,36}?"
        r"(?P<action>(?:把[^。！？；;]{0,24})?"
        r"(?:通报|禀报|询问|查问|核对|找来|取来|拿来|带来|传话|请示)"
        r"[^。！？；;]{0,36})"
    )
    _RETURN_RESULT = re.compile(
        r"(?P<result>(?:一有|有了|得到|拿到|等到|待到|"
        r"[\u4e00-\u9fffA-Za-z0-9]{1,12}有)"
        r"[^。！？；;]{0,10}?(?:答复|回复|消息|结果|回信)"
        r"[^。！？；;]{0,28}?(?:告诉|转告|通知|回报|当面说|回来告诉))"
    )
    _FUTURE_ONLY = re.compile(
        r"(?:一有|等|待|之后|稍后|回来后|有了以后|会|将|再)"
        r"[^。！？]{0,22}(?:告诉|转告|通知|回报|答复)"
    )
    _FULFILLED = re.compile(
        r"(?:答复|回复|消息|结果|回信)(?:已经)?(?:到了|来了|有了|是|为)|"
        r"(?:带回|拿到|得到|收到了?)[^。！？]{0,16}(?:答复|回复|消息|结果|回信)|"
        r"(?:会长|首领|队长|议会|上级|掌柜)[^。！？]{0,10}(?:答复|回复|决定|说(?:道|的是|了)?)"
    )

    @classmethod
    def pending(cls, frame: Any | None) -> list[dict[str, str]]:
        if frame is None:
            return []
        return [
            item
            for item in getattr(frame, "deferred_npc_commitments", [])
            if str(item.get("status") or "pending").strip().lower() == "pending"
        ]

    @classmethod
    def find_pending(
        cls,
        frame: Any | None,
        commitment_id: str,
    ) -> dict[str, str] | None:
        """Return one exact unresolved commitment without interpreting prose."""

        requested_id = cls._clean(commitment_id)
        if not requested_id:
            return None
        return next(
            (
                item
                for item in cls.pending(frame)
                if cls._clean(item.get("commitment_id")) == requested_id
            ),
            None,
        )

    @classmethod
    def mark_trigger_reached(
        cls,
        frame: Any | None,
        *,
        commitment_id: str,
        actor: str,
        evidence: str,
        location: str,
        responder: str,
    ) -> dict[str, str] | None:
        """Record that an exact public promise is now due.

        The semantic tool caller chooses the commitment and responder.  This
        mutation boundary only accepts an existing identifier and records the
        trigger; it never tries to infer a promise from keywords.
        """

        item = cls.find_pending(frame, commitment_id)
        clean_responder = cls._clean(responder)
        if item is None or not clean_responder:
            return None
        trigger_status = cls._clean(item.get("trigger_status")).lower()
        if trigger_status not in {"", "waiting"}:
            return None
        item["trigger_status"] = "reached"
        item["triggered_by"] = cls._clean(actor)[:120]
        item["trigger_evidence"] = cls._clean(evidence)[:500]
        item["trigger_location"] = cls._clean(location)[:220]
        item["trigger_responder"] = clean_responder[:160]
        return item

    @classmethod
    def update_from_public_answer(
        cls,
        frame: Any | None,
        *,
        npc: str,
        public_statement: str,
        speech_plan: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """Resolve an old commitment, then record a newly promised one."""

        if frame is None:
            return None
        plan = dict(speech_plan or {})
        cls.resolve_from_public_answer(
            frame,
            npc=npc,
            public_statement=public_statement,
            speech_plan=plan,
        )
        return cls.record_from_public_answer(
            frame,
            npc=npc,
            public_statement=public_statement,
            speech_plan=plan,
        )

    @classmethod
    def record_from_public_answer(
        cls,
        frame: Any | None,
        *,
        npc: str,
        public_statement: str,
        speech_plan: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        if frame is None:
            return None
        clean_npc = cls._clean(npc)
        clean_statement = cls._clean(public_statement)
        plan = dict(speech_plan or {})
        action = cls._clean(plan.get("deferred_action"))
        result = cls._clean(plan.get("deferred_result"))
        trigger = cls._clean(plan.get("deferred_trigger"))
        if not action or not result:
            legacy = cls._legacy_commitment(clean_statement)
            action = action or legacy.get("action", "")
            result = result or legacy.get("result", "")
            trigger = trigger or legacy.get("trigger", "")
        if not clean_npc or not action or not result:
            return None
        records = getattr(frame, "deferred_npc_commitments", None)
        if not isinstance(records, list):
            return None
        for existing in reversed(records):
            if str(existing.get("status") or "pending") != "pending":
                continue
            if cls._same_npc(clean_npc, cls._clean(existing.get("npc"))) and (
                cls._overlaps(action, cls._clean(existing.get("action")))
                or cls._overlaps(result, cls._clean(existing.get("promised_result")))
            ):
                existing["public_statement"] = clean_statement[:500]
                existing["action"] = action[:220]
                existing["promised_result"] = result[:220]
                existing["trigger"] = (trigger or "下一次合理的GM主动节拍")[:160]
                existing.setdefault("trigger_status", "waiting")
                return existing
        record = {
            "commitment_id": f"{getattr(frame, 'scene_key', 'scene')}-npc-promise-{len(records) + 1}",
            "npc": clean_npc,
            "public_statement": clean_statement[:500],
            "action": action[:220],
            "promised_result": result[:220],
            "trigger": (trigger or "下一次合理的GM主动节拍")[:160],
            "trigger_status": "waiting",
            "status": "pending",
        }
        records.append(record)
        if len(records) > 8:
            del records[:-8]
        return record

    @classmethod
    def resolve_from_public_answer(
        cls,
        frame: Any | None,
        *,
        npc: str,
        public_statement: str,
        speech_plan: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        if frame is None:
            return []
        clean_npc = cls._clean(npc)
        clean_statement = cls._clean(public_statement)
        plan = dict(speech_plan or {})
        outcome = cls._clean(plan.get("commitment_outcome")).lower()
        requested_id = cls._clean(plan.get("commitment_id"))
        resolved: list[dict[str, str]] = []
        for item in cls.pending(frame):
            item_id = cls._clean(item.get("commitment_id"))
            same_npc = cls._same_npc(clean_npc, cls._clean(item.get("npc")))
            explicit_match = bool(requested_id and requested_id == item_id)
            if outcome in {"fulfilled", "cancelled"} and (explicit_match or same_npc):
                item["status"] = "resolved" if outcome == "fulfilled" else "cancelled"
                item["trigger_status"] = (
                    "fulfilled" if outcome == "fulfilled" else "cancelled"
                )
                item["resolution"] = clean_statement[:500]
                resolved.append(item)
                continue
            if not clean_statement or cls._FUTURE_ONLY.search(clean_statement):
                continue
            promised_result = cls._clean(item.get("promised_result"))
            related = same_npc or cls._result_subject_supported(promised_result, clean_statement)
            if related and cls._FULFILLED.search(clean_statement):
                item["status"] = "resolved"
                item["trigger_status"] = "fulfilled"
                item["resolution"] = clean_statement[:500]
                resolved.append(item)
        return resolved

    @classmethod
    def apply_semantic_updates(
        cls,
        frame: Any | None,
        *,
        public_beat: str,
        updates: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        """Commit Luna-reviewed fulfilment decisions to scene state.

        The semantic extractor has already decided whether the promised result
        was actually delivered.  Revalidate identity and verbatim evidence at
        the mutation boundary so a malformed metadata object cannot silently
        clear an unrelated obligation.
        """

        if frame is None or not updates:
            return []
        clean_beat = cls._clean(public_beat)
        pending = {
            cls._clean(item.get("commitment_id")): item
            for item in cls.pending(frame)
            if cls._clean(item.get("commitment_id"))
        }
        resolved: list[dict[str, str]] = []
        seen: set[str] = set()
        for update in updates:
            if not isinstance(update, dict):
                continue
            commitment_id = cls._clean(update.get("commitment_id"))
            item = pending.get(commitment_id)
            if item is None or commitment_id in seen:
                continue
            npc = cls._clean(update.get("npc"))
            outcome = cls._clean(update.get("outcome")).lower()
            evidence = cls._clean(update.get("evidence"))
            if outcome not in {"fulfilled", "cancelled"}:
                continue
            if not cls._same_npc(npc, cls._clean(item.get("npc"))):
                continue
            if not evidence or evidence not in clean_beat:
                continue
            item["status"] = "resolved" if outcome == "fulfilled" else "cancelled"
            item["trigger_status"] = (
                "fulfilled" if outcome == "fulfilled" else "cancelled"
            )
            item["resolution"] = evidence[:500]
            item["resolution_source"] = "luna_scene_semantics"
            seen.add(commitment_id)
            resolved.append(item)
        return resolved

    @classmethod
    def _legacy_commitment(cls, statement: str) -> dict[str, str]:
        action_match = cls._ACTION.search(statement)
        result_match = cls._RETURN_RESULT.search(statement)
        if not action_match or not result_match:
            return {}
        return {
            "action": cls._clean(action_match.group("action")),
            "result": cls._clean(result_match.group("result")),
            "trigger": "NPC完成所说的短期行动后",
        }

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _same_npc(left: str, right: str) -> bool:
        return bool(left and right and (left == right or left in right or right in left))

    @staticmethod
    def _overlaps(left: str, right: str) -> bool:
        compact_left = re.sub(r"\W", "", left)
        compact_right = re.sub(r"\W", "", right)
        return bool(
            compact_left
            and compact_right
            and (
                compact_left in compact_right
                or compact_right in compact_left
                or len(set(compact_left) & set(compact_right))
                >= max(4, min(len(set(compact_left)), len(set(compact_right))) // 2)
            )
        )

    @staticmethod
    def _result_subject_supported(result: str, statement: str) -> bool:
        subjects = re.findall(
            r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(?=.{0,4}(?:答复|回复|消息|决定))",
            result,
        )
        return any(subject in statement for subject in subjects)
