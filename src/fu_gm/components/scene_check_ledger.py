from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fu_gm.models import Action, ActionResolution


class SceneCheckLedger:
    """Keep a compact, scene-scoped history of finalized uncertain actions.

    The ledger records typed check contracts and outcomes rather than trying to
    infer repeated intent from prose.  Semantic models can then decide whether
    a later approach is materially different, while Python only preserves the
    authoritative evidence and its visibility boundary.
    """

    LIMIT = 12
    MODEL_LIMIT = 8
    RETRY_BASIS_KINDS = frozenset(
        {
            "new_tool",
            "new_evidence",
            "new_position",
            "accepted_risk",
            "scene_change",
            "new_goal",
        }
    )
    _ATTRIBUTE_LABELS = {
        "DEX": "敏捷",
        "INS": "洞察",
        "MIG": "力量",
        "WLP": "意志",
    }

    @classmethod
    def record_resolution(
        cls,
        frame: object | None,
        resolution: ActionResolution,
    ) -> dict[str, object] | None:
        if frame is None or resolution.payload.get("check_result_provisional"):
            return None
        committed = resolution.payload.get("committed_source_action")
        action = committed if isinstance(committed, Action) else resolution.action
        roll = resolution.payload.get("roll")
        if roll is None or not bool(action.parameters.get("scene_check_planned")):
            return None

        existing_id = str(
            resolution.payload.get("_scene_check_attempt_id") or ""
        ).strip()
        attempts = cls._attempts(frame)
        if existing_id:
            return next(
                (
                    item
                    for item in attempts
                    if str(item.get("attempt_id") or "") == existing_id
                ),
                None,
            )

        actor = str(
            getattr(roll, "actor", "") or action.parameters.get("actor") or ""
        ).strip()
        attributes = [
            cls._ATTRIBUTE_LABELS.get(str(item or "").strip(), str(item or "").strip())
            for item in list(getattr(roll, "attributes", ()) or action.parameters.get("attributes") or [])
            if str(item or "").strip()
        ][:2]
        success = bool(getattr(roll, "success", False))
        failure_authority = action.parameters.get("failure_authority")
        failure_kind = (
            str(failure_authority.get("kind") or "attempt").strip()
            if isinstance(failure_authority, dict)
            else "attempt"
        )
        information = [
            " ".join(str(item or "").split()).strip()
            for item in list(resolution.payload.get("information") or [])
            if " ".join(str(item or "").split()).strip()
        ]
        if success:
            result_summary = "；".join(information) or str(
                action.parameters.get("success_observation") or ""
            ).strip()
        else:
            result_summary = str(
                action.parameters.get("failure_consequence")
                or action.parameters.get("failure_stakes")
                or ""
            ).strip()

        material_change = bool(
            resolution.payload.get("clock_change")
            or resolution.payload.get("clock_progress")
            or resolution.payload.get("success_state_changes")
            or action.parameters.get("success_state_changes")
            or action.parameters.get("success_transition")
            or (success and information)
            or (not success and failure_kind != "attempt")
        )
        record: dict[str, object] = {
            "attempt_id": f"check-{uuid4()}",
            "actor": actor,
            "action_type": str(getattr(action.action_type, "value", action.action_type)),
            "interaction_kind": str(
                action.parameters.get("scene_check_interaction_kind") or ""
            ).strip(),
            "target": str(action.parameters.get("target") or "周边环境").strip(),
            "purpose": str(
                action.parameters.get("declared_action_goal")
                or action.parameters.get("reasoning")
                or ""
            ).strip(),
            "check_label": str(
                action.parameters.get("scene_investigation_label") or ""
            ).strip(),
            "attributes": attributes,
            "difficulty": int(
                getattr(roll, "target_number", 0)
                or action.parameters.get("target_number")
                or 0
            ),
            "total": int(getattr(roll, "total", 0) or 0),
            "outcome": "success" if success else "failure",
            "critical_success": bool(getattr(roll, "critical_success", False)),
            "fumble": bool(getattr(roll, "fumble", False)),
            "failure_authority": failure_kind,
            "material_change": material_change,
            "result_summary": result_summary[:600],
            "public": False,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        attempts.append(record)
        del attempts[:-cls.LIMIT]
        resolution.payload["_scene_check_attempt_id"] = record["attempt_id"]
        return record

    @classmethod
    def publish_resolution(
        cls,
        frame: object | None,
        resolution: ActionResolution,
    ) -> bool:
        if frame is None or not str(resolution.payload.get("_scene_check_attempt_id") or ""):
            return False
        attempt_id = str(resolution.payload["_scene_check_attempt_id"])
        for item in cls._attempts(frame):
            if str(item.get("attempt_id") or "") != attempt_id:
                continue
            if item.get("public") is True:
                return False
            item["public"] = True
            return True
        return False

    @classmethod
    def model_snapshot(
        cls,
        frame: object | None,
        *,
        public_only: bool,
    ) -> list[dict[str, object]]:
        attempts = cls._attempts(frame) if frame is not None else []
        rows = [item for item in attempts if not public_only or item.get("public") is True]
        keys = (
            "attempt_id",
            "actor",
            "action_type",
            "target",
            "purpose",
            "check_label",
            "attributes",
            "difficulty",
            "total",
            "outcome",
            "failure_authority",
            "material_change",
            "interaction_kind",
            "result_summary",
            "public",
        )
        return [
            {key: item[key] for key in keys if item.get(key) not in (None, "", [], {})}
            for item in rows[-cls.MODEL_LIMIT :]
        ]

    @classmethod
    def latest_retry_blocker(
        cls,
        frame: object | None,
        *,
        target: str,
        interaction_kind: str,
    ) -> dict[str, object] | None:
        """Return the latest public failure that exhausted this approach.

        Python compares typed scene-check identity only.  It does not infer
        synonyms from player prose; a materially different retry is declared
        explicitly through ``retry_basis`` and remains the GM model's semantic
        responsibility.
        """

        clean_target = str(target or "").strip()
        clean_kind = str(interaction_kind or "").strip()
        if frame is None or not clean_target:
            return None
        for item in reversed(cls.model_snapshot(frame, public_only=True)):
            if str(item.get("target") or "").strip() != clean_target:
                continue
            if (
                clean_kind
                and str(item.get("interaction_kind") or "").strip()
                not in {"", clean_kind}
            ):
                continue
            if str(item.get("outcome") or "") != "failure":
                return None
            if str(item.get("failure_authority") or "attempt") != "attempt":
                return None
            if bool(item.get("material_change")):
                return None
            return dict(item)
        return None

    @staticmethod
    def _attempts(frame: object) -> list[dict[str, object]]:
        raw = getattr(frame, "recent_check_attempts", None)
        if not isinstance(raw, list):
            raw = []
        normalized = [dict(item) for item in raw if isinstance(item, dict)]
        # Always attach the normalized list before returning it.  Equality is
        # not enough here: an empty copy compares equal to the frame's empty
        # list but mutations would otherwise be lost.
        setattr(frame, "recent_check_attempts", normalized)
        return normalized


__all__ = ["SceneCheckLedger"]
