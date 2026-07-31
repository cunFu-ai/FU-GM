from __future__ import annotations

from typing import Any, Protocol

from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)


class SceneToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMSceneToolService:
    """Atomic boundary for public scene prose and durable public facts."""

    _PRIVATE_MARKERS = (
        "scene_intent_contract",
        "story_outline",
        "GM私密",
        "后台控制",
        "不得原样输出",
    )

    def __init__(self, host: SceneToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_scene_state",
                description="读取当前场景的公开事实、待回应事项和GM私密局面，不修改状态。",
                handler=self.get_scene_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description=(
                    "原子提交无需角色自主决定的环境变化，或已经由专用规则工具完成裁定的GM公开回应，"
                    "并只把回应中逐字出现的持久事实写入当前场景。不能让NPC、集体或PC说话、决定或行动，"
                    "也不能代替检定、移动、场景切换或命刻工具。"
                ),
                handler=self.commit_scene_response,
                parameters=(
                    GMToolParameter(
                        "public_reply",
                        "string",
                        "将原样发送给玩家的完整公开回复。",
                        required=True,
                        schema_details={"minLength": 1},
                    ),
                    GMToolParameter(
                        "public_facts",
                        "array",
                        (
                            "可选。只填写从public_reply逐字复制的完整事实句；不能概括。"
                            "没有必须单独索引的持久事实、或无法逐字复制时省略或提交空数组。"
                        ),
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动或请求证据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        frame = app.scene_frame_manager.current_frame
        recent_scene_history = []
        for historical in list(getattr(app.scene_frame_manager, "history", []) or [])[-3:]:
            recent_scene_history.append(
                {
                    "scene_name": str(getattr(historical, "scene_name", "") or ""),
                    "location": str(getattr(historical, "location", "") or ""),
                    "public_facts": list(getattr(historical, "public_facts", []) or [])[-8:],
                    "established_facts": list(
                        getattr(historical, "established_facts", []) or []
                    )[-8:],
                    "committed_consequences": list(
                        getattr(historical, "committed_consequences", []) or []
                    )[-6:],
                }
            )
        world_public_facts: list[dict[str, object]] = []
        for subject, facts in app.world_state.subject_facts.items():
            clean_facts = [
                str(fact or "").strip()
                for fact in facts
                if str(fact or "").strip()
            ]
            if not clean_facts:
                continue
            world_public_facts.append(
                {
                    "subject": str(subject or "").strip(),
                    "facts": clean_facts[-4:],
                }
            )
        story_items = [
            {
                "item_id": item.item_id,
                "name": item.name,
                "description": item.description,
                "holder": item.holder,
                "location": item.location,
                "status": str(getattr(item.status, "value", item.status) or ""),
                "tags": list(item.tags),
            }
            for item in app.world_state.story_items.values()
        ]
        return {
            "active": bool(scene),
            "frame_active": bool(frame),
            "scene_id": str(getattr(scene, "scene_id", "") or getattr(frame, "source_scene_id", "")),
            "frame_source_scene_id": str(
                getattr(frame, "source_scene_id", "") or ""
            ),
            "name": str(getattr(scene, "name", "") or getattr(frame, "scene_name", "")),
            "location": str(getattr(scene, "location", "") or getattr(frame, "location", "")),
            "participants": list(getattr(scene, "participants", []) or []),
            "participant_locations": dict(
                getattr(scene, "participant_locations", {}) or {}
            ),
            "participant_positions": dict(
                getattr(scene, "participant_positions", {}) or {}
            ),
            "participant_activities": dict(
                getattr(scene, "participant_activities", {}) or {}
            ),
            "known_actor_locations": dict(
                getattr(app.scene_manager, "actor_locations", {}) or {}
            ),
            "known_actor_positions": dict(
                getattr(app.scene_manager, "actor_positions", {}) or {}
            ),
            "suspended_scenes": [
                {
                    "scene_id": item.scene_id,
                    "name": item.name,
                    "location": item.location,
                    "participants": list(item.participants),
                    "objective": item.objective,
                }
                for item in getattr(app.scene_manager, "suspended_scenes", [])
            ],
            "objective": str(getattr(scene, "objective", "") or ""),
            "current_pressure": str(getattr(frame, "current_pressure", "") or ""),
            "public_facts": list(getattr(frame, "public_facts", []) or [])[-8:],
            "revealed_clues": list(getattr(frame, "revealed_clues", []) or [])[-6:],
            "recent_beats": list(getattr(frame, "recent_beats", []) or [])[-3:],
            "unresolved_requests": list(getattr(frame, "unresolved_requests", []) or [])[-4:],
            "visible_elements": list(getattr(frame, "visible_elements", []) or [])[-12:],
            "npc_functions": list(getattr(frame, "npc_functions", []) or [])[-8:],
            "pending_npc_questions": [
                dict(item)
                for item in (getattr(frame, "pending_npc_questions", []) or [])
                if str(item.get("status") or "open") == "open"
            ][-4:],
            "open_conditions": [
                dict(item)
                for item in (getattr(frame, "open_conditions", []) or [])
                if str(item.get("status") or "open") == "open"
            ][-4:],
            "pending_npc_commitments": [
                dict(item)
                for item in app.scene_frame_manager.npc_deferred_commitment_manager.pending(
                    frame
                )
            ][-4:],
            "settled_exchanges": [
                dict(item)
                for item in (getattr(frame, "settled_exchanges", []) or [])
                if str(item.get("outcome") or "") in {"accepted", "rejected"}
            ][-4:],
            "recent_scene_history": recent_scene_history,
            "world_public_facts": world_public_facts[-24:],
            "story_items": story_items[-16:],
            "private_situation": {
                "stakes": str(getattr(frame, "stakes", "") or ""),
                "opposition_goal": str(getattr(frame, "opposition_goal", "") or ""),
                "dilemma": str(getattr(frame, "dilemma", "") or ""),
                "secrets": list(getattr(frame, "secrets", []) or [])[:5],
                "possible_reveals": list(getattr(frame, "possible_reveals", []) or [])[:5],
                "story_outline": list(getattr(frame, "story_outline", []) or [])[:6],
            },
        }

    def get_scene_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="get_scene_state",
            ok=True,
            result=self.state_summary(context),
        )

    def commit_scene_response(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"))
        if evidence_error is not None:
            return evidence_error
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        if not public_reply:
            return self._failure(
                "PUBLIC_REPLY_REQUIRED",
                "公开回复不能为空。",
                "先完成规则或NPC决策，再提交将原样发送给玩家的回复。",
            )
        if any(marker in public_reply for marker in self._PRIVATE_MARKERS):
            return self._failure(
                "PRIVATE_CONTEXT_LEAK",
                "公开回复包含明确的后台控制字段。",
                "保留自然叙事，删除后台字段名后重新提交。",
            )
        facts, error = self._validated_public_facts(arguments.get("public_facts"), public_reply)
        discarded_public_facts: list[str] = []
        if error is not None:
            if error.error_code != "FACT_NOT_PUBLICLY_SPOKEN":
                return error
            # public_facts is an optional search/index aid.  A paraphrase must
            # never enter authoritative state, but it also must not discard an
            # otherwise valid locked public beat.  Keep only exact excerpts;
            # the full public_reply remains the source of truth.
            raw_facts = arguments.get("public_facts")
            exact_facts: list[str] = []
            if isinstance(raw_facts, list):
                for item in raw_facts[:8]:
                    candidate = self._clean_multiline(item)
                    if not candidate:
                        continue
                    if candidate in public_reply:
                        if candidate not in exact_facts:
                            exact_facts.append(candidate)
                    elif candidate not in discarded_public_facts:
                        discarded_public_facts.append(candidate)
            facts = exact_facts

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.scene_manager.current_scene is None and app.scene_frame_manager.current_frame is None:
            return self._failure(
                "NO_ACTIVE_SCENE",
                "当前没有可提交事实的场景。",
                "先通过场景生命周期工具建立场景，不要把场景回复写入世界空白处。",
            )

        with runtime.transaction_lock:
            frame = self._ensure_frame(runtime, context)
            if frame is None:
                return self._failure(
                    "SCENE_FRAME_UNAVAILABLE",
                    "当前场景框架无法建立。",
                    "不要提交公开事实；让普通场景流程先恢复当前场景。",
                )
            for fact in facts:
                app.scene_frame_manager.record_public_fact(fact)
            app.scene_frame_manager.record_gm_beat(public_reply)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)

        system_beat = bool(context.metadata.get("system_gm_beat_request"))
        public_image = self._first_sentence(public_reply)
        material_summary = facts[0] if facts else ""
        if (
            system_beat
            and context.metadata.get("heartbeat_require_material_change")
            and not material_summary
        ):
            material_summary = public_image
        heartbeat_action = str(context.metadata.get("heartbeat_action") or "").strip()
        opposition_move = ""
        if system_beat and any(
            marker in heartbeat_action
            for marker in ("opposition", "villain", "threat", "敌", "反派", "威胁")
        ):
            opposition_move = material_summary

        return GMToolReceipt(
            tool_name="commit_scene_response",
            ok=True,
            result={
                "scene_id": str(getattr(app.scene_manager.current_scene, "scene_id", "") or ""),
                "public_facts": facts,
                "discarded_public_facts": discarded_public_facts,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not system_beat,
                    action_summary=(
                        ""
                        if system_beat
                        else str(context.metadata.get("current_message") or "").strip()
                    ),
                    consequence=(
                        "" if opposition_move else material_summary
                    ),
                    opposition_move=opposition_move,
                    public_image=public_image,
                    local_question_changed=bool(
                        context.metadata.get("heartbeat_require_local_change")
                    ),
                    local_question_resolved=bool(
                        context.metadata.get("heartbeat_require_local_resolution")
                    ),
                    gm_beat_purpose=heartbeat_action if system_beat else "",
                )
            ],
        )

    @staticmethod
    def _ensure_frame(runtime: Any, context: GMToolExecutionContext) -> Any:
        app = runtime.app
        if app.scene_frame_manager.current_frame is not None:
            return app.scene_frame_manager.current_frame
        scene = app.scene_manager.current_scene
        if scene is None:
            return None
        contract = getattr(
            getattr(app.story_arc_manager.state, "current_pacing_plan", None),
            "dramatic_contract",
            None,
        )
        return app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat=str(context.metadata.get("recent_public_context") or ""),
            world_state=app.world_state,
            character_manager=app.character_manager,
            contract=contract,
        )

    @classmethod
    def _validated_public_facts(
        cls,
        value: object,
        public_reply: str,
    ) -> tuple[list[str], GMToolReceipt | None]:
        if value is None:
            return [], None
        if not isinstance(value, list):
            return [], cls._failure(
                "PUBLIC_FACTS_MUST_BE_ARRAY",
                "public_facts必须是数组。",
                "没有持久事实时提交空数组。",
            )
        facts: list[str] = []
        for item in value[:8]:
            fact = cls._clean_multiline(item)
            if not fact:
                continue
            if fact not in public_reply:
                return [], GMToolReceipt(
                    tool_name="commit_scene_response",
                    ok=False,
                    result={"retry_arguments_patch": {"public_facts": []}},
                    error_code="FACT_NOT_PUBLICLY_SPOKEN",
                    message=f"事实「{fact[:80]}」没有逐字出现在公开回复中。",
                    correction_hint=(
                        "原样保留public_reply，将public_facts改为空数组[]后重试；"
                        "不能写入玩家没听见的概括句。"
                    ),
                    retryable=True,
                    public_fallback_reply="这一步还没有写入场景状态。",
                )
            if fact not in facts:
                facts.append(fact)
        return facts, None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(
                "EVIDENCE_NOT_IN_CURRENT_MESSAGE",
                "evidence不是当前消息中的逐字连续片段。",
                "从current_message逐字复制依据，不得使用路由摘要。",
            )
        return None

    @staticmethod
    def _clean_multiline(value: object) -> str:
        return "\n".join(
            line.strip()
            for line in str(value or "").replace("\r\n", "\n").split("\n")
            if line.strip()
        ).strip()

    @staticmethod
    def _first_sentence(value: object) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            return ""
        for marker in ("。", "！", "？", "!", "?"):
            if marker in text:
                return text.split(marker, 1)[0].strip() + marker
        return text[:300]

    @staticmethod
    def _failure(code: str, message: str, hint: str) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="commit_scene_response",
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            public_fallback_reply="这一步还没有写入场景状态。",
        )
