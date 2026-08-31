from __future__ import annotations

from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import CampaignStateTransaction
from fu_gm.components.scene_change_authority import SceneChangeAuthorityPolicy
from fu_gm.components.scene_creative_writer import SceneCreativeWriterError
from fu_gm.components.scene_moment_policy import SceneMomentPolicy
from fu_gm.components.gm_semantic_tool_attestation import (
    current_semantic_tool_call_attested,
)
from fu_gm.components.table_working_brief import TableWorkingBriefManager
from fu_gm.components.scene_check_ledger import SceneCheckLedger
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

    @staticmethod
    def _player_character_control_map(runtime: Any) -> dict[str, list[str]]: ...


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
                description="只读获取当前场景的公开事实、待回应事项和GM私密局面。",
                handler=self.get_scene_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_scene_response",
                description=(
                    "原子提交专用规则工具通过required-followup交付的公开回应，"
                    "系统到期结构化记录中的精确环境变化，或受信场景开场/自由场景节拍中的可观察变化。"
                    "受信场景开场中，核心GM可直接提交完整表达已授权事实的最终文本；"
                    "其他路径由DeepSeek场景作者依据事实与表达方向生成玩家可见文本，"
                    "并把通过语义保真校验的持久事实写入当前场景。行动主体范围为非人格化环境；"
                    "NPC与集体回应、PC行动、检定、移动、场景切换和命刻变化分别使用对应专用工具。"
                ),
                handler=self.commit_scene_response,
                parameters=(
                    GMToolParameter(
                        "public_reply",
                        "string",
                        "离线模式的可选后备公开回复。",
                    ),
                    GMToolParameter(
                        "public_facts",
                        "array",
                        (
                            "可选。每项为public_reply完整表达、需要持久记录的规范事实句。"
                            "允许公开文本自然改写，但不得改变主语、状态、时序或因果；"
                            "无需单独索引持久事实时省略或提交空数组。"
                            "受信自由场景主动节拍中，事实主体必须是环境、物件或合法NPC，"
                            "只写其新变化；不得重演玩家动作或替玩家观察、判断与操作。"
                        ),
                        schema_details={
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 8,
                        },
                    ),
                    GMToolParameter(
                        "creative_direction",
                        "string",
                        "可选表达方向；不得在这里新增事实或替玩家行动。",
                    ),
                    GMToolParameter(
                        "awaits_player_response",
                        "boolean",
                        (
                            "离线后备语义标记：公开回应是否真正提出了尚需玩家回答的问题或决定。"
                            "在线表达由场景作者返回该结构化标记；不要根据问号或关键词猜测。"
                        ),
                    ),
                    GMToolParameter(
                        "change_authority",
                        "object",
                        (
                            "系统主动节拍提交非人格化环境变化时必填。kind使用active_clock、"
                            "scheduled_event或structured_hazard，authority_ref逐字引用heartbeat context中"
                            "本事务已触发且带有精确公开结果的结构化记录；普通玩家消息触发的现场回应可省略。"
                            "受信scene_opening请求或内部签发的自由场景物质变化节拍已获得场景授权，也可省略。"
                        ),
                        schema_details={
                            "additionalProperties": False,
                            "required": ["kind", "authority_ref"],
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        "active_clock",
                                        "scheduled_event",
                                        "structured_hazard",
                                    ],
                                },
                                "authority_ref": {"type": "string", "minLength": 1},
                            },
                        },
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动或请求证据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="commit_scene_fixture_action",
                description=(
                    "原子提交固定场景物件的观察、操作、损坏、修复或停用。"
                    "门闸、锁链、拉杆、祭坛和控制台属于场景固定物，不是可携带剧情物件；"
                    "首次使用时必须用authority_ref逐字引用当前场景已经准备或公开的依据，"
                    "工具会在同一事务中登记并执行，不需要先创建再操作。"
                ),
                handler=self.commit_scene_fixture_action,
                parameters=(
                    GMToolParameter("actor", "string", "执行动作的玩家角色。", required=True),
                    GMToolParameter(
                        "operation",
                        "string",
                        "固定物件操作。",
                        required=True,
                        enum=("inspect", "operate", "damage", "repair", "disable"),
                    ),
                    GMToolParameter("fixture_name", "string", "固定物件的公开名称。", required=True),
                    GMToolParameter("fixture_id", "string", "可选：当前场景中已有的固定物件ID。"),
                    GMToolParameter("description", "string", "首次登记时已经公开的简短描述。"),
                    GMToolParameter(
                        "authority_ref",
                        "string",
                        "首次登记必填：逐字复制当前场景visible_elements、public_facts或established_facts中的一项依据。",
                    ),
                    GMToolParameter(
                        "state_note",
                        "string",
                        "无需检定且动作已经完成时记录的新状态；inspect可省略。",
                    ),
                    GMToolParameter("public_result", "string", "只有GM裁定产生玩家原话之外的新结果时填写。"),
                    GMToolParameter("public_fact", "string", "可选：已经公开、需要持续索引的一句事实。"),
                    GMToolParameter(
                        "continue_with_check",
                        "boolean",
                        "动作结果不确定时设为true；只登记固定物件并强制继续属性检定，不提前改变其状态。",
                    ),
                    GMToolParameter("evidence", "string", "当前玩家消息中的逐字行动证据。", required=True, source="current_message"),
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
                "current_state": item.current_state,
                "tags": list(item.tags),
            }
            for item in app.world_state.story_items.values()
        ]
        active_scene_branches = []
        for item in [
            scene,
            *list(getattr(app.scene_manager, "suspended_scenes", []) or []),
        ]:
            if item is None:
                continue
            active_scene_branches.append(
                {
                    "scene_id": item.scene_id,
                    "name": item.name,
                    "location": item.location,
                    "participants": list(item.participants),
                    "participant_locations": dict(item.participant_locations),
                    "participant_positions": dict(item.participant_positions),
                    "objective": item.objective,
                    "camera_focused": item is scene,
                }
            )
        return {
            "active": bool(scene),
            "frame_active": bool(frame),
            "current_scene_is_camera_focus": True,
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
                    "participant_locations": dict(item.participant_locations),
                    "participant_positions": dict(item.participant_positions),
                    "objective": item.objective,
                }
                for item in getattr(app.scene_manager, "suspended_scenes", [])
            ],
            "active_scene_branches": active_scene_branches,
            "objective": str(getattr(scene, "objective", "") or ""),
            "current_pressure": str(getattr(frame, "current_pressure", "") or ""),
            "due_scene_changes": SceneChangeAuthorityPolicy.pending_system_beat_records(
                context
            ),
            "committed_consequences": list(
                getattr(frame, "committed_consequences", []) or []
            )[-6:],
            "public_facts": list(getattr(frame, "public_facts", []) or [])[-8:],
            "revealed_clues": list(getattr(frame, "revealed_clues", []) or [])[-6:],
            "recent_beats": list(getattr(frame, "recent_beats", []) or [])[-3:],
            "recent_check_attempts": SceneCheckLedger.model_snapshot(
                frame,
                public_only=False,
            ),
            "working_brief": TableWorkingBriefManager.model_snapshot(
                frame,
                include_last_public_reply=not bool(
                    context.metadata.get("recent_public_messages")
                ),
            ),
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
            "scene_fixtures": [
                {
                    "fixture_id": fixture.fixture_id,
                    "name": fixture.name,
                    "description": fixture.description,
                    "current_state": fixture.current_state,
                    "allowed_operations": list(fixture.allowed_operations),
                    "tags": list(fixture.tags),
                }
                for fixture in (
                    getattr(scene, "scene_fixtures", {}) or {}
                ).values()
            ],
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

    def commit_scene_fixture_action(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "commit_scene_fixture_action"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
        )
        if evidence_error is not None:
            evidence_error.tool_name = tool_name
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app.scene_manager.active_scenes():
            return self._failure(
                "NO_ACTIVE_SCENE",
                "当前没有可操作固定物件的场景。",
                "先建立或恢复角色所在场景。",
                tool_name=tool_name,
            )
        if app.conflict_manager.state.active:
            return self._failure(
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能绕过当前回合直接操作场景固定物。",
                "通过合法冲突行动推进目标或执行对应技能。",
                tool_name=tool_name,
            )
        actor = self._clean_multiline(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                "UNKNOWN_ACTOR",
                f"没有找到玩家角色【{actor or '未指定'}】。",
                "读取当前玩家角色后重新提交。",
                tool_name=tool_name,
            )
        controls = self.host._player_character_control_map(runtime)
        owners = [player for player, heroes in controls.items() if actor in heroes]
        if owners and context.speaker not in owners:
            return self._failure(
                "ACTOR_NOT_CONTROLLED_BY_SPEAKER",
                f"【{context.speaker}】不能替【{actor}】提交这个动作。",
                "等待该角色的操作者本人发言。",
                tool_name=tool_name,
            )
        scene = app.scene_manager.current_scene
        if scene is None or actor not in scene.participants:
            scene = next(
                (
                    branch
                    for branch in app.scene_manager.suspended_scenes
                    if actor in branch.participants
                ),
                None,
            )
        if scene is None:
            return self._failure(
                "ACTOR_NOT_IN_ACTIVE_SCENE",
                f"【{actor}】不在任何仍活动的场景分支中。",
                "先建立或恢复角色真实所在场景。",
                tool_name=tool_name,
            )
        if scene.scene_type.value == "conflict":
            return self._failure(
                "CONFLICT_ACTION_REQUIRED",
                "冲突中不能绕过当前回合直接操作场景固定物。",
                "通过合法冲突行动推进目标或执行对应技能。",
                tool_name=tool_name,
            )

        operation = self._clean_multiline(arguments.get("operation")).lower()
        allowed_operations = {"inspect", "operate", "damage", "repair", "disable"}
        if operation not in allowed_operations:
            return self._failure(
                "INVALID_FIXTURE_OPERATION",
                "固定物件操作类型无效。",
                "使用工具schema列出的操作。",
                tool_name=tool_name,
            )
        fixture_name = self._clean_multiline(arguments.get("fixture_name"))
        fixture_id = self._clean_multiline(arguments.get("fixture_id"))
        fixture = app.scene_manager.find_scene_fixture(
            fixture_id=fixture_id,
            name=fixture_name,
            scene=scene,
        )
        authority_ref = self._clean_multiline(arguments.get("authority_ref"))
        current_frame = app.scene_frame_manager.current_frame
        frame = (
            current_frame
            if current_frame is not None
            and str(current_frame.source_scene_id or "").strip()
            == str(scene.scene_id or "").strip()
            else app.scene_frame_manager.suspended_frames.get(
                str(scene.scene_id or "").strip()
            )
        )
        if fixture is None:
            authorities = [
                self._clean_multiline(item)
                for item in [
                    *list(getattr(frame, "visible_elements", []) or []),
                    *list(getattr(frame, "public_facts", []) or []),
                    *list(getattr(frame, "established_facts", []) or []),
                ]
                if self._clean_multiline(item)
            ]
            if (
                not authority_ref
                or authority_ref not in authorities
                or not fixture_name
                or fixture_name not in authority_ref
            ):
                return self._failure(
                    "SCENE_FIXTURE_AUTHORITY_REQUIRED",
                    f"当前场景尚未权威建立固定物件【{fixture_name or '未指定'}】。",
                    "逐字引用当前场景已准备或公开的依据；不要因玩家猜测而制造新机关。",
                    tool_name=tool_name,
                )

        continue_with_check = arguments.get("continue_with_check") is True
        state_note = self._clean_multiline(arguments.get("state_note"))
        if not continue_with_check and operation == "operate" and not state_note:
            return self._failure(
                "FIXTURE_STATE_REQUIRED",
                "确定完成的操作缺少固定物件的新状态。",
                "填写动作完成后可持续追踪的简短状态；若结果不确定则设continue_with_check=true。",
                tool_name=tool_name,
            )
        default_states = {
            "damage": "已损坏",
            "repair": "已修复",
            "disable": "已停用",
        }
        if not continue_with_check and not state_note:
            state_note = default_states.get(operation, "")
        public_result = self._clean_multiline(arguments.get("public_result"))
        public_fact = self._clean_multiline(arguments.get("public_fact"))
        transaction_snapshot = CampaignStateTransaction.capture(
            app,
            context.campaign_id,
        )
        try:
            with runtime.transaction_lock:
                focus_mode = "current"
                current_scene = app.scene_manager.current_scene
                if current_scene is None or actor not in current_scene.participants:
                    focused, focus_mode = app.scene_manager.focus_actor_branch(
                        actor,
                        name=f"{scene.location or scene.name}·{actor}",
                        location=scene.location,
                    )
                    scene = focused
                else:
                    scene = current_scene
                frame = app.scene_frame_manager.current_frame
                if frame is None:
                    frame = self._ensure_frame(runtime, context)
                if scene is None or actor not in scene.participants:
                    raise RuntimeError("未能聚焦角色所在的活动场景分支。")
                if fixture is None:
                    fixture = app.scene_manager.ensure_scene_fixture(
                        name=fixture_name,
                        description=self._clean_multiline(arguments.get("description")),
                        allowed_operations=sorted(allowed_operations),
                        source_authority=authority_ref,
                        fixture_id=fixture_id,
                    )
                if fixture.allowed_operations and operation not in fixture.allowed_operations:
                    raise ValueError(
                        f"固定物件【{fixture.name}】不支持操作【{operation}】。"
                    )
                before_state = fixture.current_state
                if not continue_with_check and state_note:
                    fixture.current_state = state_note
                fixture.history.append(
                    {
                        "operation": operation,
                        "actor": actor,
                        "from_state": before_state,
                        "to_state": fixture.current_state,
                        "evidence": self._clean_multiline(arguments.get("evidence")),
                        "public_fact": public_fact,
                    }
                )
                app.scene_manager.record_participant_activity(
                    actor,
                    f"{operation}固定场景物件【{fixture.name}】",
                )
                if public_fact:
                    app.scene_frame_manager.record_public_fact(public_fact)
                if public_result:
                    app.scene_frame_manager.record_gm_beat(public_result)
                action_round = (
                    {}
                    if continue_with_check
                    else app.record_free_scene_player_action(actor)
                )
                clock_lines = app.turn_response_renderer.public_state_lines(
                    action_round,
                    existing_lines=[public_result],
                )
                reply = "\n".join(
                    item
                    for item in [public_result, *clock_lines]
                    if str(item or "").strip()
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, transaction_snapshot)
            return self._failure(
                "SCENE_FIXTURE_COMMIT_FAILED",
                str(exc) or "固定场景物件动作未能提交。",
                "读取当前场景固定物件状态后修正参数；不要改用可携带剧情物件工具。",
                tool_name=tool_name,
            )

        followup = (
            {
                "allowed_followup_tools": ["declare_check_action"],
                "required_followup_tools": ["declare_check_action"],
            }
            if continue_with_check
            else {}
        )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "scene_id": scene.scene_id,
                "fixture": {
                    "fixture_id": fixture.fixture_id,
                    "name": fixture.name,
                    "current_state": fixture.current_state,
                },
                "action_round": dict(action_round),
                "focus_mode": focus_mode,
                "silent_commit_allowed": not bool(reply),
                "source_message_already_public": not bool(reply),
                "saved_path": saved_path,
                **followup,
            },
            state_changed=True,
            public_fallback_reply=reply,
            lock_public_reply=bool(reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    consequence=(public_fact if public_result else ""),
                    public_image=(
                        self._first_sentence(public_result)
                        if public_result
                        else ""
                    ),
                )
            ],
        )

    def commit_scene_response(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"))
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.scene_manager.current_scene is None and app.scene_frame_manager.current_frame is None:
            return self._failure(
                "NO_ACTIVE_SCENE",
                "当前没有可提交事实的场景。",
                "先通过场景生命周期工具建立场景，不要把场景回复写入世界空白处。",
            )

        system_beat = bool(context.metadata.get("system_gm_beat_request"))
        authored_scene_opening = bool(
            system_beat
            and context.metadata.get("gm_authored_scene_opening") is True
            and str(context.metadata.get("heartbeat_action") or "").strip()
            == "scene_opening"
        )
        authored_free_scene_beat = bool(
            system_beat
            and context.metadata.get("gm_authored_free_scene_beat") is True
            and context.metadata.get("heartbeat_require_material_change") is True
            and str(context.metadata.get("heartbeat_action") or "").strip()
            == "free_scene_beat"
        )
        trusted_followup = SceneChangeAuthorityPolicy.trusted_required_followup(
            context,
            "commit_scene_response",
        )
        if not system_beat and not trusted_followup:
            return self._failure(
                "SCENE_RESPONSE_FOLLOWUP_REQUIRED",
                "当前事务没有已完成规则工具留下的场景回应义务。",
                "先由对应的行动、规则或场景工具提交结果；其成功回执会在本事务内开放这项公开收尾能力。",
            )

        authority_review = (
            SceneChangeAuthorityPolicy.resolve_required_followup(context)
            if trusted_followup
            else None
        )
        if authority_review is not None and not authority_review.valid:
            return self._failure(
                authority_review.error_code,
                authority_review.message,
                authority_review.correction_hint,
            )
        if (
            system_beat
            and authority_review is None
            and not authored_scene_opening
            and not authored_free_scene_beat
        ):
            authority_review = SceneChangeAuthorityPolicy.resolve_system_beat(
                app=app,
                context=context,
                value=arguments.get("change_authority"),
            )
            if not authority_review.valid:
                proposed_reply = self._clean_multiline(arguments.get("public_reply"))
                if (
                    context.metadata.get("heartbeat_require_material_change")
                    and proposed_reply
                    and SceneMomentPolicy.only_restates_packet(
                        proposed_reply,
                        self.state_summary(context),
                    )
                ):
                    return self._failure(
                        "NO_NEW_MATERIAL_CHANGE",
                        "这段主动节拍只是在换一种说法重复最近已经送达的局面或后果。",
                        "保持静默，或依据明确的新触发提交对象、前态和后态都不同的真实变化。",
                    )
                return self._failure(
                    authority_review.error_code,
                    authority_review.message,
                    authority_review.correction_hint,
                )

        raw_facts = arguments.get("public_facts")
        if authority_review is None and raw_facts is not None and not isinstance(raw_facts, list):
            return self._failure(
                "PUBLIC_FACTS_MUST_BE_ARRAY",
                "public_facts必须是数组。",
                "没有持久事实时提交空数组。",
            )
        submitted_facts = (
            [
                self._clean_multiline(item)
                for item in raw_facts[:8]
                if self._clean_multiline(item)
            ]
            if isinstance(raw_facts, list)
            else []
        )
        requested_facts = (
            list(authority_review.public_facts)
            if authority_review is not None
            else list(
                dict.fromkeys(
                    submitted_facts
                )
            )
        )
        public_reply = (
            authority_review.public_reply
            if authority_review is not None
            else self._clean_multiline(arguments.get("public_reply"))
        )
        semantic_agency_reviewed = current_semantic_tool_call_attested(
            context,
            "commit_scene_response",
        )
        if (
            authored_scene_opening or authored_free_scene_beat
        ) and not semantic_agency_reviewed:
            agency_error = next(
                (
                    error
                    for candidate in [public_reply, *requested_facts]
                    if candidate
                    for error in [SceneMomentPolicy.player_agency_violation(candidate)]
                    if error
                ),
                "",
            )
            if agency_error:
                return self._failure(
                    "AUTHORED_BEAT_PERFORMS_PLAYER_ACTION",
                    agency_error,
                    "把public_reply与public_facts改为环境、物件或合法NPC作主语的客观新变化；"
                    "删除对玩家上一轮动作的重演，以及替玩家注意、低头查看、判断或操作的句子。",
                )
        creative_metadata: dict[str, object] = {}
        grounded_public_facts: set[str] = set()
        awaits_player_response = bool(arguments.get("awaits_player_response"))
        creative_writer = getattr(app, "scene_creative_writer", None)
        direct_authored_opening = bool(
            authored_scene_opening
            and public_reply
            and all(fact in public_reply for fact in requested_facts)
        )
        if direct_authored_opening:
            creative_metadata = {
                "author": "core_gm",
                "operation": "scene_response",
                "used_model": False,
                "scene_writer_bypassed": True,
            }
        if (
            authority_review is None
            and not direct_authored_opening
            and creative_writer is not None
            and creative_writer.available
        ):
            scene_state = self.state_summary(context)
            try:
                composition = creative_writer.compose_public_scene_text(
                    operation="scene_response",
                    facts={
                        "public_facts": requested_facts,
                        "creative_direction": self._clean_multiline(
                            arguments.get("creative_direction")
                        ),
                        "scene": {
                            "name": scene_state.get("name", ""),
                            "location": scene_state.get("location", ""),
                            "participants": scene_state.get("participants", []),
                            "objective": scene_state.get("objective", ""),
                            "current_pressure": scene_state.get(
                                "current_pressure", ""
                            ),
                            "visible_elements": scene_state.get(
                                "visible_elements", []
                            ),
                            "private_situation": scene_state.get(
                                "private_situation", {}
                            ),
                        },
                        "current_player_message": str(
                            context.metadata.get("current_message") or ""
                        ).strip(),
                        "system_gm_beat": bool(
                            context.metadata.get("system_gm_beat_request")
                        ),
                    },
                    recent_public_messages=self._recent_public_messages(context),
                    fallback_public_reply=public_reply,
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "SCENE_CREATIVE_AUTHOR_FAILED",
                    f"DeepSeek场景作者未能完成环境回应：{exc}",
                    "不要由核心GM补写成品；保持当前状态并稍后重试。",
                )
            public_reply = composition.public_reply
            awaits_player_response = composition.awaits_player_response
            grounded_public_facts.update(composition.grounded_public_facts)
            creative_metadata = {
                "author": "scene_creative_writer",
                "model": composition.model,
                "used_model": composition.used_model,
                "operation": "scene_response",
            }
        if (
            (authored_scene_opening or authored_free_scene_beat)
            and creative_writer is not None
            and creative_writer.available
        ):
            validate_player_agency = getattr(
                creative_writer,
                "validate_player_agency",
                None,
            )
            if not callable(validate_player_agency):
                return self._failure(
                    "FINAL_SCENE_AGENCY_AUDIT_UNAVAILABLE",
                    "最终场景文本没有可用的玩家自主权语义审计器。",
                    "不要提交或发布这段文本；恢复最终文本语义审计后重新生成主动节拍。",
                )
            player_characters = [
                character.name
                for character in app.character_manager.all()
                if "pc" in character.traits
            ]
            participant_names = list(
                getattr(app.scene_manager.current_scene, "participants", []) or []
            )
            npc_characters = list(
                dict.fromkeys(
                    [
                        character.name
                        for character in app.character_manager.all()
                        if "pc" not in character.traits
                    ]
                    + [
                        str(name or "").strip()
                        for name in participant_names
                        if str(name or "").strip()
                        and str(name or "").strip() not in player_characters
                    ]
                )
            )
            try:
                validate_player_agency(
                    public_text=public_reply,
                    player_characters=player_characters,
                    npc_characters=npc_characters,
                    deadline=context.agent_deadline_monotonic,
                )
            except SceneCreativeWriterError as exc:
                return self._failure(
                    "FINAL_SCENE_AGENCY_AUDIT_FAILED",
                    f"最终场景文本未通过玩家自主权语义审计：{exc}",
                    "只描述环境、物件或合法NPC的新变化；玩家角色接下来如何查看、移动、取物、说话或判断必须留给玩家。",
                )
        if not public_reply:
            return self._failure(
                "PUBLIC_REPLY_REQUIRED",
                "公开回复不能为空。",
                "提交已授权事实或表达方向，由场景作者生成公开回复。",
            )
        if any(marker in public_reply for marker in self._PRIVATE_MARKERS):
            return self._failure(
                "PRIVATE_CONTEXT_LEAK",
                "公开回复包含明确的后台控制字段。",
                "保留自然叙事，删除后台字段名后重新提交。",
            )
        facts, error = self._validated_public_facts(requested_facts, public_reply)
        discarded_public_facts: list[str] = (
            [
                item
                for item in submitted_facts
                if item not in requested_facts
            ]
            if authority_review is not None
            else []
        )
        if error is not None:
            if error.error_code != "FACT_NOT_PUBLICLY_SPOKEN":
                return error
            if requested_facts and all(
                fact in grounded_public_facts for fact in requested_facts
            ):
                facts = list(requested_facts)
                error = None
            elif creative_metadata:
                error.tool_name = "commit_scene_response"
                error.error_code = "CREATIVE_PUBLIC_FACT_MISSING"
                error.message = "DeepSeek场景作者漏掉了核心GM提交的公开事实。"
                error.correction_hint = "当前状态未写入；保留事实含义并重试创作作者。"
                return error
            else:
                exact_facts: list[str] = []
                for candidate in requested_facts:
                    if candidate in public_reply:
                        exact_facts.append(candidate)
                    else:
                        discarded_public_facts.append(candidate)
                facts = exact_facts

        if (
            system_beat
            and context.metadata.get("heartbeat_require_material_change")
            and authority_review is None
            and SceneMomentPolicy.only_restates_packet(
                public_reply,
                self.state_summary(context),
            )
        ):
            return self._failure(
                "NO_NEW_MATERIAL_CHANGE",
                "这段主动节拍只是在换一种说法重复最近已经送达的局面或后果。",
                "保持静默，或依据明确的新触发提交对象、前态和后态都不同的真实变化。",
            )

        change_authority = (
            dict(authority_review.authority)
            if authority_review is not None
            and authority_review.authority.get("kind")
            in SceneChangeAuthorityPolicy.SYSTEM_BEAT_KINDS
            else {}
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
                "change_authority": change_authority,
                "creative_author": creative_metadata,
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
                    scene_resolved=bool(
                        context.metadata.get("heartbeat_require_local_resolution")
                    ),
                    session_question_resolved=bool(
                        context.metadata.get("heartbeat_require_session_resolution")
                    ),
                    awaits_player_response=awaits_player_response,
                    closure_payoff=bool(
                        context.metadata.get("heartbeat_require_session_resolution")
                    ),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or heartbeat_action
                        ).strip()
                        if system_beat
                        else ""
                    ),
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
    def _recent_public_messages(
        context: GMToolExecutionContext,
    ) -> list[dict[str, object]]:
        raw = context.metadata.get("recent_messages")
        if isinstance(raw, list):
            return [
                dict(item)
                for item in raw[-8:]
                if isinstance(item, dict)
                and str(item.get("content") or item.get("text") or "").strip()
            ]
        recent = str(context.metadata.get("recent_public_context") or "").strip()
        return [{"role": "table", "content": recent}] if recent else []

    @staticmethod
    def _failure(
        code: str,
        message: str,
        hint: str,
        *,
        tool_name: str = "commit_scene_response",
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            public_fallback_reply="这一步还没有写入场景状态。",
        )
