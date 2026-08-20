from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import CampaignStateTransaction
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.models import (
    DungeonExploreMode,
    DungeonImportance,
    DungeonPreparation,
    SceneType,
    TravelEventType,
)


class DungeonToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...


class GMDungeonToolService:
    """Typed lifecycle boundary for preparing, entering and leaving dungeons."""

    def __init__(self, host: DungeonToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_dungeon_state",
                description=(
                    "查看当前地下城、区域结构、危险命刻和既有准备；不推进探索，"
                    "也不把隐藏陷阱或奖励直接说给玩家。"
                ),
                handler=self.get_dungeon_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="start_dungeon_exploration",
                description=(
                    "当队伍已经实际进入一个结构复杂、值得探索的地点时，准备并开始地下城。"
                    "工具会根据重要性和准备程度选择地下城场景、细致探索或幕间叙事模式，"
                    "建立区域骨架和场景级危险命刻。调用前提：队伍已经实际进入；"
                    "发现入口或讨论是否进入仍属于入口场景。"
                    "旅行途中若当前待处理事件明确是地下城发现，可暂时挂起旅程进入探索；"
                    "离开后仍须处理该旅行事件才能继续上路。"
                ),
                handler=self.start_dungeon_exploration,
                parameters=(
                    GMToolParameter("name", "string", "地下城的稳定名称。", required=True),
                    GMToolParameter("location", "string", "队伍实际进入的地点。", required=True),
                    GMToolParameter(
                        "importance",
                        "string",
                        "major为重要地点，minor为次要地点。",
                        enum=tuple(item.value for item in DungeonImportance),
                    ),
                    GMToolParameter(
                        "preparation",
                        "string",
                        "prepared为事先准备，improvised为现场即兴。",
                        enum=tuple(item.value for item in DungeonPreparation),
                    ),
                    GMToolParameter(
                        "mode",
                        "string",
                        "通常省略，让规则层依重要性推荐。",
                        enum=tuple(item.value for item in DungeonExploreMode),
                    ),
                    GMToolParameter("purpose", "string", "英雄为什么来到这里、想找到什么。"),
                    GMToolParameter("concept", "string", "地点形态；省略时由地下城表生成。"),
                    GMToolParameter("focus", "string", "地下城核心人物、物品、真相或目标。"),
                    GMToolParameter("inhabitants", "string", "主要居民或敌对存在。"),
                    GMToolParameter("peculiarity", "string", "能被感知和互动的鲜明特异点。"),
                    GMToolParameter(
                        "participants",
                        "array",
                        "实际一同进入者；通常省略以沿用当前场景在场人物。",
                        schema_details={"items": {"type": "string"}},
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中证明队伍实际进入的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="finish_dungeon_exploration",
                description=(
                    "队伍已经真正离开地下城时，按完成、撤退或放弃的实际结果结束探索、"
                    "归档场景级危险命刻，并把真实同行者带到出口场景。"
                    "调用前提：Boss战和阻塞选择均已结清。"
                ),
                handler=self.finish_dungeon_exploration,
                parameters=(
                    GMToolParameter(
                        "outcome",
                        "string",
                        "实际离开结果：目标解决、主动撤退或放弃探索。",
                        required=True,
                        enum=("completed", "retreated", "abandoned"),
                    ),
                    GMToolParameter("completion_reason", "string", "已经发生的收束结果。", required=True),
                    GMToolParameter("exit_location", "string", "离开后实际到达的位置；默认地下城入口地点。"),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中证明队伍已经离开的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )

    def get_dungeon_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        manager = runtime.app.dungeon_manager
        return GMToolReceipt(
            tool_name="get_dungeon_state",
            ok=True,
            result={
                "current": self._primitive(manager.state),
                "prepared_briefs": [
                    self._primitive(brief) for brief in manager.design_history
                ],
                "history": [self._primitive(state) for state in manager.history],
                "status": manager.format_status(),
            },
        )

    def start_dungeon_exploration(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "start_dungeon_exploration"
        error = self._require_adventure(context, tool_name)
        if error is not None:
            return error
        error = self._validate_evidence(context, arguments.get("evidence"), tool_name)
        if error is not None:
            return error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        error = self._blocking_error(app, tool_name)
        if error is not None:
            return error
        if app.conflict_manager.state.active:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "冲突尚未结束，不能把战斗中的撤离意图直接写成已经进入地下城。",
                "先结算冲突或完成真实转场。",
            )
        active_journey = app.travel_manager.active_journey
        pending_travel_event = app.travel_manager.pending_travel_event()
        travel_discovery_interruption = bool(
            active_journey is not None
            and active_journey.status == "event_pending"
            and pending_travel_event is not None
            and pending_travel_event.event_type == TravelEventType.DISCOVERY
            and "dungeon" in set(pending_travel_event.danger_tags)
        )
        if active_journey is not None and not travel_discovery_interruption:
            return self._failure(
                tool_name,
                "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
                "队伍仍在进行中的旅程里，不能直接覆盖成地下城探索。",
                (
                    "只有当前待处理的旅行事件明确发现了地下城入口时，才能暂时离开路线进入探索；"
                    "其他情况先处理途中事件并继续旅行，或由玩家明确中止行程。"
                ),
            )
        if app.dungeon_manager.state.active:
            return self._failure(
                tool_name,
                "DUNGEON_ALREADY_ACTIVE",
                f"地下城【{app.dungeon_manager.state.name}】仍在探索中。",
                "继续探索当前地下城，或在真实离开后先调用finish_dungeon_exploration。",
            )

        name = self._clean(arguments.get("name"))
        location = self._clean(arguments.get("location"))
        if not name or not location:
            return self._failure(
                tool_name,
                "DUNGEON_IDENTITY_REQUIRED",
                "开始地下城需要明确名称和实际地点。",
                "从当前公开场景取值；队伍实际抵达入口后再开始地下城。",
            )
        participants, error = self._participants(app, arguments.get("participants"))
        if error is not None:
            return error

        snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                brief = app.dungeon_manager.design_dungeon(
                    name,
                    importance=self._clean(arguments.get("importance"))
                    or DungeonImportance.MAJOR,
                    preparation=self._clean(arguments.get("preparation"))
                    or DungeonPreparation.PREPARED,
                    purpose=self._clean(arguments.get("purpose")),
                    concept=self._clean(arguments.get("concept")),
                    focus=self._clean(arguments.get("focus")),
                    inhabitants=self._clean(arguments.get("inhabitants")),
                    peculiarity=self._clean(arguments.get("peculiarity")),
                    mode=self._clean(arguments.get("mode")) or None,
                )
                app.scene_manager.start_scene(
                    name,
                    SceneType.DUNGEON,
                    location=location,
                    participants=participants,
                    objective=brief.purpose,
                )
                state = app.dungeon_manager.start_from_brief(
                    brief,
                    location=location,
                )
                if app.world_map_manager is not None:
                    app.world_map_manager.enrich_dungeon_state(state)
                app.world_state.record_memory_event(
                    f"队伍进入地下城【{name}】。",
                    kind="dungeon_started",
                    entities=[name, location, *participants],
                    tags=["dungeon", "scene"],
                    source="GMDungeonToolService",
                    payload={
                        "mode": state.mode.value,
                        "purpose": brief.purpose,
                        "suspended_journey_id": (
                            active_journey.journey_id
                            if travel_discovery_interruption
                            and active_journey is not None
                            else ""
                        ),
                        "travel_event_day": (
                            pending_travel_event.day
                            if travel_discovery_interruption
                            and pending_travel_event is not None
                            else 0
                        ),
                    },
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, snapshot)
            return self._failure(
                tool_name,
                "DUNGEON_START_REJECTED",
                str(exc) or "地下城未能建立。",
                "地下城状态保持未开始；修正名称、地点、模式或参与者后重试。",
            )

        entrance = next(
            (
                area
                for area in state.areas
                if area.name == state.current_area
            ),
            None,
        )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "brief": self._primitive(brief),
                "state": self._primitive(state),
                "scene_id": app.scene_manager.current_scene.scene_id,
                "participants": participants,
                "journey_suspended": travel_discovery_interruption,
                "suspended_journey_id": (
                    active_journey.journey_id
                    if travel_discovery_interruption
                    and active_journey is not None
                    else ""
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=(
                entrance.description
                if entrance is not None
                else f"队伍进入了【{name}】。"
            ),
        )

    def finish_dungeon_exploration(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "finish_dungeon_exploration"
        error = self._require_adventure(context, tool_name)
        if error is not None:
            return error
        error = self._validate_evidence(context, arguments.get("evidence"), tool_name)
        if error is not None:
            return error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        error = self._blocking_error(app, tool_name)
        if error is not None:
            return error
        if app.conflict_manager.state.active:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "冲突尚未结束，不能直接收束地下城。",
                "先结算冲突；不能跳过角色归零选择、反派逃跑或投降。",
            )
        if not app.dungeon_manager.state.active:
            return self._failure(
                tool_name,
                "DUNGEON_NOT_ACTIVE",
                "当前没有进行中的地下城。",
                "读取get_dungeon_state确认当前状态。",
            )
        completion_reason = self._clean(arguments.get("completion_reason"))
        outcome = self._clean(arguments.get("outcome")).lower() or "completed"
        if outcome not in {"completed", "retreated", "abandoned"}:
            return self._failure(
                tool_name,
                "DUNGEON_OUTCOME_INVALID",
                "地下城离开结果必须是completed、retreated或abandoned。",
                "根据已经发生的剧情选择真实结果，不要把撤退写成完成。",
            )
        if not completion_reason:
            return self._failure(
                tool_name,
                "DUNGEON_COMPLETION_REQUIRED",
                "地下城收束需要一个已经发生的完成或离开结果。",
                "填写completion_reason；尚未发生时继续探索。",
            )

        state = app.dungeon_manager.state
        scene = app.scene_manager.current_scene
        participants = list(scene.participants) if scene is not None else []
        if not participants:
            participants = [
                character.name
                for character in app.character_manager.all()
                if "pc" in character.traits
            ]
        exit_location = self._clean(arguments.get("exit_location")) or state.location
        suspended_journey = app.travel_manager.active_journey
        resumes_travel_event = bool(
            suspended_journey is not None
            and suspended_journey.status == "event_pending"
            and app.travel_manager.pending_travel_event() is not None
        )
        snapshot = CampaignStateTransaction.capture(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                ended = app.end_dungeon(completion_reason, outcome=outcome)
                if ended is None:
                    raise RuntimeError("地下城状态在收束前已经结束。")
                app.scene_manager.start_scene(
                    f"{ended.name}出口",
                    SceneType.STANDARD,
                    location=exit_location,
                    participants=participants,
                    objective="决定下一步行程",
                    summary=completion_reason,
                )
                app.world_state.record_memory_event(
                    f"地下城【{ended.name}】探索结束：{completion_reason}",
                    kind=(
                        "dungeon_finished"
                        if outcome == "completed"
                        else "dungeon_departed"
                    ),
                    entities=[ended.name, exit_location, *participants],
                    tags=[
                        "dungeon",
                        {
                            "completed": "resolved",
                            "retreated": "retreated",
                            "abandoned": "abandoned",
                        }[outcome],
                    ],
                    source="GMDungeonToolService",
                    payload={"outcome": outcome},
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            CampaignStateTransaction.restore(app, snapshot)
            return self._failure(
                tool_name,
                "DUNGEON_FINISH_REJECTED",
                str(exc) or "地下城未能收束。",
                "保持现有地下城状态，先解决冲突或待决选择后重试。",
            )

        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={
                "ended": self._primitive(ended),
                "outcome": outcome,
                "exit_location": exit_location,
                "participants": participants,
                "journey_event_still_pending": resumes_travel_event,
                "resume_journey_id": (
                    suspended_journey.journey_id
                    if resumes_travel_event and suspended_journey is not None
                    else ""
                ),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=completion_reason,
        )

    @classmethod
    def _participants(
        cls,
        app: Any,
        value: object,
    ) -> tuple[list[str], GMToolReceipt | None]:
        current = app.scene_manager.current_scene
        current_participants = list(current.participants) if current is not None else []
        if value in (None, []):
            participants = current_participants
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            participants = list(
                dict.fromkeys(cls._clean(item) for item in value if cls._clean(item))
            )
            if current is not None:
                remote = [name for name in participants if name not in current_participants]
                if remote:
                    return [], cls._failure(
                        "start_dungeon_exploration",
                        "DUNGEON_PARTICIPANT_NOT_PRESENT",
                        "以下人物不在当前场景，不能被带入地下城：" + "、".join(remote),
                        "只使用当前在场人物；先完成真实转场或切换分队。",
                    )
        else:
            return [], cls._failure(
                "start_dungeon_exploration",
                "DUNGEON_PARTICIPANTS_MUST_BE_ARRAY",
                "地下城参与者必须是人物名称数组。",
                "省略participants以使用当前场景在场人物。",
            )

        if not participants:
            participants = [
                character.name
                for character in app.character_manager.all()
                if "pc" in character.traits
            ]
        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        if not any(name in known_pcs for name in participants):
            return [], cls._failure(
                "start_dungeon_exploration",
                "DUNGEON_PC_REQUIRED",
                "地下城场景至少需要一名实际在场的玩家角色。",
                "先建立玩家角色所在场景，再开始地下城。",
            )
        return participants, None

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if is_current_message_evidence(context, value):
            return None
        return cls._failure(
            tool_name,
            "EVIDENCE_NOT_LITERAL",
            "evidence不是当前消息中的逐字连续片段。",
            "从current_message复制证明进入或离开的原句；不得使用路由摘要。",
        )

    @classmethod
    def _require_adventure(
        cls,
        context: GMToolExecutionContext,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if context.gate_status == "adventure":
            return None
        return cls._failure(
            tool_name,
            "ADVENTURE_NOT_ACTIVE",
            "当前还没有进入可结算地下城的冒险阶段。",
            "先完成第零章并开始第一章。",
        )

    @classmethod
    def _blocking_error(cls, app: Any, tool_name: str) -> GMToolReceipt | None:
        windows = [
            window
            for window in app.interceptor.decision_window_manager.pending()
            if window.blocking
        ]
        if not windows:
            return None
        return cls._failure(
            tool_name,
            "BLOCKING_DECISION_PENDING",
            "仍有必须先处理的规则选择。",
            "先处理当前DecisionWindow，不能跳过玩家选择。",
        )

    @classmethod
    def _primitive(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return cls._primitive(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._primitive(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._primitive(item) for item in value]
        return value

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _failure(
        tool_name: str,
        code: str,
        message: str,
        hint: str,
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code=code,
            message=message,
            correction_hint=hint,
            retryable=True,
            public_fallback_reply="这一步还没有生效。",
        )
