from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SceneChangeAuthorityReview:
    """一次自由文本局面变化所引用的结构化权限。"""

    valid: bool
    authority: dict[str, object]
    public_reply: str = ""
    public_facts: tuple[str, ...] = ()
    error_code: str = ""
    message: str = ""
    correction_hint: str = ""


class SceneChangeAuthorityPolicy:
    """只按结构化状态验证环境变化权限，不从叙事措辞猜规则。

    氛围、公开事实和 ``current_pressure`` 都不会进入这里的权限目录。
    它们可以说明画面，但不能仅凭一段文字获得新的作用范围或能力。
    """

    CHECK_FAILURE_KINDS = frozenset(
        {
            "attempt",
            "active_clock",
            "npc_commitment",
            "structured_hazard",
        }
    )
    SYSTEM_BEAT_KINDS = frozenset(
        {
            "active_clock",
            "scheduled_event",
            "structured_hazard",
        }
    )
    _PRESSURE_CLOCK_TYPES = frozenset({"threat", "villain", "dungeon", "boss"})
    _LIVE_RECORD_STATUSES = frozenset({"active", "due", "reached", "triggered"})
    _DUE_RECORD_STATUSES = frozenset({"due", "reached", "triggered"})
    _REQUIRED_FOLLOWUP_CONTEXT_KEY = "_gm_agent_required_followup_context"

    @classmethod
    def trusted_required_followup(
        cls,
        context: Any,
        tool_name: str,
    ) -> bool:
        """Return whether a successful receipt temporarily requires ``tool_name``."""

        metadata = dict(getattr(context, "metadata", {}) or {})
        value = metadata.get(cls._REQUIRED_FOLLOWUP_CONTEXT_KEY)
        if not isinstance(value, dict):
            return False
        source_tool = str(value.get("source_tool") or "").strip()
        required = {
            str(item or "").strip()
            for item in list(value.get("required_tools") or [])
            if str(item or "").strip()
        }
        clean_tool = str(tool_name or "").strip()
        if not source_tool or clean_tool not in required:
            return False
        if clean_tool == "commit_scene_response":
            return cls.normalized_scene_response_followup(
                value.get("scene_response_followup")
            ) is not None
        return True

    @classmethod
    def normalized_scene_response_followup(
        cls,
        value: object,
    ) -> dict[str, object] | None:
        """Validate an exact public result emitted by the preceding rule tool."""

        if not isinstance(value, dict):
            return None
        public_reply = str(value.get("public_reply") or "").strip()
        public_facts = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in list(value.get("public_facts") or [])
                if str(item or "").strip()
            )
        )
        if not public_reply or any(fact not in public_reply for fact in public_facts):
            return None
        return {
            "public_reply": public_reply,
            "public_facts": public_facts,
        }

    @classmethod
    def resolve_required_followup(
        cls,
        context: Any,
    ) -> SceneChangeAuthorityReview:
        metadata = dict(getattr(context, "metadata", {}) or {})
        value = metadata.get(cls._REQUIRED_FOLLOWUP_CONTEXT_KEY)
        if not isinstance(value, dict):
            return cls._invalid(
                {},
                "SCENE_RESPONSE_FOLLOWUP_REQUIRED",
                "当前事务没有已完成规则工具留下的场景回应义务。",
                "先由对应规则工具提交精确公开结果。",
            )
        payload = cls.normalized_scene_response_followup(
            value.get("scene_response_followup")
        )
        if payload is None:
            return cls._invalid(
                {},
                "SCENE_RESPONSE_RESULT_REQUIRED",
                "上一条回执没有登记可直接送达的精确公开结果。",
                "由规则工具在scene_response_followup中登记public_reply和逐字public_facts。",
            )
        return SceneChangeAuthorityReview(
            True,
            {
                "kind": "required_followup",
                "authority_ref": str(value.get("source_tool") or "").strip(),
            },
            public_reply=str(payload["public_reply"]),
            public_facts=tuple(payload["public_facts"]),
        )

    @classmethod
    def pending_system_beat_records(
        cls,
        context: Any,
    ) -> list[dict[str, object]]:
        """Expose only exact, due scene effects from trusted heartbeat context."""

        metadata = dict(getattr(context, "metadata", {}) or {})
        value = metadata.get("scene_change_authorities")
        if not isinstance(value, list):
            return []
        records: list[dict[str, object]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            record = cls._normalized_system_record(raw)
            if record is not None:
                records.append(record)
        return records

    @classmethod
    def has_pending_system_beat_authority(cls, context: Any) -> bool:
        return bool(cls.pending_system_beat_records(context))

    @classmethod
    def remember_receipt_authorities(
        cls,
        context: Any,
        receipt: Any,
    ) -> None:
        """Carry exact effects emitted by a successful structured tool receipt.

        Tool code is trusted; prose and model arguments are not.  This bridge lets
        a timer, hazard or other dedicated rule tool produce a one-transaction
        authority record without treating ambient scene text as permission.
        """

        if not bool(getattr(receipt, "ok", False)) or not bool(
            getattr(receipt, "state_changed", False)
        ):
            return
        result = getattr(receipt, "result", None)
        if not isinstance(result, dict):
            return
        singular = result.get("scene_change_authority")
        plural = result.get("scene_change_authorities")
        raw_records = (
            list(plural)
            if isinstance(plural, list)
            else ([singular] if singular is not None else [])
        )
        accepted_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue
            record = cls._normalized_system_record(raw)
            if record is None:
                continue
            record["source_tool"] = str(getattr(receipt, "tool_name", "") or "")
            accepted_by_key[
                (
                    str(record.get("kind") or ""),
                    str(record.get("authority_id") or ""),
                )
            ] = record
        if not accepted_by_key:
            return
        metadata = getattr(context, "metadata", None)
        if not isinstance(metadata, dict):
            return
        existing = [
            deepcopy(item)
            for item in list(metadata.get("scene_change_authorities") or [])
            if isinstance(item, dict)
        ]
        keyed: dict[tuple[str, str], dict[str, object]] = {}
        for item in [*existing, *accepted_by_key.values()]:
            normalized = cls._normalized_system_record(item)
            if normalized is None:
                continue
            key = (
                str(normalized.get("kind") or ""),
                str(normalized.get("authority_id") or ""),
            )
            keyed[key] = normalized
        metadata["scene_change_authorities"] = list(keyed.values())

    @classmethod
    def validate_check_failure(
        cls,
        *,
        app: Any,
        context: Any,
        value: object,
        failure_consequence: str = "",
    ) -> SceneChangeAuthorityReview:
        authority, error = cls._parse(value, allowed=cls.CHECK_FAILURE_KINDS)
        if error is not None:
            return error
        kind = str(authority["kind"])
        if kind == "attempt":
            if str(authority.get("authority_ref") or "").strip():
                return cls._invalid(
                    authority,
                    "ATTEMPT_AUTHORITY_HAS_EXTERNAL_REFERENCE",
                    "attempt类型只表示这次尝试本身没有达成目标。",
                    "清空authority_ref；外部人物、环境或持续威胁造成的后果改用对应的结构化权限来源。",
                )
            return SceneChangeAuthorityReview(True, authority)
        authority["proposed_effect"] = str(failure_consequence or "").strip()
        return cls._validate_external(
            app=app,
            context=context,
            authority=authority,
            allowed=cls.CHECK_FAILURE_KINDS,
        )

    @classmethod
    def validate_system_beat(
        cls,
        *,
        app: Any,
        context: Any,
        value: object,
        public_reply: str = "",
        public_facts: list[str] | None = None,
    ) -> SceneChangeAuthorityReview:
        review = cls.resolve_system_beat(
            app=app,
            context=context,
            value=value,
        )
        if not review.valid:
            return review
        allowed_reply = review.public_reply
        proposed_reply = str(public_reply or "").strip()
        allowed_facts = set(review.public_facts)
        proposed_facts = {
            str(item or "").strip()
            for item in list(public_facts or [])
            if str(item or "").strip()
        }
        if proposed_reply != allowed_reply or proposed_facts != allowed_facts:
            return cls._invalid(
                review.authority,
                "SCENE_CHANGE_EFFECT_NOT_AUTHORIZED",
                "主动节拍的公开变化与结构化记录中已授权的具体结果不一致。",
                "使用到期记录的精确public_reply和public_facts；若没有精确结果记录，就保持静默。",
            )
        return review

    @classmethod
    def resolve_system_beat(
        cls,
        *,
        app: Any,
        context: Any,
        value: object,
    ) -> SceneChangeAuthorityReview:
        """Resolve the canonical payload of one due structured scene effect."""

        authority, error = cls._parse(value, allowed=cls.SYSTEM_BEAT_KINDS)
        if error is not None:
            return error
        reference = str(authority.get("authority_ref") or "")
        record = cls._matching_trusted_record(
            dict(getattr(context, "metadata", {}) or {}),
            key="scene_change_authorities",
            reference=reference,
            kind=str(authority.get("kind") or ""),
            statuses=cls._DUE_RECORD_STATUSES,
        )
        if record is None:
            return cls._not_found(authority)
        scope_error = cls._record_scope_error(app, authority, record)
        if scope_error is not None:
            return scope_error
        allowed_reply = str(record.get("public_reply") or "").strip()
        allowed_facts = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in list(record.get("public_facts") or [])
                if str(item or "").strip()
            )
        )
        if not allowed_reply or any(fact not in allowed_reply for fact in allowed_facts):
            return cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_RECORD_INVALID",
                "结构化变化记录没有可直接送达的精确公开结果。",
                "由产生该结果的规则工具同时登记public_reply，并让每项public_facts逐字出现在其中。",
            )
        return SceneChangeAuthorityReview(
            True,
            authority,
            public_reply=allowed_reply,
            public_facts=allowed_facts,
        )

    @classmethod
    def _normalized_system_record(
        cls,
        value: dict[str, object],
    ) -> dict[str, object] | None:
        kind = str(value.get("kind") or value.get("source_kind") or "").strip()
        reference = str(
            value.get("authority_id")
            or value.get("clock_name")
            or value.get("event_id")
            or value.get("hazard_id")
            or ""
        ).strip()
        status = str(value.get("status") or "").strip().lower()
        public_reply = str(value.get("public_reply") or "").strip()
        public_facts = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in list(value.get("public_facts") or [])
                if str(item or "").strip()
            )
        )
        if (
            kind not in cls.SYSTEM_BEAT_KINDS
            or not reference
            or status not in cls._DUE_RECORD_STATUSES
            or not public_reply
            or any(fact not in public_reply for fact in public_facts)
        ):
            return None
        return {
            "authority_id": reference,
            "kind": kind,
            "status": status,
            "scene_id": str(value.get("scene_id") or "").strip(),
            "public_reply": public_reply,
            "public_facts": public_facts,
            **(
                {"source_tool": str(value.get("source_tool") or "").strip()}
                if str(value.get("source_tool") or "").strip()
                else {}
            ),
        }

    @classmethod
    def _parse(
        cls,
        value: object,
        *,
        allowed: frozenset[str],
    ) -> tuple[dict[str, object], SceneChangeAuthorityReview | None]:
        if not isinstance(value, dict):
            authority: dict[str, object] = {}
            return authority, cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_REQUIRED",
                "这项变化没有引用结构化权限来源。",
                "普通未达成结果使用kind=attempt；外部变化引用本事务已触发并精确登记结果的命刻、NPC承诺或结构化危险记录。",
            )
        authority = {
            "kind": str(value.get("kind") or "").strip(),
            "authority_ref": str(value.get("authority_ref") or "").strip(),
        }
        kind = str(authority["kind"])
        if kind not in allowed:
            return authority, cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_KIND_INVALID",
                f"未知或当前工具不可使用的局面变化权限类型【{kind or '未指定'}】。",
                "从当前工具schema列出的kind中选择，并提交准确authority_ref。",
            )
        return authority, None

    @classmethod
    def _validate_external(
        cls,
        *,
        app: Any,
        context: Any,
        authority: dict[str, object],
        allowed: frozenset[str],
    ) -> SceneChangeAuthorityReview:
        kind = str(authority.get("kind") or "")
        reference = str(authority.get("authority_ref") or "")
        if kind not in allowed:
            return cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_KIND_INVALID",
                f"当前事务不能使用权限类型【{kind}】。",
                "使用当前工具schema允许的结构化权限来源。",
            )
        if not reference:
            return cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_REFERENCE_REQUIRED",
                f"权限类型【{kind}】缺少authority_ref。",
                "重新读取权威状态，逐字填写现有记录的名称或ID。",
            )

        proposed_effect = str(authority.get("proposed_effect") or "").strip()
        if kind == "active_clock":
            manager = getattr(app, "clock_manager", None)
            if manager is None or not manager.exists(reference):
                return cls._not_found(authority)
            clock = manager.get(reference)
            if (
                str(getattr(clock, "status", "active") or "active").strip().lower()
                != "active"
                or str(getattr(clock, "clock_type", "") or "").strip().lower()
                not in cls._PRESSURE_CLOCK_TYPES
            ):
                return cls._not_found(authority)
            scene = getattr(getattr(app, "scene_manager", None), "current_scene", None)
            if (
                str(getattr(clock, "scope", "") or "").strip() == "scene"
                and str(getattr(clock, "scene_id", "") or "").strip()
                and str(getattr(clock, "scene_id", "") or "").strip()
                != str(getattr(scene, "scene_id", "") or "").strip()
            ):
                return cls._invalid(
                    authority,
                    "SCENE_CHANGE_AUTHORITY_SCOPE_MISMATCH",
                    "引用的场景命刻不属于当前聚焦场景。",
                    "使用当前场景的活动压力命刻，或保持静默。",
                )
        frame = getattr(getattr(app, "scene_frame_manager", None), "current_frame", None)
        if kind == "npc_commitment":
            manager = getattr(
                getattr(app, "scene_frame_manager", None),
                "npc_deferred_commitment_manager",
                None,
            )
            commitment = (
                manager.find_pending(frame, reference)
                if manager is not None
                else None
            )
            if (
                commitment is None
                or str(commitment.get("trigger_status") or "").strip().lower()
                != "reached"
            ):
                return cls._not_found(authority)
        metadata = dict(getattr(context, "metadata", {}) or {})
        record = cls._matching_trusted_record(
            metadata,
            key="check_failure_authorities",
            reference=reference,
            kind=kind,
        )
        if record is None:
            return cls._not_found(authority)
        scope_error = cls._record_scope_error(app, authority, record)
        if scope_error is not None:
            return scope_error
        allowed_effect = str(record.get("failure_consequence") or "").strip()
        if not allowed_effect or proposed_effect != allowed_effect:
            return cls._invalid(
                authority,
                "SCENE_CHANGE_EFFECT_NOT_AUTHORIZED",
                "检定失败后果与结构化权限记录中已授权的具体结果不一致。",
                "逐字使用该记录的failure_consequence；没有精确结果记录时只使用attempt的安全未达成结果。",
            )
        return SceneChangeAuthorityReview(True, authority)

    @staticmethod
    def _matching_trusted_record(
        metadata: dict[str, object],
        *,
        key: str,
        reference: str,
        kind: str,
        statuses: frozenset[str] | None = None,
    ) -> dict[str, object] | None:
        value = metadata.get(key)
        if not isinstance(value, list):
            return None
        for raw in value:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item_ref = str(
                item.get("authority_id")
                or item.get("clock_name")
                or item.get("commitment_id")
                or item.get("event_id")
                or item.get("hazard_id")
                or ""
            ).strip()
            item_kind = str(item.get("kind") or item.get("source_kind") or "").strip()
            status = str(item.get("status") or "active").strip().lower()
            allowed_statuses = statuses or SceneChangeAuthorityPolicy._LIVE_RECORD_STATUSES
            if item_ref == reference and item_kind == kind and status in allowed_statuses:
                return item
        return None

    @classmethod
    def _record_scope_error(
        cls,
        app: Any,
        authority: dict[str, object],
        record: dict[str, object],
    ) -> SceneChangeAuthorityReview | None:
        scene_id = str(record.get("scene_id") or "").strip()
        scene = getattr(getattr(app, "scene_manager", None), "current_scene", None)
        current_scene_id = str(getattr(scene, "scene_id", "") or "").strip()
        if scene_id and scene_id != current_scene_id:
            return cls._invalid(
                authority,
                "SCENE_CHANGE_AUTHORITY_SCOPE_MISMATCH",
                "引用的结构化权限记录不属于当前聚焦场景。",
                "使用当前场景的到期记录，或保持静默。",
            )
        return None

    @classmethod
    def _not_found(
        cls,
        authority: dict[str, object],
    ) -> SceneChangeAuthorityReview:
        reference = str(authority.get("authority_ref") or "")
        return cls._invalid(
            authority,
            "SCENE_CHANGE_AUTHORITY_NOT_FOUND",
            f"没有找到当前有效的结构化权限记录【{reference}】。",
            "从当前事务读取已触发且精确登记结果的命刻、NPC承诺或危险记录；公开事实和氛围压力不构成变化权限。",
        )

    @staticmethod
    def _invalid(
        authority: dict[str, object],
        error_code: str,
        message: str,
        correction_hint: str,
    ) -> SceneChangeAuthorityReview:
        return SceneChangeAuthorityReview(
            False,
            dict(authority),
            error_code=error_code,
            message=message,
            correction_hint=correction_hint,
        )
