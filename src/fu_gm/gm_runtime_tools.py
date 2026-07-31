from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import (
    CampaignStateSnapshot,
    CampaignStateTransaction,
)
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolPacingEvent,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.gm_decision_followups import (
    add_gm_fumble_followups,
    required_followup_mode,
)
from fu_gm.models import Action, ActionType, GamePanel, SceneType
from fu_gm.session_gate import SessionGateSignal


class RuntimeToolHost(Protocol):
    session_gates: Any

    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    def _player_character_control_map(self, runtime: Any) -> dict[str, list[str]]: ...

    def _handle_gate_signal(
        self,
        payload: dict[str, Any],
        *,
        gate: Any,
        signal: SessionGateSignal,
    ) -> dict[str, Any]: ...

    def _end_session(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class GMRuntimeToolService:
    """Typed host controls for sessions, scenes and conflict initiative.

    The language model chooses *when* these capabilities are appropriate. This
    service owns only structural validation and atomic state transitions; it
    deliberately performs no keyword or prose-intent recognition.
    """

    _GENERIC_SCENE_TYPES = {
        SceneType.STANDARD,
        SceneType.INTERLUDE,
        SceneType.GM,
    }
    _FRAME_SCALARS = {
        "premise",
        "stakes",
        "current_pressure",
        "dramatic_question",
        "signature_image",
        "opposition_goal",
        "dilemma",
        "reversal",
        "climax_type",
        "closure_requirement",
        "irreversible_change",
        "ending_echo",
    }
    _FRAME_LISTS = {
        "fantastic_details",
        "escalation_ladder",
        "possible_payoffs",
        "visible_elements",
        "npc_functions",
        "clue_pool",
        "secrets",
        "possible_reveals",
        "telegraphed_threats",
        "danger_candidates",
        "discovery_candidates",
        "special_mechanism_candidates",
        "story_outline",
    }
    _HIDDEN_FRAME_FIELDS = {
        "reversal",
        "secrets",
        "possible_reveals",
        "story_outline",
    }

    def __init__(self, host: RuntimeToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_runtime_state",
                description="读取当前会话门控、场景、冲突和当前行动者；不修改状态。",
                handler=self.get_runtime_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_session",
                description=(
                    "根据玩家明确同意开启开团前共识、第零章或冒险会话。"
                    "进入冒险会由规则层检查第零章与角色卡是否完成。"
                ),
                handler=self.start_session,
                parameters=(
                    GMToolParameter(
                        "phase",
                        "string",
                        "要进入的会话阶段。",
                        required=True,
                        enum=("pre_session", "session_zero", "adventure"),
                    ),
                    GMToolParameter("reason", "string", "玩家明确请求或共识的简短说明。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="pause_session",
                description="根据明确请求暂停当前跑团，但保留当前存档与场景。",
                handler=self.pause_session,
                parameters=(
                    GMToolParameter("reason", "string", "暂停原因。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="end_session",
                description="根据明确收团请求结算经验、总结本场、保存并关闭当前会话。",
                handler=self.end_session,
                parameters=(
                    GMToolParameter("title", "string", "本场标题；可以留空。"),
                    GMToolParameter("public_reply", "string", "面向玩家的自然收团话语。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字收团依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_scene",
                description=(
                    "建立一个非冲突场景，并同时写入GM私有的局面框架和面向玩家的开场。"
                    "这是局面准备，不是写死剧情；未公开内容之后可以调整。"
                ),
                handler=self.start_scene,
                parameters=(
                    GMToolParameter("name", "string", "场景名称。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "场景地点。", required=True),
                    GMToolParameter(
                        "participants",
                        "array",
                        (
                            "实际在场的角色与NPC名称，至少一名。第一章首场默认包含"
                            "current_state_summary.gameplay.characters中的全部PC，除非公开共识明确分队。"
                        ),
                        required=True,
                    ),
                    GMToolParameter("objective", "string", "当前场景公开目标或问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        "只供GM使用的局面、线索网、压力和未公开暗线；只能使用schema列出的字段。",
                        required=True,
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter("public_opening", "string", "先描述现场再把决定权交给玩家的自然开场。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中允许进入此场景的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="transition_scene",
                description=(
                    "把当前非冲突场景原子切换到邻近或叙事上连续的新场景。"
                    "玩家明确前往、离开或随行时，可把这段普通移动直接结算为抵达；"
                    "只移动获得授权的PC和实际随行NPC，不替任何角色完成抵达后的核对、交涉或选择。"
                ),
                handler=self.transition_scene,
                parameters=(
                    GMToolParameter("name", "string", "抵达后的场景名称。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "抵达后的非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "抵达地点。", required=True),
                    GMToolParameter("movers", "array", "本次明确移动的玩家角色名称。", required=True),
                    GMToolParameter("npc_companions", "array", "从当前场景实际随行的NPC名称。"),
                    GMToolParameter("destination_npcs", "array", "此前已确定在目的地等候的NPC名称。"),
                    GMToolParameter("objective", "string", "新场景当前公开目标或问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        "新场景只供GM使用的局面、线索网、压力和未公开暗线；只能使用schema列出的字段。",
                        required=True,
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter("transition_summary", "string", "旧场景已经发生的客观收束；不得提前兑现目的地事件。", required=True),
                    GMToolParameter("public_arrival", "string", "面向玩家的抵达描述；只呈现抵达时已可观察的事实。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中授权移动的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="focus_scene_branch",
                description=(
                    "当玩家角色不在当前镜头、但仍在另一个并行地点行动时，先把镜头切到该角色所在分支。"
                    "此工具只切换镜头并保留原分支，不结束场景、不公开叙事，也不代替随后的行动结算。"
                ),
                handler=self.focus_scene_branch,
                parameters=(
                    GMToolParameter("actor", "string", "需要获得镜头的玩家角色。", required=True),
                    GMToolParameter("name", "string", "该并行镜头的简短场景名。", required=True),
                    GMToolParameter(
                        "scene_type",
                        "string",
                        "并行镜头的非冲突场景类型。",
                        required=True,
                        enum=tuple(
                            item.value
                            for item in sorted(
                                self._GENERIC_SCENE_TYPES,
                                key=lambda item: item.value,
                            )
                        ),
                    ),
                    GMToolParameter("location", "string", "角色本次行动所在的具体地点。", required=True),
                    GMToolParameter("objective", "string", "该分支当前公开问题。"),
                    GMToolParameter(
                        "private_situation",
                        "object",
                        "新建分支时可选的GM私有局面；恢复既有分支时不会覆盖原框架。",
                        schema_details=self._private_situation_schema_details(),
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中该角色在另一分支行动的逐字依据。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
                max_successful_calls_per_message=1,
            )
        )
        registry.register(
            GMToolDefinition(
                name="end_scene",
                description="结束当前非冲突场景并统一清理场景级命刻、效果和待决窗口。",
                handler=self.end_scene,
                parameters=(
                    GMToolParameter("summary", "string", "本场景已经发生的客观结果。", required=True),
                    GMToolParameter("public_reply", "string", "面向玩家的自然转场或收束。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中的逐字转场依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_conflict",
                description=(
                    "把当前局面切入冲突，使用已经建档的PC、完整回合盟友NPC和敌人进行"
                    "团队先攻检定并建立双方交替回合。所有NPC必须先有规则战斗档案，"
                    "不能临时生成通用数值。"
                ),
                handler=self.start_conflict,
                parameters=(
                    GMToolParameter("scene_name", "string", "冲突名称。", required=True),
                    GMToolParameter("pcs", "array", "参战玩家角色名称。", required=True),
                    GMToolParameter(
                        "allied_npcs",
                        "array",
                        "可选；会在玩家方完整执行回合的盟友NPC名称。",
                    ),
                    GMToolParameter("enemies", "array", "参战敌人规则实体名称。", required=True),
                    GMToolParameter("leader", "string", "进行团队先攻检定的领队。", required=True),
                    GMToolParameter("supporters", "array", "协助先攻检定的其他PC。"),
                    GMToolParameter("objective", "string", "双方诉诸武力的当前目标。", required=True),
                    GMToolParameter("public_opening", "string", "先说清冲突如何爆发及眼前可观察局势。", required=True),
                    GMToolParameter("evidence", "string", "当前消息或系统GM节拍中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="run_current_npc_turn",
                description=(
                    "当冲突当前行动者是敌方或完整回合盟友NPC时，由核心GM从"
                    "current_state_summary.runtime.conflict.current_npc_tactical_snapshot.legal_actions"
                    "中直接选择一项并结算。规则层会校验目标、技能、法术、命刻与资源，"
                    "并从权威档案回填属性、伤害和消耗；不会再次调用NPC模型。"
                ),
                handler=self.run_current_npc_turn,
                parameters=(
                    GMToolParameter("expected_actor", "string", "GM预计的当前NPC；用于防止状态漂移。", required=True),
                    GMToolParameter(
                        "npc_action_type",
                        "string",
                        "必须逐字选择合法动作目录中的类型。",
                        required=True,
                        enum=(
                            "Attack",
                            "Spell",
                            "Guard",
                            "Hinder",
                            "Investigate",
                            "Objective",
                            "Skill",
                            "UltimaRecover",
                            "Escape",
                            "Surrender",
                        ),
                    ),
                    GMToolParameter("target", "string", "攻击、法术、妨碍、调查或技能的合法目标。"),
                    GMToolParameter("targets", "array", "多目标法术的合法目标列表。"),
                    GMToolParameter("guarded_target", "string", "防御时要掩护的合法盟友；只防御自己则留空。"),
                    GMToolParameter("spell_name", "string", "施法时逐字填写合法动作目录中的法术名。"),
                    GMToolParameter("chosen_damage_type", "string", "法术要求时选择的伤害类型。"),
                    GMToolParameter("chosen_status", "string", "法术要求时选择的一种异常状态。"),
                    GMToolParameter("chosen_statuses", "array", "法术要求两种异常状态时提交的列表。"),
                    GMToolParameter("attack_target", "string", "【抢攻】等法术提供顺势攻击时的攻击目标。"),
                    GMToolParameter("skill_name", "string", "使用技能时逐字填写合法动作目录中的技能名。"),
                    GMToolParameter(
                        "status_effect",
                        "string",
                        "妨碍时选择的状态。",
                        enum=("slow", "dazed", "weakened", "shaken"),
                    ),
                    GMToolParameter("clock_name", "string", "推进或倒转目标时逐字填写当前命刻名。"),
                    GMToolParameter(
                        "target_number",
                        "integer",
                        "推进目标时由GM根据局面选择的难度等级，至少为7；不要用命刻格数代替。",
                    ),
                    GMToolParameter("reasoning", "string", "只供审计的简短战术理由，不会公开。"),
                    GMToolParameter(
                        "action_description",
                        "string",
                        (
                            "核心GM直接写好的1到2句公开行动描述。只写NPC开始做什么、姿态与可见动作，"
                            "不得预告尚未掷出的成功、失败、伤害或状态结果；规则层会紧接着追加实际结算。"
                        ),
                        required=True,
                    ),
                    GMToolParameter("scene_brief", "string", "可选；本轮最相关的公开现场事实。"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="end_conflict",
                description="在冲突结果已经成立时结束冲突；可以保留同一地点继续普通场景，也可以结束整个场景。",
                handler=self.end_conflict,
                parameters=(
                    GMToolParameter("outcome", "string", "已经成立的冲突结果。", required=True),
                    GMToolParameter("continue_scene", "boolean", "是否留在同一地点继续普通场景。", required=True),
                    GMToolParameter("public_reply", "string", "面向玩家的冲突收束。", required=True),
                    GMToolParameter("evidence", "string", "当前消息或结算结果中的逐字依据。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        conflict_payload: dict[str, object] = {
            "active": bool(app.conflict_manager.state.active),
            "scene_name": app.conflict_manager.state.scene_name,
            "round": int(app.conflict_manager.state.round_number or 0),
            "current_actor": str(
                app.conflict_manager.state.current_actor() or ""
            ),
            "turn_order": list(app.conflict_manager.state.turn_order),
            "player_side": list(app.conflict_manager.state.player_side),
            "enemy_side": list(app.conflict_manager.state.enemy_side),
            "fallen_pcs": dict(app.conflict_manager.state.fallen_pcs),
            "sacrificed_pcs": sorted(app.conflict_manager.state.sacrifices),
            "pc_defeat_consequences": {
                name: list(consequences)
                for name, consequences in app.conflict_manager.state.pc_defeat_consequences.items()
            },
            "defeated_npc_fates": dict(
                app.conflict_manager.state.defeated_npc_fates
            ),
            "resolution_status": (
                app.conflict_manager.resolution_status()
            ),
        }
        current_actor = str(conflict_payload["current_actor"] or "")
        if (
            conflict_payload["active"]
            and current_actor
            and app.character_manager.exists(current_actor)
            and "pc" not in app.character_manager.get(current_actor).traits
            and {"enemy", "villain", "ally"}
            & set(app.character_manager.get(current_actor).traits)
            and getattr(app, "npc_combat_rules", None) is not None
        ):
            # State summaries are observations, not scene turns.  Building the
            # full panel would refresh scene frames, pacing plans and dynamic
            # memory recall before the GM has chosen any tool.  NPC combat
            # legality only needs the current clock board and combat state.
            panel = GamePanel(
                game_phase=app.conflict_manager.format_phase(),
                active_clocks=app.clock_manager.formatted(),
                pc_status=[],
                enemy_status=[],
                recent_chat=str(
                    context.metadata.get("recent_public_context") or ""
                ),
                current_actor=current_actor,
            )
            conflict_payload["current_npc_tactical_snapshot"] = (
                app.npc_combat_rules.build_tactical_snapshot(
                    panel,
                    current_actor,
                )
            )
        return {
            "gate": asdict(gate),
            "scene": (
                {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "objective": scene.objective,
                    "recovered_fallen_pcs": list(scene.recovered_fallen_pcs),
                }
                if scene is not None
                else None
            ),
            "suspended_scenes": [
                {
                    "scene_id": item.scene_id,
                    "name": item.name,
                    "scene_type": item.scene_type.value,
                    "location": item.location,
                    "participants": list(item.participants),
                    "objective": item.objective,
                }
                for item in app.scene_manager.suspended_scenes
            ],
            "conflict": conflict_payload,
        }

    def get_runtime_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="get_runtime_state",
            ok=True,
            result=self.state_summary(context),
        )

    @classmethod
    def _private_situation_schema_details(cls) -> dict[str, object]:
        return {
            "additionalProperties": False,
            "properties": {
                **{
                    name: {"type": "string"}
                    for name in sorted(cls._FRAME_SCALARS)
                },
                **{
                    name: {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                    for name in sorted(cls._FRAME_LISTS)
                },
            },
        }

    def start_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_session")
        if evidence_error is not None:
            return evidence_error
        phase = self._clean(arguments.get("phase"))
        runtime = self.host._runtime(context.campaign_id)
        ledger = runtime.app.session_ledger
        ledger_session_id = str(ledger.session_id or "").strip()
        if (
            ledger.active
            and ledger_session_id
            and ledger_session_id != context.session_id
        ):
            return self._failure(
                "start_session",
                "SESSION_LEDGER_ID_MISMATCH",
                (
                    f"战役仍有活动场次账本【{ledger_session_id}】，"
                    f"不能用新的场次标识【{context.session_id}】覆盖它。"
                ),
                "先读取当前会话状态，并继续、暂停后继续，或正常结束原场次。",
            )
        if ledger.active and phase != "adventure":
            return self._failure(
                "start_session",
                "ADVENTURE_LEDGER_STILL_ACTIVE",
                "当前冒险场次尚未收团，不能直接切回开团前或第零章阶段。",
                "先暂停以保留现场，或正常收团后再开启新的第零章讨论。",
            )
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        if gate.status == phase:
            return self._failure(
                "start_session",
                "SESSION_ALREADY_IN_PHASE",
                f"当前会话已经处于{phase}阶段。",
                "不要重复开启；根据当前阶段继续回应玩家。",
            )
        result = self.host._handle_gate_signal(
            {
                "campaign_id": context.campaign_id,
                "session_id": context.session_id,
                "channel_id": context.channel_id,
                "speaker": context.speaker,
                "message": str(context.metadata.get("current_message") or ""),
                # The outer GM transaction owns the only player-facing
                # opening, so state setup cannot trigger a nested model call.
                "defer_session_zero_opening": phase == "session_zero",
                "defer_adventure_opening": phase == "adventure",
            },
            gate=gate,
            signal=SessionGateSignal(
                kind="start",
                status=phase,
                reason=self._clean(arguments.get("reason")),
            ),
        )
        blocked = bool(result.get("blocked"))
        result = dict(result)
        current_scene = runtime.app.scene_manager.current_scene
        resuming_adventure = bool(
            phase == "adventure"
            and not blocked
            and current_scene is not None
            and current_scene.scene_type != SceneType.SESSION_ZERO
        )
        if phase == "session_zero" and not blocked:
            result["session_zero_opening_required"] = True
            result["opening_instruction"] = str(
                context.metadata.get("current_message") or ""
            ).strip()
        if phase == "adventure" and not blocked:
            result["adventure_opening_required"] = not resuming_adventure
            result["adventure_resumed"] = resuming_adventure
            if resuming_adventure:
                result["resumed_scene"] = {
                    "scene_id": current_scene.scene_id,
                    "name": current_scene.name,
                    "scene_type": current_scene.scene_type.value,
                    "location": current_scene.location,
                    "participants": list(current_scene.participants),
                    "objective": current_scene.objective,
                }
            else:
                result["allowed_followup_tools"] = ["start_scene"]
                result["required_followup_tools"] = ["start_scene"]
        return GMToolReceipt(
            tool_name="start_session",
            ok=not blocked,
            result=dict(result),
            error_code="ADVENTURE_START_BLOCKED" if blocked else "",
            message="尚未满足进入冒险的规则条件。" if blocked else "",
            correction_hint="根据blockers继续完成第零章或角色创建，不能声称第一章已经开始。" if blocked else "",
            retryable=blocked,
            state_changed=True,
            public_fallback_reply=(
                str(result.get("reply") or "").strip()
                if blocked or phase not in {"session_zero", "adventure"}
                else ""
            ),
            # A successful adventure gate intentionally grants exactly one
            # typed follow-up: start_scene.  Locking the empty receipt lets the
            # capability policy constrain that continuation without publishing
            # a meta acknowledgement between the gate and the scene opening.
            lock_public_reply=(
                blocked
                or (phase == "adventure" and not resuming_adventure)
            ),
        )

    def pause_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "pause_session")
        if evidence_error is not None:
            return evidence_error
        gate = self.host.session_gates.get(context.campaign_id, context.channel_id, context.session_id)
        if not gate.active:
            return self._failure("pause_session", "SESSION_NOT_ACTIVE", "当前没有正在进行的跑团会话。", "不要声称已经暂停。")
        runtime = self.host._runtime(context.campaign_id)
        with runtime.transaction_lock:
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
            updated = self.host.session_gates.pause(
                context.campaign_id,
                context.channel_id,
                context.session_id,
                reason=self._clean(arguments.get("reason")),
            )
        return GMToolReceipt(
            tool_name="pause_session",
            ok=True,
            result={"gate": asdict(updated), "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply="先停在这里，当前进度已经保存。",
        )

    def end_session(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_session")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        blocking = [
            window
            for window in runtime.app.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if blocking:
            return self._failure(
                "end_session",
                "BLOCKING_DECISION_PENDING",
                "仍有必须由玩家决定的规则窗口，不能替玩家跳过后收团。",
                "先让对应玩家处理归零或其他阻塞选择。",
                result={"pending_windows": [window.window_id for window in blocking]},
            )
        with runtime.transaction_lock:
            result = self.host._end_session(
                {
                    "campaign_id": context.campaign_id,
                    "session_id": context.session_id,
                    "channel_id": context.channel_id,
                    "title": self._clean(arguments.get("title")),
                }
            )
        if not bool(result.get("ok", True)):
            return self._failure(
                "end_session",
                str(result.get("error_code") or "END_SESSION_FAILED"),
                str(result.get("error") or "本场未能完成收团结算。"),
                "先处理返回的待决窗口或恢复有效会话，再重新收团。",
                result=dict(result),
            )
        return GMToolReceipt(
            tool_name="end_session",
            ok=True,
            result=dict(result),
            state_changed=True,
            public_fallback_reply=self._clean(arguments.get("public_reply")),
            lock_public_reply=True,
        )

    def start_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_scene")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "start_scene")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.conflict_manager.state.active:
            return self._failure(
                "start_scene",
                "CONFLICT_ACTIVE",
                "当前冲突仍在进行，不能直接建立普通场景。",
                "先调用end_conflict提交已经成立的冲突结果；不得用转场跳过回合、归零选择或敌人结局。",
            )
        lifecycle_error = self._active_scene_lifecycle_error(
            app,
            "start_scene",
        )
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "start_scene")
        if blocking_error is not None:
            return blocking_error
        participants, participants_error = self._string_list(
            arguments.get("participants"),
            tool_name="start_scene",
            field_name="participants",
            require_nonempty=True,
        )
        if participants_error is not None:
            return participants_error
        situation, situation_error = self._validate_private_situation(arguments.get("private_situation"))
        if situation_error is not None:
            return situation_error
        public_opening = self._clean_multiline(arguments.get("public_opening"))
        if not public_opening:
            return self._failure("start_scene", "PUBLIC_OPENING_REQUIRED", "场景开场不能为空。", "先描述现场，再把决定权交给玩家。")
        leak = self._private_leak(public_opening, situation)
        if leak:
            return self._failure(
                "start_scene",
                "PRIVATE_SCENE_INFORMATION_LEAK",
                f"公开开场泄露了GM私有字段【{leak}】。",
                "从公开开场移除未揭示暗线；可观察事实应放入visible_elements而非secrets。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure("start_scene", "INVALID_SCENE_TYPE", "场景类型无效。", "使用工具schema中的非冲突场景类型。")
        managed_type_error = self._generic_scene_type_error(
            scene_type,
            "start_scene",
        )
        if managed_type_error is not None:
            return managed_type_error
        current_scene = app.scene_manager.current_scene
        if (
            current_scene is not None
            and current_scene.scene_type != SceneType.SESSION_ZERO
        ):
            return self._failure(
                "start_scene",
                "SCENE_ALREADY_ACTIVE",
                f"当前场景【{current_scene.name}】仍在进行，不能被新场景静默覆盖。",
                (
                    "人物实际抵达新地点时使用transition_scene；"
                    "只结束当前场景则先调用end_scene。"
                ),
            )
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                scene = app.start_scene(
                    self._clean(arguments.get("name")),
                    scene_type,
                    location=self._clean(arguments.get("location")),
                    participants=participants,
                    objective=self._clean(arguments.get("objective")),
                )
                frame = app.scene_frame_manager.ensure_frame(
                    scene=scene,
                    recent_chat=public_opening,
                    world_state=app.world_state,
                    character_manager=app.character_manager,
                    contract=self._current_contract(app),
                )
                for key, value in situation.items():
                    setattr(frame, key, value)
                app.scene_frame_manager._touch(frame)
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure("start_scene", "SCENE_START_FAILED", str(exc), "修正场景参数后重新建立；不要声称场景已切换。")
        return GMToolReceipt(
            tool_name="start_scene",
            ok=True,
            result={
                "scene": {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "recovered_fallen_pcs": list(scene.recovered_fallen_pcs),
                },
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_opening,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    public_image=self._first_sentence(public_opening),
                    gm_beat_purpose=(
                        str(context.metadata.get("heartbeat_action") or "").strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def transition_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "transition_scene",
        )
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "transition_scene")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        current = app.scene_manager.current_scene
        if current is None:
            return self._failure(
                "transition_scene",
                "NO_ACTIVE_SCENE",
                "当前没有可供转出的场景。",
                "首次建立场景请使用start_scene。",
            )
        if app.conflict_manager.state.active or current.scene_type == SceneType.CONFLICT:
            return self._failure(
                "transition_scene",
                "CONFLICT_ACTIVE",
                "冲突仍在进行，不能用普通转场跳过回合或结果。",
                "先使用end_conflict结算冲突，再进行场景转场。",
            )
        lifecycle_error = self._active_scene_lifecycle_error(
            app,
            "transition_scene",
        )
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "transition_scene")
        if blocking_error is not None:
            return blocking_error

        movers, movers_error = self._string_list(
            arguments.get("movers"),
            tool_name="transition_scene",
            field_name="movers",
            require_nonempty=True,
        )
        if movers_error is not None:
            return movers_error
        companions, companions_error = self._string_list(
            arguments.get("npc_companions") or [],
            tool_name="transition_scene",
            field_name="npc_companions",
            require_nonempty=False,
        )
        if companions_error is not None:
            return companions_error
        destination_npcs, destination_error = self._string_list(
            arguments.get("destination_npcs") or [],
            tool_name="transition_scene",
            field_name="destination_npcs",
            require_nonempty=False,
        )
        if destination_error is not None:
            return destination_error

        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        non_pc_movers = [name for name in movers if name not in known_pcs]
        if non_pc_movers:
            return self._failure(
                "transition_scene",
                "MOVER_MUST_BE_PLAYER_CHARACTER",
                "movers只能包含玩家角色：" + "、".join(non_pc_movers),
                "随行NPC放入npc_companions；目的地NPC放入destination_npcs。",
            )
        if not context.metadata.get("system_gm_beat_request"):
            controls = self.host._player_character_control_map(runtime)
            known_ownership = any(controls.values())
            controlled = set(controls.get(context.speaker, []))
            unauthorized = [
                name for name in movers if known_ownership and name not in controlled
            ]
            if unauthorized:
                return self._failure(
                    "transition_scene",
                    "PLAYER_CHARACTER_NOT_CONTROLLED",
                    "发言者不能替其他玩家的角色转场：" + "、".join(unauthorized),
                    "只保留该玩家控制且在当前消息中明确行动的角色。",
                )
        resolved_companions: list[str] = []
        absent_companions: list[str] = []
        current_participants = set(current.participants)
        for name in companions:
            canonical = app.world_state.resolve_npc_name(name) or name
            if canonical not in current_participants and name not in current_participants:
                absent_companions.append(name)
                continue
            if canonical not in resolved_companions:
                resolved_companions.append(canonical)
        if absent_companions:
            return self._failure(
                "transition_scene",
                "NPC_COMPANION_NOT_PRESENT",
                "以下NPC不在当前场景，不能直接声明随行：" + "、".join(absent_companions),
                "只保留当前在场且已经答应随行的NPC；目的地人物使用destination_npcs。",
            )
        companions = resolved_companions
        destination_npcs = list(
            dict.fromkeys(
                app.world_state.resolve_npc_name(name) or name
                for name in destination_npcs
            )
        )

        situation, situation_error = self._validate_private_situation(
            arguments.get("private_situation"),
            tool_name="transition_scene",
        )
        if situation_error is not None:
            return situation_error
        public_arrival = self._clean_multiline(arguments.get("public_arrival"))
        if not public_arrival:
            return self._failure(
                "transition_scene",
                "PUBLIC_ARRIVAL_REQUIRED",
                "抵达描述不能为空。",
                "只描述移动完成后立即可观察的现场，不提前完成后续行动。",
            )
        leak = self._private_leak(public_arrival, situation)
        if leak:
            return self._failure(
                "transition_scene",
                "PRIVATE_SCENE_INFORMATION_LEAK",
                f"抵达描述泄露了GM私有字段【{leak}】。",
                "从公开描述移除未揭示暗线；可观察事实应放入visible_elements。",
            )
        name = self._clean(arguments.get("name"))
        location = self._clean(arguments.get("location"))
        transition_summary = self._clean(arguments.get("transition_summary"))
        if not name or not location or not transition_summary:
            return self._failure(
                "transition_scene",
                "TRANSITION_FIELDS_REQUIRED",
                "场景名称、地点和旧场景收束都不能为空。",
                "补充实际目的地与已经发生的离场事实，不预设抵达后的结果。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure(
                "transition_scene",
                "INVALID_SCENE_TYPE",
                "场景类型无效。",
                "使用工具schema中的非冲突场景类型。",
            )
        managed_type_error = self._generic_scene_type_error(
            scene_type,
            "transition_scene",
        )
        if managed_type_error is not None:
            return managed_type_error

        participants = list(dict.fromkeys([*movers, *companions, *destination_npcs]))
        # ``end_scene`` archives and clears the focused frame. Keep the
        # authoritative situation alive long enough to decide whether the
        # destination is another room of the same physical place.
        previous_frame = app.scene_frame_manager.current_frame
        snapshot = self._snapshot(app, context.campaign_id)
        action_round: dict[str, object] = {}
        action_round_events: list[dict[str, object]] = []
        public_reply = public_arrival
        try:
            with runtime.transaction_lock:
                ended = app.end_scene(
                    transition_summary,
                    restore_suspended=False,
                )
                scene = app.start_scene(
                    name,
                    scene_type,
                    location=location,
                    participants=participants,
                    objective=self._clean(arguments.get("objective")),
                )
                frame = app.scene_frame_manager.ensure_frame(
                    scene=scene,
                    recent_chat=public_arrival,
                    world_state=app.world_state,
                    character_manager=app.character_manager,
                    contract=self._current_contract(app),
                )
                continuity_inherited = (
                    app.scene_frame_manager.inherit_transition_continuity(
                        previous_frame,
                        frame,
                        scene=scene,
                    )
                )
                for key, value in situation.items():
                    setattr(frame, key, value)
                app.scene_frame_manager._touch(frame)
                if not context.metadata.get("system_gm_beat_request"):
                    for mover in movers:
                        event = app.record_free_scene_player_action(mover)
                        if event:
                            action_round = event
                            action_round_events.append(event)
                    clock_lines = app.turn_response_renderer.public_state_lines(
                        action_round,
                        existing_lines=[public_arrival],
                    )
                    if clock_lines:
                        public_reply = "\n".join([public_arrival, *clock_lines])
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "transition_scene",
                "SCENE_TRANSITION_FAILED",
                str(exc),
                "修正转场参数后重试；不要声称角色已经抵达。",
            )
        return GMToolReceipt(
            tool_name="transition_scene",
            ok=True,
            result={
                "ended_scene": ended.name if ended else "",
                "scene": {
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                },
                "movers": movers,
                "npc_companions": companions,
                "destination_npcs": destination_npcs,
                "location_continuity_inherited": continuity_inherited,
                "action_round": dict(action_round),
                "action_round_events": list(action_round_events),
                "allowed_followup_tools": [
                    "decide_npc_response",
                    "introduce_npc",
                    "start_conflict",
                ],
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    public_image=self._first_sentence(public_arrival),
                    gm_beat_purpose=(
                        str(context.metadata.get("heartbeat_action") or "").strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def focus_scene_branch(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        """Focus a split-party branch without ending the previous camera."""

        tool_name = "focus_scene_branch"
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            tool_name,
        )
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, tool_name)
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        current = app.scene_manager.current_scene
        if current is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_SCENE",
                "当前没有可暂存的聚焦场景。",
                "首次建立场景使用start_scene；普通物理转场使用transition_scene。",
            )
        if app.conflict_manager.state.active or current.scene_type == SceneType.CONFLICT:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "冲突中不能切换到并行普通镜头绕过回合。",
                "先按当前冲突回合行动或正式结束冲突。",
            )
        blocking_error = self._blocking_window_error(app, tool_name)
        if blocking_error is not None:
            return blocking_error

        actor = self._clean(arguments.get("actor"))
        if not actor or not app.character_manager.exists(actor):
            return self._failure(
                tool_name,
                "UNKNOWN_ACTOR",
                f"没有找到玩家角色【{actor or '未指定'}】。",
                "先读取当前角色与玩家映射。",
            )
        character = app.character_manager.get(actor)
        if "pc" not in character.traits:
            return self._failure(
                tool_name,
                "FOCUS_ACTOR_MUST_BE_PC",
                "并行玩家镜头只能由玩家角色发起。",
                "NPC行动继续使用NPC工具；不要把NPC当成玩家镜头。",
            )
        if not context.metadata.get("system_gm_beat_request"):
            controls = self.host._player_character_control_map(runtime)
            known_ownership = any(controls.values())
            if known_ownership and actor not in set(controls.get(context.speaker, [])):
                return self._failure(
                    tool_name,
                    "PLAYER_CHARACTER_NOT_CONTROLLED",
                    f"发言者不能替【{actor}】切换镜头。",
                    "只使用当前玩家实际控制且本句明确行动的角色。",
                )
        if actor in current.participants:
            return GMToolReceipt.success(
                tool_name,
                result={
                    "mode": "current",
                    "scene_id": current.scene_id,
                    "actor": actor,
                    "allowed_followup_tools": [
                        "perform_in_scene_action",
                        "perform_check_action",
                        "perform_character_action",
                        "decide_npc_response",
                    ],
                },
                state_changed=False,
            )

        name = self._clean(arguments.get("name"))
        location = self._clean(arguments.get("location"))
        if not name or not location:
            return self._failure(
                tool_name,
                "FOCUS_FIELDS_REQUIRED",
                "并行镜头必须有场景名和地点。",
                "依据角色最后位置与本次明确行动填写，不要虚构远距离移动。",
            )
        try:
            scene_type = SceneType(self._clean(arguments.get("scene_type")))
        except ValueError:
            return self._failure(
                tool_name,
                "INVALID_SCENE_TYPE",
                "并行镜头场景类型无效。",
                "使用工具schema中的非冲突场景类型。",
            )
        managed_type_error = self._generic_scene_type_error(
            scene_type,
            tool_name,
        )
        if managed_type_error is not None:
            return managed_type_error
        situation, situation_error = self._validate_private_situation(
            arguments.get("private_situation") or {},
            tool_name=tool_name,
        )
        if situation_error is not None:
            return situation_error

        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                app.scene_frame_manager.suspend_current_frame()
                scene, mode = app.scene_manager.focus_actor_branch(
                    actor,
                    name=name,
                    scene_type=scene_type,
                    location=location,
                    objective=self._clean(arguments.get("objective")),
                )
                frame = app.scene_frame_manager.restore_suspended_frame(scene)
                if frame is None:
                    frame = app.scene_frame_manager.ensure_frame(
                        scene=scene,
                        recent_chat=str(context.metadata.get("recent_public_context") or ""),
                        world_state=app.world_state,
                        character_manager=app.character_manager,
                        contract=self._current_contract(app),
                    )
                    for key, value in situation.items():
                        setattr(frame, key, value)
                    app.scene_frame_manager._touch(frame)
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                tool_name,
                "SCENE_FOCUS_FAILED",
                str(exc),
                "修正镜头参数后重试；不要改用普通转场结束另一分支。",
            )
        return GMToolReceipt.success(
            tool_name,
            result={
                "mode": mode,
                "actor": actor,
                "scene_id": scene.scene_id,
                "scene": {
                    "name": scene.name,
                    "scene_type": scene.scene_type.value,
                    "location": scene.location,
                    "participants": list(scene.participants),
                    "objective": scene.objective,
                },
                "suspended_scene_ids": [
                    item.scene_id for item in app.scene_manager.suspended_scenes
                ],
                "allowed_followup_tools": [
                    "perform_in_scene_action",
                    "move_scene_group",
                    "pass_in_scene_action",
                    "perform_check_action",
                    "perform_character_action",
                    "perform_scene_action",
                    "decide_npc_response",
                ],
                "required_followup_tools": [
                    "perform_in_scene_action",
                    "move_scene_group",
                    "pass_in_scene_action",
                    "perform_check_action",
                    "perform_character_action",
                    "perform_scene_action",
                    "decide_npc_response",
                ],
                "saved_path": saved_path,
            },
            state_changed=True,
        )

    def end_scene(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_scene")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.scene_manager.current_scene is None:
            return self._failure("end_scene", "NO_ACTIVE_SCENE", "当前没有可结束的场景。", "读取runtime state后再决定是否转场。")
        if app.conflict_manager.state.active:
            return self._failure("end_scene", "CONFLICT_ACTIVE", "当前仍在冲突中。", "先调用end_conflict处理冲突结果。")
        lifecycle_error = self._active_scene_lifecycle_error(app, "end_scene")
        if lifecycle_error is not None:
            return lifecycle_error
        blocking_error = self._blocking_window_error(app, "end_scene")
        if blocking_error is not None:
            return blocking_error
        with runtime.transaction_lock:
            ended = app.end_scene(self._clean(arguments.get("summary")))
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="end_scene",
            ok=True,
            result={"ended_scene": ended.name if ended else "", "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=self._clean_multiline(arguments.get("public_reply")),
            lock_public_reply=True,
        )

    def start_conflict(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "start_conflict")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "start_conflict")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if app.conflict_manager.state.active:
            return self._failure("start_conflict", "CONFLICT_ALREADY_ACTIVE", "冲突已经开始。", "继续当前回合，不要重新掷先攻。")
        blocking_error = self._blocking_window_error(app, "start_conflict")
        if blocking_error is not None:
            return blocking_error
        pcs, pcs_error = self._string_list(arguments.get("pcs"), tool_name="start_conflict", field_name="pcs", require_nonempty=True)
        if pcs_error is not None:
            return pcs_error
        allied_npcs, allied_error = self._string_list(
            arguments.get("allied_npcs") or [],
            tool_name="start_conflict",
            field_name="allied_npcs",
            require_nonempty=False,
        )
        if allied_error is not None:
            return allied_error
        enemies, enemy_error = self._string_list(arguments.get("enemies"), tool_name="start_conflict", field_name="enemies", require_nonempty=True)
        if enemy_error is not None:
            return enemy_error
        supporters, supporter_error = self._string_list(arguments.get("supporters") or [], tool_name="start_conflict", field_name="supporters", require_nonempty=False)
        if supporter_error is not None:
            return supporter_error
        side_duplicates = sorted(
            {
                name
                for name in [*pcs, *allied_npcs, *enemies]
                if sum(
                    name in side
                    for side in (pcs, allied_npcs, enemies)
                )
                > 1
            }
        )
        if side_duplicates:
            return self._failure(
                "start_conflict",
                "COMBATANT_ON_MULTIPLE_SIDES",
                "同一参战者不能同时属于多个阵营：" + "、".join(side_duplicates),
                "从pcs、allied_npcs和enemies中只保留一个归属。",
            )
        missing = [
            name
            for name in [*pcs, *allied_npcs, *enemies]
            if not app.character_manager.exists(name)
        ]
        if missing:
            return self._failure(
                "start_conflict",
                "COMBAT_PROFILE_REQUIRED",
                "以下参战者没有规则战斗档案：" + "、".join(missing),
                "PC先完成建卡；NPC先调用create_npc_combatant。禁止自动套用通用5级小兵。",
                result={"missing_combatants": missing},
            )
        invalid_pcs = [name for name in pcs if "pc" not in app.character_manager.get(name).traits]
        invalid_allies = [
            name
            for name in allied_npcs
            if (
                "ally" not in app.character_manager.get(name).traits
                or "pc" in app.character_manager.get(name).traits
                or {"enemy", "villain"}
                & set(app.character_manager.get(name).traits)
            )
        ]
        invalid_enemies = [
            name
            for name in enemies
            if not ({"enemy", "villain"} & set(app.character_manager.get(name).traits))
        ]
        if invalid_pcs or invalid_allies or invalid_enemies:
            return self._failure(
                "start_conflict",
                "COMBAT_SIDE_MISMATCH",
                "参战者阵营与规则档案不一致。",
                (
                    "pcs必须是玩家角色；allied_npcs必须有ally特质且不是PC或敌人；"
                    "enemies必须有enemy或villain特质。"
                ),
                result={
                    "invalid_pcs": invalid_pcs,
                    "invalid_allied_npcs": invalid_allies,
                    "invalid_enemies": invalid_enemies,
                },
            )
        sacrificed_pcs = [
            name for name in pcs if name in app.conflict_manager.state.sacrifices
        ]
        fallen_pcs = [
            name for name in pcs if name in app.conflict_manager.state.fallen_pcs
        ]
        zero_hp_combatants = [
            name
            for name in [*pcs, *allied_npcs, *enemies]
            if app.character_manager.get(name).hp <= 0
            and name not in app.conflict_manager.state.fallen_pcs
        ]
        if sacrificed_pcs:
            return self._failure(
                "start_conflict",
                "SACRIFICED_PC_CANNOT_RETURN",
                "已经牺牲的玩家角色不能再次加入冲突：" + "、".join(sacrificed_pcs),
                "从参战者中移除这些角色；牺牲通常是永久性的。",
                result={"sacrificed_pcs": sacrificed_pcs},
            )
        if fallen_pcs and app.scene_manager.current_scene is not None:
            return self._failure(
                "start_conflict",
                "FALLEN_PC_STILL_UNCONSCIOUS",
                "以下玩家角色在当前场景已经放弃抵抗，不能再次参战：" + "、".join(fallen_pcs),
                "结束当前场景；这些角色只会在下一次实际参与的新场景开始时恢复到危机值。",
                result={"fallen_pcs": fallen_pcs},
            )
        if zero_hp_combatants:
            return self._failure(
                "start_conflict",
                "ZERO_HP_COMBATANT_UNRESOLVED",
                "以下参战者仍为0生命值，不能开始新的冲突：" + "、".join(zero_hp_combatants),
                "先处理生命值归零的待决选择、恢复或敌人的退场结果。",
                result={"zero_hp_combatants": zero_hp_combatants},
            )
        leader = self._clean(arguments.get("leader"))
        if leader not in pcs:
            return self._failure("start_conflict", "INITIATIVE_LEADER_INVALID", "先攻领队不在参战PC中。", "从pcs中选择leader。")
        if any(name not in pcs or name == leader for name in supporters):
            return self._failure("start_conflict", "INITIATIVE_SUPPORTER_INVALID", "先攻协助者必须是除领队外的参战PC。", "修正supporters后重试。")
        opening = self._clean_multiline(arguments.get("public_opening"))
        if not opening:
            return self._failure("start_conflict", "CONFLICT_OPENING_REQUIRED", "冲突开场不能为空。", "先说清双方为什么此刻诉诸武力。")
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                scene_name = self._clean(arguments.get("scene_name"))
                objective = self._clean(arguments.get("objective"))
                scene = app.scene_manager.current_scene
                parent_scene = {
                    "_parent_scene_id": scene.scene_id if scene is not None else "",
                    "_parent_scene_name": scene.name if scene is not None else "",
                    "_parent_scene_type": (
                        scene.scene_type.value if scene is not None else ""
                    ),
                    "_parent_scene_objective": (
                        scene.objective if scene is not None else ""
                    ),
                    "_parent_scene_summary": (
                        scene.summary if scene is not None else ""
                    ),
                }
                if scene is None:
                    scene = app.start_scene(
                        scene_name,
                        SceneType.CONFLICT,
                        participants=[*pcs, *allied_npcs, *enemies],
                        objective=objective,
                    )
                else:
                    scene.scene_type = SceneType.CONFLICT
                    scene.name = scene_name
                    scene.objective = objective
                    for name in [*pcs, *allied_npcs, *enemies]:
                        app.scene_manager.add_participant(name)
                resolution = app.interceptor.resolve(
                    Action(
                        ActionType.START_CONFLICT,
                        {
                            "scene_name": scene_name,
                            "pcs": pcs,
                            "allied_npcs": allied_npcs,
                            "enemies": enemies,
                            "leader": leader,
                            "supporters": supporters,
                            **parent_scene,
                        },
                    )
                )
                app.resolution_committer.commit(resolution)
                current_actor = str(app.conflict_manager.state.current_actor() or "")
                initiative_pending = bool(
                    resolution.payload.get("initiative_pending")
                    and not app.conflict_manager.state.active
                )
                pending_decisions = self._pending_decision_summaries(
                    app,
                    transaction_id=str(
                        resolution.payload.get("check_batch_id") or ""
                    ),
                )
                required_followup_tools: list[str] = []
                required_followup_calls: list[dict[str, object]] = []
                gm_fumble_required = add_gm_fumble_followups(
                    pending_decisions=pending_decisions,
                    required_tools=required_followup_tools,
                    required_calls=required_followup_calls,
                )
                followup_mode = required_followup_mode(
                    required_followup_calls,
                    independent_obligation_added=gm_fumble_required,
                )
                decision_prompt = ""
                if initiative_pending and pending_decisions:
                    prompts = []
                    for pending in pending_decisions[:3]:
                        owner = str(pending["owner"] or "")
                        prefix = "GM" if owner == "__gm__" else f"【{owner}】"
                        prompt = str(pending["prompt"] or "").strip()
                        if prompt:
                            prompts.append(f"{prefix}：{prompt}")
                    decision_prompt = "\n".join(prompts)
                public_reply = "\n".join(
                    part
                    for part in (
                        opening,
                        str(resolution.rules_text or "").strip(),
                        decision_prompt,
                        f"轮到【{current_actor}】行动。" if current_actor else "",
                    )
                    if part
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure("start_conflict", "CONFLICT_START_FAILED", str(exc), "修正规则实体或先攻参数后重试；不要声称冲突已经开始。")
        return GMToolReceipt(
            tool_name="start_conflict",
            ok=True,
            result={
                "scene_id": scene.scene_id,
                "turn_order": list(app.conflict_manager.state.turn_order),
                "allied_npcs": list(allied_npcs),
                "current_actor": current_actor,
                "players_first": bool(resolution.payload.get("players_first")),
                "initiative_pending": initiative_pending,
                "check_batch_id": str(
                    resolution.payload.get("check_batch_id") or ""
                ),
                "pending_decisions": pending_decisions,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": followup_mode,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=not bool(
                        context.metadata.get("system_gm_beat_request")
                    ),
                    action_summary=str(
                        context.metadata.get("current_message") or ""
                    ).strip(),
                    consequence=(
                        f"冲突【{scene.name}】正在等待团队先攻定稿。"
                        if initiative_pending
                        else f"冲突【{scene.name}】开始。"
                    ),
                    public_image=self._first_sentence(public_reply),
                    opposition_move=(
                        self._first_sentence(public_reply)
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                    gm_beat_purpose=(
                        str(context.metadata.get("heartbeat_action") or "").strip()
                        if context.metadata.get("system_gm_beat_request")
                        else ""
                    ),
                )
            ],
        )

    def run_current_npc_turn(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app.conflict_manager.state.active:
            return self._failure("run_current_npc_turn", "NO_ACTIVE_CONFLICT", "当前没有冲突回合。", "不要生成NPC战斗行动。")
        actor = str(app.conflict_manager.state.current_actor() or "")
        expected = self._clean(arguments.get("expected_actor"))
        if actor != expected:
            return self._failure(
                "run_current_npc_turn",
                "NPC_TURN_STATE_CHANGED",
                f"当前行动者已经是【{actor or '无'}】，不是【{expected}】。",
                "重新读取get_runtime_state后再决定是否执行NPC回合。",
            )
        if not app.character_manager.exists(actor):
            return self._failure("run_current_npc_turn", "NPC_COMBAT_PROFILE_MISSING", f"【{actor}】没有战斗档案。", "先建立NPC战斗档案。")
        actor_traits = set(app.character_manager.get(actor).traits)
        if "pc" in actor_traits or not (
            {"enemy", "villain", "ally"} & actor_traits
        ):
            return self._failure("run_current_npc_turn", "CURRENT_ACTOR_IS_PLAYER", f"当前轮到玩家角色【{actor}】。", "等待该玩家行动，不得由GM代操。")
        snapshot = self._snapshot(app, context.campaign_id)
        action_parameters = {
            key: arguments[key]
            for key in (
                "npc_action_type",
                "target",
                "targets",
                "guarded_target",
                "spell_name",
                "chosen_damage_type",
                "chosen_status",
                "chosen_statuses",
                "attack_target",
                "skill_name",
                "status_effect",
                "clock_name",
                "target_number",
                "reasoning",
                "action_description",
            )
            if arguments.get(key) not in (None, "")
        }
        try:
            with runtime.transaction_lock:
                reply = app.run_npc_turn(
                    action_parameters,
                    self._clean(arguments.get("scene_brief")),
                )
                pending_decisions = self._pending_decision_summaries(app)
                required_followup_tools: list[str] = []
                required_followup_calls: list[dict[str, object]] = []
                gm_fumble_required = add_gm_fumble_followups(
                    pending_decisions=pending_decisions,
                    required_tools=required_followup_tools,
                    required_calls=required_followup_calls,
                )
                followup_mode = required_followup_mode(
                    required_followup_calls,
                    independent_obligation_added=gm_fumble_required,
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "run_current_npc_turn",
                "NPC_TURN_FAILED",
                str(exc),
                (
                    "保持当前行动者不变，重新读取current_npc_tactical_snapshot，"
                    "从legal_actions中选择一项并修正目标或名称后重试。"
                ),
            )
        return GMToolReceipt(
            tool_name="run_current_npc_turn",
            ok=True,
            result={
                "actor": actor,
                "selected_action": dict(action_parameters),
                "next_actor": str(app.conflict_manager.state.current_actor() or ""),
                "pending_decisions": pending_decisions,
                "allowed_followup_tools": list(required_followup_tools),
                "required_followup_tools": list(required_followup_tools),
                "required_followup_calls": list(required_followup_calls),
                "required_followup_mode": followup_mode,
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=self._clean_multiline(reply),
            lock_public_reply=True,
        )

    def end_conflict(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "end_conflict")
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        if not app.conflict_manager.state.active:
            return self._failure("end_conflict", "NO_ACTIVE_CONFLICT", "当前没有进行中的冲突。", "不要重复结束冲突。")
        blocking_error = self._blocking_window_error(app, "end_conflict")
        if blocking_error is not None:
            return blocking_error
        outcome = self._clean(arguments.get("outcome"))
        if not outcome:
            return self._failure(
                "end_conflict",
                "CONFLICT_OUTCOME_REQUIRED",
                "结束冲突时必须提交已经成立的客观结果。",
                "根据当前胜负、撤离、投降、谈判或目标完成情况写明结果；不能留空。",
            )
        continue_scene = bool(arguments.get("continue_scene"))
        public_reply = self._clean_multiline(arguments.get("public_reply"))
        if not public_reply:
            return self._failure(
                "end_conflict",
                "CONFLICT_CLOSING_REQUIRED",
                "结束冲突时必须给玩家一个可感知的收束。",
                "用自然叙事说明战斗如何停下以及眼前局面，不输出后台状态。",
            )
        conflict_state = app.conflict_manager.state
        scene = app.scene_manager.current_scene
        parent_scene_id = str(conflict_state.parent_scene_id or "")
        parent_scene_type = str(conflict_state.parent_scene_type or "")
        if parent_scene_id and (
            scene is None or str(scene.scene_id or "") != parent_scene_id
        ):
            return self._failure(
                "end_conflict",
                "CONFLICT_PARENT_SCENE_MISMATCH",
                "冲突所属的父场景与当前场景不一致，不能安全收束。",
                "重新读取当前场景与冲突状态；不要覆盖或跳过原场景。",
                result={
                    "parent_scene_id": parent_scene_id,
                    "current_scene_id": str(scene.scene_id or "") if scene else "",
                },
            )
        if (
            not continue_scene
            and parent_scene_type == SceneType.DUNGEON.value
            and app.dungeon_manager.state.active
        ):
            return self._failure(
                "end_conflict",
                "ACTIVE_DUNGEON_REQUIRES_SCENE_CONTINUATION",
                "这场冲突发生在仍在探索的地下城中，不能连同父场景一起直接结束。",
                (
                    "先用continue_scene=true结束冲突并返回地下城；"
                    "若队伍随后完成或撤离，再调用finish_dungeon_exploration。"
                ),
            )
        active_journey = app.travel_manager.active_journey
        if (
            not continue_scene
            and active_journey is not None
            and scene is not None
            and set(scene.participants).intersection(active_journey.party_names)
        ):
            return self._failure(
                "end_conflict",
                "ACTIVE_JOURNEY_REQUIRES_SCENE_CONTINUATION",
                "这场冲突发生在尚未结束的旅程中，不能连同旅行场景一起直接结束。",
                (
                    "先用continue_scene=true结束冲突并回到途中；"
                    "随后按实际结果调用continue_travel或abort_travel。"
                ),
            )
        with runtime.transaction_lock:
            if continue_scene:
                parent_scene_name = str(conflict_state.parent_scene_name or "")
                parent_scene_objective = str(
                    conflict_state.parent_scene_objective or ""
                )
                parent_scene_summary = str(conflict_state.parent_scene_summary or "")
                app.conflict_manager.end_scene(
                    list(scene.participants) if scene is not None else None
                )
                if scene is not None:
                    if parent_scene_id:
                        try:
                            scene.scene_type = SceneType(parent_scene_type)
                        except ValueError:
                            scene.scene_type = SceneType.STANDARD
                        scene.name = parent_scene_name or scene.name
                        scene.objective = parent_scene_objective
                        scene.summary = parent_scene_summary
                        if outcome:
                            scene.summary = (
                                f"{parent_scene_summary}\n{outcome}".strip()
                            )
                    else:
                        scene.scene_type = SceneType.STANDARD
                        scene.summary = outcome
            elif scene is not None:
                app.end_scene(outcome)
            else:
                app.conflict_manager.end_scene()
            saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        return GMToolReceipt(
            tool_name="end_conflict",
            ok=True,
            result={"outcome": outcome, "continued_scene": continue_scene, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=public_reply,
            lock_public_reply=True,
            pacing_events=[
                GMToolPacingEvent(
                    climax=outcome,
                    consequence=outcome,
                    public_image=self._first_sentence(arguments.get("public_reply")),
                    local_question_resolved=True,
                )
            ],
        )

    @classmethod
    def _active_scene_lifecycle_error(
        cls,
        app: Any,
        tool_name: str,
    ) -> GMToolReceipt | None:
        scene = app.scene_manager.current_scene
        if scene is None:
            return None
        if (
            app.dungeon_manager.state.active
            and scene.scene_type == SceneType.DUNGEON
        ):
            return cls._failure(
                tool_name,
                "ACTIVE_DUNGEON_REQUIRES_DUNGEON_TOOL",
                "当前镜头仍在进行地下城探索，不能用普通场景工具绕过区域、危险命刻或出口。",
                (
                    "区域行动使用ExploreDungeon；真实完成、撤退或放弃后调用"
                    "finish_dungeon_exploration。"
                ),
            )
        journey = app.travel_manager.active_journey
        if journey is not None and set(scene.participants).intersection(
            journey.party_names
        ):
            return cls._failure(
                tool_name,
                "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
                "当前镜头中的队伍仍在一段尚未结束的旅程中，不能用普通场景工具跳过旅行状态。",
                (
                    "处理途中事件后调用continue_travel；若玩家决定返程、停留或改道，"
                    "调用abort_travel。途中冲突使用start_conflict。"
                ),
            )
        return None

    @classmethod
    def _generic_scene_type_error(
        cls,
        scene_type: SceneType,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if scene_type in cls._GENERIC_SCENE_TYPES:
            return None
        typed_tool = {
            SceneType.TRAVEL: "travel_party",
            SceneType.DUNGEON: "start_dungeon_exploration",
            SceneType.REST: "perform_character_action（休息）",
        }.get(scene_type, "对应的专用规则工具")
        return cls._failure(
            tool_name,
            "MANAGED_SCENE_TYPE_REQUIRES_TYPED_TOOL",
            f"【{scene_type.value}】场景由专用规则生命周期管理，不能用普通场景工具建立。",
            f"调用{typed_tool}，让场景画面与规则状态在同一事务中生效。",
        )

    @classmethod
    def _validate_private_situation(
        cls,
        value: object,
        *,
        tool_name: str = "start_scene",
    ) -> tuple[dict[str, object], GMToolReceipt | None]:
        if not isinstance(value, dict):
            return {}, cls._failure(tool_name, "PRIVATE_SITUATION_MUST_BE_OBJECT", "private_situation必须是对象。", "按场景框架字段提交。")
        unknown = sorted(set(value) - cls._FRAME_SCALARS - cls._FRAME_LISTS)
        if unknown:
            return {}, cls._failure(tool_name, "UNKNOWN_SCENE_FRAME_FIELD", "场景框架包含未声明字段：" + "、".join(unknown), "只使用工具声明的局面字段。")
        result: dict[str, object] = {}
        for key in cls._FRAME_SCALARS:
            if key in value:
                result[key] = cls._clean(value.get(key))
        for key in cls._FRAME_LISTS:
            if key not in value:
                continue
            raw = value.get(key)
            if not isinstance(raw, list):
                return {}, cls._failure(tool_name, "SCENE_FRAME_LIST_REQUIRED", f"场景框架字段【{key}】必须是数组。", "改为字符串数组后重新提交。")
            result[key] = list(dict.fromkeys(cls._clean(item) for item in raw if cls._clean(item)))[:20]
        return result, None

    @classmethod
    def _private_leak(cls, public_reply: str, situation: dict[str, object]) -> str:
        compact_reply = " ".join(public_reply.split())
        for field in cls._HIDDEN_FRAME_FIELDS:
            raw = situation.get(field)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                secret = cls._clean(value)
                if len(secret) >= 4 and secret in compact_reply:
                    return field
        return ""

    @staticmethod
    def _current_contract(app: Any) -> Any:
        plan = getattr(app.story_arc_manager.state, "current_pacing_plan", None)
        return getattr(plan, "dramatic_contract", None)

    @staticmethod
    def _snapshot(app: Any, campaign_id: str) -> CampaignStateSnapshot:
        return CampaignStateTransaction.capture(app, campaign_id)

    @staticmethod
    def _restore(app: Any, snapshot: CampaignStateSnapshot) -> None:
        CampaignStateTransaction.restore(app, snapshot)

    @classmethod
    def _string_list(
        cls,
        value: object,
        *,
        tool_name: str,
        field_name: str,
        require_nonempty: bool,
    ) -> tuple[list[str], GMToolReceipt | None]:
        if not isinstance(value, list):
            return [], cls._failure(tool_name, "STRING_ARRAY_REQUIRED", f"参数【{field_name}】必须是字符串数组。", "按工具schema重新提交。")
        if any(not isinstance(item, str) for item in value):
            return [], cls._failure(tool_name, "STRING_ARRAY_REQUIRED", f"参数【{field_name}】只能包含字符串。", "删除对象或数字元素。")
        result = list(dict.fromkeys(cls._clean(item) for item in value if cls._clean(item)))
        if require_nonempty and not result:
            return [], cls._failure(tool_name, "NONEMPTY_ARRAY_REQUIRED", f"参数【{field_name}】不能为空。", "提供至少一个规则实体名称。")
        return result, None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(tool_name, "EVIDENCE_NOT_LITERAL", "evidence不是当前消息中的逐字连续片段。", "从current_message复制原句，不使用摘要或推断。")
        return None

    @classmethod
    def _require_adventure(
        cls,
        context: GMToolExecutionContext,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if context.gate_status == "adventure":
            return None
        return cls._failure(tool_name, "ADVENTURE_NOT_ACTIVE", "当前还未进入冒险阶段。", "先完成会话门控与第零章，再建立冒险场景。")

    @classmethod
    def _blocking_window_error(cls, app: Any, tool_name: str) -> GMToolReceipt | None:
        blocking = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if not blocking:
            return None
        return cls._failure(
            tool_name,
            "BLOCKING_DECISION_PENDING",
            "仍有必须由玩家本人完成的规则选择，不能切换流程。",
            "先处理对应DecisionWindow。",
            result={"pending_windows": [window.window_id for window in blocking]},
        )

    @staticmethod
    def _pending_decision_summaries(
        app: Any,
        *,
        transaction_id: str = "",
    ) -> list[dict[str, object]]:
        return [
            {
                "window_id": window.window_id,
                "kind": window.kind,
                "owner": window.owner,
                "prompt": window.prompt,
                "options": list(window.options),
                "blocking": bool(window.blocking),
                "allowed_responders": list(window.allowed_responders),
                "payload": dict(window.payload),
            }
            for window in app.interceptor.decision_window_manager.pending()
            if not transaction_id or window.transaction_id == transaction_id
        ]

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _clean_multiline(value: object) -> str:
        return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()

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
    def _failure(
        tool_name: str,
        code: str,
        message: str,
        hint: str,
        *,
        result: dict[str, object] | None = None,
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            result=dict(result or {}),
            public_fallback_reply="这一步还没有生效，我需要先把当前状态或规则条件确认清楚。",
        )
