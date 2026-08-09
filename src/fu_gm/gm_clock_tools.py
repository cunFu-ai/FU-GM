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
from fu_gm.models import Clock


class ClockToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMClockToolService:
    """Validated lifecycle commands for clocks chosen semantically by the GM."""

    _CLOCK_TYPES = ("objective", "threat", "villain", "dungeon", "boss")
    _PRESSURE_TYPES = {"threat", "villain", "dungeon", "boss"}
    _PUBLIC_FORBIDDEN = ("威胁命刻", "目标命刻", "仪式命刻", "赌注：", "自动推进：")
    _CHANGE_CAUSES = (
        "direct_action_success",
        "direct_action_failure",
        "rule_failure_consequence",
        "gm_fictional_consequence",
        "skill_effect",
        "manual_correction",
    )

    def __init__(self, host: ClockToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_clocks",
                description="读取当前活动命刻、作用域、自动推进节奏和后台后果，不修改状态。",
                handler=self.get_clocks,
            )
        )
        registry.register(
            GMToolDefinition(
                name="create_clock",
                description=(
                    "GM根据当前局面主动建立命刻。前台命刻必须原样公开进度；自动命刻只能按完整行动轮推进，"
                    "不能按聊天消息或单个角色行动推进。仪式必须使用perform_ritual_project_action，"
                    "不能用通用命刻工具代替。"
                ),
                handler=self.create_clock,
                parameters=(
                    GMToolParameter("name", "string", "简短且具象的命刻名称。", required=True),
                    GMToolParameter("segments", "integer", "格数。", required=True),
                    GMToolParameter("clock_type", "string", "命刻用途。", required=True, enum=self._CLOCK_TYPES),
                    GMToolParameter("scope", "string", "持续范围。", required=True, enum=("scene", "session", "campaign")),
                    GMToolParameter("stakes", "string", "后台记录：命刻填满意味着什么。", required=True),
                    GMToolParameter("completion_consequence", "string", "后台记录：填满后实际发生的后果。", required=True),
                    GMToolParameter("auto_advance", "boolean", "是否在完整行动轮结束时自动推进。", required=True),
                    GMToolParameter("auto_advance_every", "integer", "每多少个完整行动轮推进一次。"),
                    GMToolParameter(
                        "advance_on_rest",
                        "boolean",
                        "只有跨场景的压力确实会随英雄休息而推进时设为true。",
                    ),
                    GMToolParameter("visibility", "string", "foreground会公开，background仅供GM。", required=True, enum=("foreground", "background")),
                    GMToolParameter("public_reply", "string", "前台命刻创建时原样发给玩家的回复。"),
                    GMToolParameter("evidence", "string", "当前消息中触发GM判断的逐字证据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="change_clock",
                description=(
                    "因直接行动结果、明确失败代价、虚构后果、技能或人工修正改变一个现有命刻。"
                    "单纯观察客观威胁不会改变威胁进度；行动轮自动推进由系统事件负责，不能调用本工具冒充。"
                ),
                handler=self.change_clock,
                parameters=(
                    GMToolParameter("name", "string", "现有命刻名称。", required=True),
                    GMToolParameter("delta", "integer", "填充为正、擦除为负，范围-3到3。", required=True),
                    GMToolParameter("cause", "string", "本次变化的规则或虚构来源。", required=True, enum=self._CHANGE_CAUSES),
                    GMToolParameter("reason", "string", "后台简述命刻为何改变。", required=True),
                    GMToolParameter("public_reply", "string", "前台命刻变化时原样发给玩家的回复。"),
                    GMToolParameter("completion_facts", "array", "前台目标或压力命刻填满时，回复中已经公开并兑现的结果事实。"),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="close_clock",
                description="在虚构局面已经解决、目标作废或后果已经兑现时结束命刻。",
                handler=self.close_clock,
                parameters=(
                    GMToolParameter("name", "string", "现有命刻名称。", required=True),
                    GMToolParameter("mode", "string", "resolved为解决，abandoned为作废。", required=True, enum=("resolved", "abandoned")),
                    GMToolParameter("reason", "string", "后台结案原因。", required=True),
                    GMToolParameter("public_reply", "string", "前台命刻结案时原样发给玩家的回复。"),
                    GMToolParameter("public_facts", "array", "回复中已公开且需要持续记住的结案事实。"),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        budget = app.campaign_pacing_manager.pressure_budget(
            conflict_active=bool(app.conflict_manager.state.active),
            boss_scene=bool(getattr(app, "_is_boss_pressure_scene", lambda: False)()),
        )
        return {
            "active": [self._clock_payload(clock, app.clock_manager) for clock in app.clock_manager.all()],
            "pacing_budget": {
                "max_foreground_pressure_clocks": budget.max_foreground_pressure_clocks,
                "max_auto_advance_clocks": budget.max_auto_advance_clocks,
                "max_public_clock_lines": budget.max_public_clock_lines,
                "allow_multi_threat_pressure": budget.allow_multi_threat_pressure,
            },
        }

    def get_clocks(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(tool_name="get_clocks", ok=True, result=self.state_summary(context))

    def create_clock(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "create_clock")
        if evidence_error is not None:
            return evidence_error
        name = self._clean(arguments.get("name"))
        segments = int(arguments.get("segments") or 0)
        clock_type = self._clean(arguments.get("clock_type"))
        scope = self._clean(arguments.get("scope"))
        stakes = self._clean(arguments.get("stakes"))
        consequence = self._clean(arguments.get("completion_consequence"))
        visibility = self._clean(arguments.get("visibility"))
        auto_advance = bool(arguments.get("auto_advance"))
        auto_every = int(arguments.get("auto_advance_every") or 1)
        advance_on_rest = bool(arguments.get("advance_on_rest"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        if not name:
            return self._failure("create_clock", "CLOCK_NAME_REQUIRED", "命刻必须有名称。", "提供简短、具象且不会与现有命刻混淆的名称。")
        if segments not in {4, 6, 8, 10, 12}:
            return self._failure("create_clock", "INVALID_CLOCK_SIZE", "命刻格数必须是4、6、8、10或12。", "根据任务复杂度或威胁紧迫度重新选择。")
        if not stakes or not consequence:
            return self._failure("create_clock", "CLOCK_CONSEQUENCE_REQUIRED", "命刻必须有明确的填满含义和后果。", "补充后台stakes与completion_consequence，但不要把字段标签发给玩家。")
        if clock_type == "ritual" or name.startswith("仪式："):
            return self._failure(
                "create_clock",
                "RITUAL_REQUIRES_RITUAL_TOOL",
                "仪式不能用通用命刻工具建立。",
                "使用perform_ritual_project_action提交PlanRitual；规则层会建立正确格数、施法者和最终施法事务。",
            )
        if auto_every < 1 or auto_every > 12:
            return self._failure("create_clock", "INVALID_AUTO_CADENCE", "自动推进间隔必须在1到12个行动轮之间。", "使用完整行动轮作为时间单位。")

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        manager = app.clock_manager
        if manager.exists(name):
            return self._failure("create_clock", "CLOCK_ALREADY_EXISTS", f"命刻【{name}】已经存在。", "读取现有命刻后选择推进、关闭或另取不混淆的名称。")
        if manager.is_retired(name):
            return self._failure("create_clock", "CLOCK_NAME_RETIRED", f"命刻【{name}】已经结案。", "不要让已兑现的命刻从零复活；若局面确实不同，请使用新名称。")
        if scope == "scene" and app.scene_manager.current_scene is None:
            return self._failure("create_clock", "SCENE_REQUIRED", "场景命刻需要一个当前场景。", "先建立场景，或将真正跨场景的命刻设为session/campaign。")
        if advance_on_rest and (
            clock_type not in self._PRESSURE_TYPES
            or scope not in {"session", "campaign"}
        ):
            return self._failure(
                "create_clock",
                "REST_ADVANCE_REQUIRES_LONG_PRESSURE_CLOCK",
                "只有session/campaign范围的压力命刻才能随休息推进。",
                "关闭advance_on_rest，或把真正跨场景持续发展的威胁设为session/campaign压力命刻。",
            )

        budget_error = self._validate_pressure_budget(app, clock_type, visibility, auto_advance)
        if budget_error is not None:
            return budget_error
        marker = f"【{name}】0/{segments}"
        if visibility == "foreground":
            reply_error = self._validate_public_reply("create_clock", public_reply, marker)
            if reply_error is not None:
                return reply_error

        scene = app.scene_manager.current_scene
        clock = Clock(
            name=name,
            max_segments=segments,
            current=0,
            clock_type=clock_type,
            stakes=stakes,
            auto_advance="每个完整行动轮结束时推进1格" if auto_advance else "",
            visibility=visibility,
            auto_advance_every=auto_every,
            advance_on_rest=advance_on_rest,
            scope=scope,
            scene_id=str(getattr(scene, "scene_id", "") or "") if scope == "scene" else "",
            owner="GM",
            source=self._clean(arguments.get("evidence")),
            completion_consequence=consequence,
        )
        with runtime.transaction_lock:
            if public_reply:
                self._ensure_scene_frame(
                    app,
                    context,
                    recent_chat=public_reply,
                )
            manager.add(clock)
            if public_reply:
                self._record_public_beat(app, public_reply)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        fallback = public_reply or marker
        system_beat = bool(context.metadata.get("system_gm_beat_request"))
        pressure_created = clock_type in self._PRESSURE_TYPES
        return GMToolReceipt(
            tool_name="create_clock",
            ok=True,
            result={"clock": self._clock_payload(clock, manager), "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=fallback,
            lock_public_reply=bool(public_reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not system_beat,
                    action_summary=(
                        ""
                        if system_beat
                        else str(context.metadata.get("current_message") or "").strip()
                    ),
                    consequence=f"命刻【{name}】进入当前局面。",
                    opposition_move=(
                        self._first_sentence(fallback) if pressure_created else ""
                    ),
                    public_image=self._first_sentence(fallback),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if system_beat
                        else ""
                    ),
                )
            ],
        )

    def change_clock(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "change_clock")
        if evidence_error is not None:
            return evidence_error
        name = self._clean(arguments.get("name"))
        delta = int(arguments.get("delta") or 0)
        cause = self._clean(arguments.get("cause"))
        reason = self._clean(arguments.get("reason"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        if delta == 0 or delta < -3 or delta > 3:
            return self._failure("change_clock", "INVALID_CLOCK_DELTA", "命刻变化必须是-3到3之间的非零整数。", "按检定结果、机会或具体技能重新计算变化。")
        if not reason:
            return self._failure("change_clock", "CLOCK_CHANGE_REASON_REQUIRED", "命刻变化需要后台原因。", "说明行动如何直接改变该命刻，不能用单纯观察客观威胁作为原因。")

        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        manager = app.clock_manager
        if not manager.exists(name):
            return self._failure("change_clock", "CLOCK_NOT_FOUND", f"没有找到命刻【{name}】。", "先调用get_clocks并使用当前活动命刻的名称。")
        clock = manager.get(name)
        if clock.clock_type == "ritual" or clock.name.startswith("仪式："):
            return self._failure(
                "change_clock",
                "RITUAL_REQUIRES_RITUAL_TOOL",
                f"命刻【{clock.name}】属于仪式事务，不能直接修改。",
                "使用perform_ritual_project_action提交ContributeRitual或CastRitual，以保留检定、施法者和精神值规则。",
            )
        before = int(clock.current)
        after = max(0, min(int(clock.max_segments), before + delta))
        actual_delta = after - before
        if actual_delta == 0:
            return self._failure("change_clock", "CLOCK_CANNOT_CHANGE", f"命刻【{clock.name}】已经无法按这个方向变化。", "不要声称进度发生改变；选择其他行动或关闭已结束命刻。")
        marker = f"【{clock.name}】{after}/{clock.max_segments}"
        if str(clock.visibility or "foreground") == "foreground":
            reply_error = self._validate_public_reply("change_clock", public_reply, marker)
            if reply_error is not None:
                return reply_error

        completion_facts, facts_error = self._validated_facts(
            arguments.get("completion_facts") or [],
            public_reply,
            tool_name="change_clock",
        )
        if facts_error is not None:
            return facts_error
        fills_pressure = after >= clock.max_segments and clock.clock_type in self._PRESSURE_TYPES
        completes_objective = (
            after >= clock.max_segments and clock.clock_type == "objective"
        )
        foreground_completion = (
            str(clock.visibility or "foreground").strip().lower()
            not in {"background", "hidden", "dormant", "后台"}
            and (fills_pressure or completes_objective)
        )
        if foreground_completion and not completion_facts:
            return self._failure(
                "change_clock",
                "CLOCK_COMPLETION_FACT_REQUIRED",
                f"命刻【{clock.name}】将被填满，但没有提交已经发生的公开结果。",
                "在public_reply中说明目标达成或威胁兑现，并把对应原句放入completion_facts；不能只显示进度。",
            )

        with runtime.transaction_lock:
            if public_reply or completion_facts:
                self._ensure_scene_frame(
                    app,
                    context,
                    recent_chat=public_reply,
                )
            manager.advance(clock.name, delta)
            for fact in completion_facts:
                self._record_public_fact(app, fact)
            if fills_pressure:
                manager.resolve(
                    clock.name,
                    note=clock.completion_consequence or clock.stakes or reason,
                    archive=True,
                )
            elif completes_objective:
                manager.resolve(
                    clock.name,
                    note=clock.completion_consequence or clock.stakes or reason,
                    archive=True,
                )
            if public_reply:
                self._record_public_beat(app, public_reply)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        direct_player_change = cause in {
            "direct_action_success",
            "direct_action_failure",
            "skill_effect",
        }
        completed = after >= clock.max_segments
        completion_summary = "；".join(completion_facts)
        if completed and not completion_summary:
            completion_summary = clock.completion_consequence or reason
        return GMToolReceipt(
            tool_name="change_clock",
            ok=True,
            result={
                "name": clock.name,
                "before": before,
                "after": after,
                "delta": actual_delta,
                "cause": cause,
                "reason": reason,
                "status": (
                    "resolved"
                    if fills_pressure or completes_objective
                    else clock.status
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply or marker,
            lock_public_reply=bool(public_reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=direct_player_change,
                    action_summary=(
                        str(context.metadata.get("current_message") or "").strip()
                        if direct_player_change
                        else ""
                    ),
                    consequence=completion_summary if completed else "",
                    local_payoff=(
                        completion_summary
                        if completed and clock.clock_type not in self._PRESSURE_TYPES
                        else ""
                    ),
                    climax=(
                        completion_summary
                        if completed
                        and clock.scope in {"session", "campaign"}
                        and clock.clock_type in {"boss", "villain"}
                        else ""
                    ),
                    opposition_move=(
                        completion_summary
                        if completed and clock.clock_type in self._PRESSURE_TYPES
                        else ""
                    ),
                    public_image=self._first_sentence(public_reply),
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def close_clock(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "close_clock")
        if evidence_error is not None:
            return evidence_error
        name = self._clean(arguments.get("name"))
        mode = self._clean(arguments.get("mode"))
        reason = self._clean(arguments.get("reason"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        if not reason:
            return self._failure("close_clock", "CLOCK_CLOSE_REASON_REQUIRED", "结束命刻需要明确原因。", "说明局面如何解决、被挫败或失去意义。")
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        manager = app.clock_manager
        if not manager.exists(name):
            return self._failure("close_clock", "CLOCK_NOT_FOUND", f"没有找到命刻【{name}】。", "先调用get_clocks确认活动命刻。")
        clock = manager.get(name)
        if clock.clock_type == "ritual" or clock.name.startswith("仪式："):
            return self._failure(
                "close_clock",
                "RITUAL_REQUIRES_RITUAL_TOOL",
                f"命刻【{clock.name}】属于仪式事务，不能直接结案。",
                "使用perform_ritual_project_action完成最终施法；场景结束会中断未完成仪式。",
            )
        marker = f"【{clock.name}】{clock.current}/{clock.max_segments}"
        if str(clock.visibility or "foreground") == "foreground":
            reply_error = self._validate_public_reply("close_clock", public_reply, marker)
            if reply_error is not None:
                return reply_error
        facts, facts_error = self._validated_facts(
            arguments.get("public_facts") or [],
            public_reply,
            tool_name="close_clock",
        )
        if facts_error is not None:
            return facts_error
        with runtime.transaction_lock:
            if public_reply or facts:
                self._ensure_scene_frame(
                    app,
                    context,
                    recent_chat=public_reply,
                )
            if mode == "resolved":
                manager.resolve(clock.name, note=reason, archive=True)
            else:
                manager.abandon(clock.name, note=reason)
            for fact in facts:
                self._record_public_fact(app, fact)
            if public_reply:
                self._record_public_beat(app, public_reply)
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="close_clock",
            ok=True,
            result={"name": clock.name, "status": mode, "reason": reason, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=public_reply or marker,
            lock_public_reply=bool(public_reply),
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    consequence=reason,
                    local_payoff=reason if mode == "resolved" else "",
                    public_image=self._first_sentence(public_reply),
                    local_question_resolved=mode == "resolved",
                    gm_beat_purpose=(
                        str(
                            context.metadata.get("heartbeat_beat_purpose")
                            or context.metadata.get("heartbeat_action")
                            or ""
                        ).strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def _validate_pressure_budget(
        self,
        app: Any,
        clock_type: str,
        visibility: str,
        auto_advance: bool,
    ) -> GMToolReceipt | None:
        if clock_type not in self._PRESSURE_TYPES:
            return None
        budget = app.campaign_pacing_manager.pressure_budget(
            conflict_active=bool(app.conflict_manager.state.active),
            boss_scene=bool(getattr(app, "_is_boss_pressure_scene", lambda: False)()),
        )
        active = [
            item
            for item in app.clock_manager.all()
            if item.clock_type in self._PRESSURE_TYPES and item.current < item.max_segments
        ]
        foreground_count = sum(
            str(item.visibility or "foreground") == "foreground" for item in active
        )
        auto_count = sum(bool(item.auto_advance) for item in active)
        if visibility == "foreground" and foreground_count >= budget.max_foreground_pressure_clocks:
            return self._failure(
                "create_clock",
                "FOREGROUND_PRESSURE_BUDGET_EXCEEDED",
                "当前阶段的前台压力命刻已经达到上限。",
                "不要同时轰炸玩家；改为background、关闭旧压力，或等到危机/首领阶段。",
            )
        if auto_advance and auto_count >= budget.max_auto_advance_clocks:
            return self._failure(
                "create_clock",
                "AUTO_PRESSURE_BUDGET_EXCEEDED",
                "当前阶段的自动压力命刻已经达到上限。",
                "改为不自动推进、关闭旧压力，或把新威胁留在后台。",
            )
        return None

    @classmethod
    def _validate_public_reply(
        cls,
        tool_name: str,
        public_reply: str,
        marker: str,
    ) -> GMToolReceipt | None:
        if not public_reply:
            return cls._failure(tool_name, "PUBLIC_CLOCK_REPLY_REQUIRED", "前台命刻变化必须向玩家公开。", f"在自然叙事中原样包含进度「{marker}」。")
        if marker not in public_reply:
            return cls._failure(tool_name, "CLOCK_PROGRESS_NOT_PUBLIC", f"公开回复没有原样包含「{marker}」。", "加入简洁进度，不输出命刻类型、后台赌注或自动推进字段。")
        if any(token in public_reply for token in cls._PUBLIC_FORBIDDEN):
            return cls._failure(tool_name, "CLOCK_BACKSTAGE_FIELD_LEAK", "公开回复包含命刻后台字段或类型标签。", "只显示【名称】当前/总格数，并用自然叙事表现压力。")
        return None

    @classmethod
    def _validated_facts(
        cls,
        value: object,
        public_reply: str,
        *,
        tool_name: str,
    ) -> tuple[list[str], GMToolReceipt | None]:
        if not isinstance(value, list):
            return [], cls._failure(tool_name, "PUBLIC_FACTS_MUST_BE_ARRAY", "公开事实必须是数组。", "没有持久事实时提交空数组。")
        facts: list[str] = []
        for item in value[:8]:
            fact = cls._clean_multiline(item)
            if not fact:
                continue
            if fact not in public_reply:
                return [], cls._failure(tool_name, "FACT_NOT_PUBLICLY_SPOKEN", f"事实「{fact[:80]}」没有逐字出现在公开回复中。", "只能写入玩家实际看见的原句。")
            if fact not in facts:
                facts.append(fact)
        return facts, None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(tool_name, "EVIDENCE_NOT_IN_CURRENT_MESSAGE", "evidence不是当前消息中的逐字连续片段。", "从current_message逐字复制依据，不使用摘要。")
        return None

    @staticmethod
    def _clock_payload(clock: Clock, manager: Any) -> dict[str, object]:
        return {
            "name": clock.name,
            "current": clock.current,
            "max_segments": clock.max_segments,
            "clock_type": clock.clock_type,
            "scope": clock.scope,
            "scene_id": clock.scene_id,
            "visibility": clock.visibility,
            "auto_advance": clock.auto_advance,
            "auto_advance_every": clock.auto_advance_every,
            "advance_on_rest": bool(clock.advance_on_rest),
            "status": clock.status,
            "stakes": clock.stakes,
            "completion_consequence": clock.completion_consequence,
            "public": manager.format_clock(clock, public=True, include_hint=False),
        }

    @staticmethod
    def _record_public_fact(app: Any, fact: str) -> None:
        if app.scene_frame_manager.current_frame is not None:
            app.scene_frame_manager.record_public_fact(fact)

    @staticmethod
    def _record_public_beat(app: Any, reply: str) -> None:
        if app.scene_frame_manager.current_frame is not None:
            app.scene_frame_manager.record_gm_beat(reply)

    @staticmethod
    def _ensure_scene_frame(
        app: Any,
        context: GMToolExecutionContext,
        *,
        recent_chat: str,
    ) -> Any | None:
        scene = app.scene_manager.current_scene
        if scene is None:
            return None
        plan = getattr(app.story_arc_manager.state, "current_pacing_plan", None)
        return app.scene_frame_manager.ensure_frame(
            scene=scene,
            recent_chat=(
                recent_chat
                or str(context.metadata.get("recent_public_context") or "")
                or str(context.metadata.get("current_message") or "")
            ),
            world_state=app.world_state,
            character_manager=app.character_manager,
            contract=getattr(plan, "dramatic_contract", None),
        )

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

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
    def _failure(tool_name: str, code: str, message: str, hint: str) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            public_fallback_reply="这一步还没有改变命刻。",
        )
