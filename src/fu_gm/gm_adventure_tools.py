from __future__ import annotations

import hashlib
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Protocol

from fu_gm.components.campaign_state_transaction import (
    CampaignStateSnapshot,
    CampaignStateTransaction,
)
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.equipment_catalog import get_equipment_example
from fu_gm.gm_evidence import is_current_message_evidence
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.models import Action, ActionType, PersistentChangeType, TravelRouteType, TravelThreatLevel


class AdventureToolHost(Protocol):
    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> Any: ...

    def _autosave_campaign(self, runtime: Any, campaign_id: str) -> str: ...

    @staticmethod
    def _player_character_control_map(runtime: Any) -> dict[str, list[str]]: ...


class GMAdventureToolService:
    """Travel and GM-authored reward capabilities for the tool agent.

    The model selects a semantic capability. Existing TravelManager,
    WorldMapManager, EconomyManager and ActionInterceptor remain the only
    authorities for dice, route length, costs and rewards.
    """

    _DIFFICULTIES = ("easy", "normal", "hard", "boss")

    def __init__(self, host: AdventureToolHost) -> None:
        self.host = host

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_travel_state",
                description="查看已发现地点、已登记路线、交通方式与上一段旅程；不推进旅行。",
                handler=self.get_travel_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="get_progression_state",
                description="查看玩家角色的等级、经验、职业等级和当前是否可以升级；不修改角色卡。",
                handler=self.get_progression_state,
            )
        )
        registry.register(
            GMToolDefinition(
                name="level_up_character",
                description=(
                    "根据玩家明确选择，为其角色消耗10点经验并提升一级。"
                    "规则层会校验职业、技能等级上限、20/40级属性提升和职业精通英雄技能；"
                    "信息不足时返回需要追问的具体项目。"
                ),
                handler=self.level_up_character,
                parameters=(
                    GMToolParameter("character_name", "string", "要升级的玩家角色。", required=True),
                    GMToolParameter("class_name", "string", "本级投入的职业。", required=True),
                    GMToolParameter("skill_name", "string", "本级获得或提升的该职业技能。", required=True),
                    GMToolParameter("attribute_increase", "string", "升至20或40级时选择提升的属性。"),
                    GMToolParameter("hero_skill", "string", "职业升至10级时选择的英雄技能。"),
                    GMToolParameter("status_immunity", "string", "英雄技能要求时选择的异常状态免疫。"),
                    GMToolParameter("extra_spells", "array", "英雄技能要求时选择的额外法术名。"),
                    GMToolParameter("new_identity", "string", "玩家同时明确改变的新身份。"),
                    GMToolParameter("new_theme", "string", "玩家同时明确改变的新主题。"),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中升级选择的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="travel_party",
                description=(
                    "在冒险阶段按地图路线与威胁骰结算一段明确的队伍旅行。"
                    "目的地或是否立即出发仍不明确时不要调用；危险和发现只提交规则种子，不擅自结算后续冲突。"
                ),
                handler=self.travel_party,
                parameters=(
                    GMToolParameter("origin", "string", "当前出发地点。", required=True),
                    GMToolParameter("destination", "string", "玩家明确决定前往的地点。", required=True),
                    GMToolParameter(
                        "participants",
                        "array",
                        "实际同行的人物名称；省略时只带上当前场景中的玩家角色。",
                    ),
                    GMToolParameter("transport", "string", "交通方式，默认徒步。"),
                    GMToolParameter("payer", "string", "雇佣旅行服务时付费的玩家角色。"),
                    GMToolParameter("explicit_distance", "integer", "没有已登记路线时由GM裁定的徒步旅行日距离。"),
                    GMToolParameter(
                        "route_type",
                        "string",
                        "明确路线类型；通常让地图推导。",
                        enum=tuple(item.value for item in TravelRouteType),
                    ),
                    GMToolParameter(
                        "default_threat_level",
                        "string",
                        "没有路线分段时使用的威胁等级。",
                        enum=tuple(item.value for item in TravelThreatLevel),
                    ),
                    GMToolParameter("evidence", "string", "当前消息中明确出发决定的逐字片段。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="continue_travel",
                description=(
                    "当前旅程因危险或发现暂停，且该事件已经在场景中真正处理完毕后继续旅行。"
                    "不能只凭玩家打算处理、正在尝试或GM预想结果调用；若途中再次发生事件，"
                    "旅程会再次暂停。"
                ),
                handler=self.continue_travel,
                parameters=(
                    GMToolParameter(
                        "event_resolution",
                        "string",
                        "已经发生并公开成立的处理结果；不得写计划、假设或未结算的成功。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中证明途中事件已解决、并准备继续上路的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="abort_travel",
                description=(
                    "玩家已经明确决定返程、停留或放弃当前目的地时，中止当前旅程并把队伍"
                    "留在实际到达的位置。只表达担忧、讨论备选路线或尚在处理途中冲突时不要调用。"
                ),
                handler=self.abort_travel,
                parameters=(
                    GMToolParameter(
                        "reason",
                        "string",
                        "已经成立的返程、停留或放弃原因。",
                        required=True,
                    ),
                    GMToolParameter(
                        "end_location",
                        "string",
                        "队伍中止旅程后实际所在的位置。",
                        required=True,
                    ),
                    GMToolParameter(
                        "evidence",
                        "string",
                        "当前消息中明确停止当前旅程的逐字片段。",
                        required=True,
                        source="current_message",
                    ),
                ),
                side_effect="write",
            )
        )
        registry.register(
            GMToolDefinition(
                name="award_stage_reward",
                description=(
                    "当场景或冲突已经明确产生奖励时，按队伍等级和难度发放阶段宝藏。"
                    "不能把普通调查、尚未打开的宝箱或未解决的战斗当成已经获得奖励。"
                ),
                handler=self.award_stage_reward,
                parameters=(
                    GMToolParameter("recipients", "array", "获得奖励的玩家角色；省略时为全部在档PC。"),
                    GMToolParameter("difficulty", "string", "奖励档位。", required=True, enum=self._DIFFICULTIES),
                    GMToolParameter("rare_item", "string", "明确指定且已登记的稀有物品；通常留空让规则层选择。"),
                    GMToolParameter("source", "string", "奖励来自哪个已解决事件。", required=True),
                    GMToolParameter("evidence", "string", "当前消息中证明事件已解决或奖励已取得的逐字片段。", required=True, source="current_message"),
                ),
                side_effect="write",
            )
        )

    def state_summary(self, context: GMToolExecutionContext) -> dict[str, object]:
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        travel = app.travel_manager
        locations = [
            location.name
            for location in app.world_state.map_locations.values()
            if bool(location.discovered)
        ]
        return {
            "discovered_locations": locations,
            "known_routes": [self._primitive(route) for route in travel.routes.values()] if travel else [],
            "transport_options": [
                {
                    "name": option.name,
                    "route_type": option.route_type.value,
                    "price": option.price,
                    "passenger_capacity": option.passenger_capacity,
                    "travel_multiplier": option.travel_multiplier,
                    "owned_asset": option.owned,
                }
                for option in TravelManager.TRANSPORT_OPTIONS.values()
            ],
            "last_journey": self._primitive(travel.last_journey) if travel and travel.last_journey else None,
            "active_journey": (
                self._primitive(travel.active_journey)
                if travel and travel.active_journey
                else None
            ),
            "pending_event": (
                self._primitive(travel.pending_travel_event())
                if travel and travel.pending_travel_event()
                else None
            ),
            "interrupted_journeys": (
                [
                    self._primitive(item)
                    for item in travel.interrupted_journeys
                ]
                if travel
                else []
            ),
        }

    def get_travel_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        return GMToolReceipt(
            tool_name="get_travel_state",
            ok=True,
            result=self.state_summary(context),
        )

    def get_progression_state(
        self,
        context: GMToolExecutionContext,
        _arguments: dict[str, object],
    ) -> GMToolReceipt:
        runtime = self.host._runtime(context.campaign_id)
        rows = []
        for character in runtime.app.character_manager.all():
            if "pc" not in character.traits:
                continue
            rows.append(
                {
                    "name": character.name,
                    "level": character.level,
                    "experience_points": character.experience_points,
                    "can_level_up": runtime.app.progression_manager.can_level_up(
                        character.name
                    ),
                    "classes": dict(character.classes),
                    "skills": dict(character.skills),
                }
            )
        return GMToolReceipt(
            tool_name="get_progression_state",
            ok=True,
            result={"characters": rows},
        )

    def level_up_character(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(
            context,
            arguments.get("evidence"),
            "level_up_character",
        )
        if evidence_error is not None:
            return evidence_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        blocking_error = self._blocking_window_error(app, "level_up_character")
        if blocking_error is not None:
            return blocking_error
        if app.conflict_manager.state.active or app.session_ledger.active:
            return self._failure(
                "level_up_character",
                "LEVEL_UP_DURING_ACTIVE_SESSION",
                "升级应在一场游戏结束并结算经验后完成，不能插入正在进行的冒险或冲突。",
                "先正常收团并结算本场经验，再由角色操作者选择这一级。",
            )
        character_name = self._clean(arguments.get("character_name"))
        if (
            not character_name
            or not app.character_manager.exists(character_name)
            or "pc" not in app.character_manager.get(character_name).traits
        ):
            return self._failure(
                "level_up_character",
                "UNKNOWN_PLAYER_CHARACTER",
                f"没有找到可升级的玩家角色【{character_name or '未指定'}】。",
                "先调用get_progression_state并使用其中的标准角色名。",
            )
        control_map = self.host._player_character_control_map(runtime)
        known_owners = [
            player
            for player, heroes in control_map.items()
            if character_name in heroes
        ]
        controlled = list(control_map.get(context.speaker, []))
        if (
            known_owners
            and context.speaker not in known_owners
        ) or (controlled and character_name not in controlled):
            return self._failure(
                "level_up_character",
                "CHARACTER_NOT_CONTROLLED_BY_SPEAKER",
                f"【{context.speaker}】不能替【{character_name}】选择升级内容。",
                "等待该角色的操作者确认职业与技能选择。",
            )
        class_name = self._clean(arguments.get("class_name"))
        skill_name = self._clean(arguments.get("skill_name"))
        if not class_name or not skill_name:
            return self._failure(
                "level_up_character",
                "LEVEL_UP_CHOICE_REQUIRED",
                "升级必须同时选择本级投入的职业和职业技能。",
                "询问玩家要提升哪个职业、取得或提升哪个该职业技能。",
            )
        raw_extra_spells = arguments.get("extra_spells") or []
        if not isinstance(raw_extra_spells, list) or not all(
            isinstance(item, str) for item in raw_extra_spells
        ):
            return self._failure(
                "level_up_character",
                "EXTRA_SPELLS_MUST_BE_ARRAY",
                "extra_spells必须是法术名数组。",
                "使用字符串数组；不需要额外法术时省略该字段。",
            )
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                result = app.level_up_character(
                    character_name,
                    class_name=class_name,
                    skill_name=skill_name,
                    attribute_increase=self._clean(
                        arguments.get("attribute_increase")
                    ),
                    hero_skill=self._clean(arguments.get("hero_skill")),
                    status_immunity=arguments.get("status_immunity") or None,
                    extra_spells=[
                        self._clean(item)
                        for item in raw_extra_spells
                        if self._clean(item)
                    ],
                    new_identity=self._clean(arguments.get("new_identity")),
                    new_theme=self._clean(arguments.get("new_theme")),
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "level_up_character",
                "LEVEL_UP_REJECTED",
                str(exc) or "升级选择不符合规则。",
                "根据返回原因补齐或修改职业、技能、属性提升或英雄技能后重试。",
            )
        return GMToolReceipt(
            tool_name="level_up_character",
            ok=True,
            result={
                "level_up": self._primitive(result),
                "saved_path": saved_path,
            },
            state_changed=True,
            public_fallback_reply=(
                f"【{character_name}】升到了{result.level_after}级，"
                f"【{class_name}】获得了【{result.skill_name}】。"
            ),
        )

    def travel_party(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "travel_party")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "travel_party")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        blocking_error = self._blocking_window_error(app, "travel_party")
        if blocking_error is not None:
            return blocking_error
        if app.conflict_manager.state.active:
            return self._failure(
                "travel_party",
                "CONFLICT_ACTIVE",
                "冲突尚未结束，不能直接结算整段旅行。",
                "先结算或结束冲突；不要把撤离意图当成已经抵达目的地。",
            )
        if app.dungeon_manager.state.active:
            return self._failure(
                "travel_party",
                "ACTIVE_DUNGEON_REQUIRES_DUNGEON_TOOL",
                f"地下城【{app.dungeon_manager.state.name}】仍在探索中，不能直接覆盖成旅行场景。",
                (
                    "队伍真正离开后先调用finish_dungeon_exploration，"
                    "再从实际出口位置开始旅行。"
                ),
            )
        origin = self._clean(arguments.get("origin"))
        destination = self._clean(arguments.get("destination"))
        if not origin or not destination or origin == destination:
            return self._failure(
                "travel_party",
                "INVALID_ROUTE_ENDPOINTS",
                "旅行需要不同的明确起点和终点。",
                "读取get_travel_state并根据当前场景与玩家决定重新选择。",
            )
        transport = self._clean(arguments.get("transport")) or "徒步"
        option = TravelManager.TRANSPORT_OPTIONS.get(transport)
        if option is None:
            return self._failure(
                "travel_party",
                "UNKNOWN_TRANSPORT",
                f"没有登记交通方式【{transport}】。",
                "调用get_travel_state，从transport_options中选择。",
            )
        party_names, participant_error = self._travel_participants(
            app,
            arguments.get("participants"),
        )
        if participant_error is not None:
            return participant_error
        party_size = max(1, len(party_names))
        if option.owned:
            if option.passenger_capacity < party_size:
                return self._failure(
                    "travel_party",
                    "TRANSPORT_CAPACITY_EXCEEDED",
                    f"【{transport}】只能搭载约{option.passenger_capacity}人，当前队伍有{party_size}人。",
                    "改用容量足够的交通方式，或先处理分队方案。",
                )
            owned = any(
                change.change_type == PersistentChangeType.TRANSPORT and change.name == option.name
                for change in app.world_state.persistent_changes
            )
            if not owned:
                return self._failure(
                    "travel_party",
                    "TRANSPORT_NOT_OWNED",
                    f"队伍尚未拥有【{transport}】。",
                    "改用徒步或已拥有的交通工具；若要购买，应先结算购买行动。",
                )
            app.travel_manager.register_owned_transport(transport)
        payer = self._clean(arguments.get("payer"))
        if not option.owned and option.price > 0:
            if not payer:
                return self._failure(
                    "travel_party",
                    "TRAVEL_PAYER_REQUIRED",
                    f"雇佣【{transport}】需要指定由哪名角色付款。",
                    "向玩家确认付款者，不能自行花费角色金币。",
                )
            payer_error = self._validate_payer(runtime, context, payer)
            if payer_error is not None:
                return payer_error
            if payer not in party_names:
                return self._failure(
                    "travel_party",
                    "TRAVEL_PAYER_NOT_PRESENT",
                    f"付款者【{payer}】不在这支旅行队伍中。",
                    "选择实际同行且由当前发言者控制的付款角色。",
                )

        raw_distance = arguments.get("explicit_distance")
        explicit_distance = int(raw_distance) if raw_distance is not None else None
        if explicit_distance is not None and explicit_distance <= 0:
            return self._failure(
                "travel_party",
                "INVALID_TRAVEL_DISTANCE",
                "旅行距离必须大于0。",
                "由地图路线推导，或提交正整数徒步旅行日距离。",
            )
        commit_key = self._commit_key(
            "travel",
            context,
            self._clean(arguments.get("evidence")),
            origin,
            destination,
            transport,
            payer,
            *party_names,
        )
        duplicate = self._find_commit(app, "travel_tool_commit", commit_key)
        if duplicate is not None:
            return GMToolReceipt(
                tool_name="travel_party",
                ok=True,
                result=dict(duplicate.payload.get("result") or {}),
                state_changed=False,
                public_fallback_reply="这段旅程已经结算过了，没有重复推进。",
            )

        active = app.travel_manager.active_journey
        if active is not None:
            return self._failure(
                "travel_party",
                "JOURNEY_ALREADY_ACTIVE",
                f"队伍仍在从【{active.origin}】前往【{active.destination}】的途中。",
                "先处理pending_event并调用continue_travel；不要创建第二段并行旅程。",
                result={"active_journey": self._primitive(active)},
            )
        location_error = self._validate_travel_origin(app, origin, party_names)
        if location_error is not None:
            return location_error

        snapshot = None
        try:
            with runtime.transaction_lock:
                snapshot = self._snapshot(app, context.campaign_id)
                step = app.begin_staged_travel(
                    journey_id=commit_key,
                    origin=origin,
                    destination=destination,
                    distance=explicit_distance,
                    default_threat_level=self._clean(arguments.get("default_threat_level")) or TravelThreatLevel.MEDIUM,
                    route_type=self._clean(arguments.get("route_type")) or None,
                    transport=transport,
                    participants=party_names,
                    enforce_owned_transport=option.owned,
                )
                service = None
                if not option.owned and option.price > 0:
                    progress = step.get("progress")
                    completed = step.get("completed_journey")
                    planned_days = int(
                        getattr(progress, "total_days", 0)
                        or getattr(completed, "days", 0)
                    )
                    service = app.interceptor.economy_manager.pay_travel_service(
                        payer,
                        transport,
                        days=planned_days,
                        party_size=party_size,
                    )
                result_payload = self._staged_travel_result(step)
                if service is not None:
                    result_payload["service_transaction"] = self._primitive(service)
                app.world_state.record_memory_event(
                    f"旅行工具提交：{origin} -> {destination}",
                    kind="travel_tool_commit",
                    entities=[origin, destination, *party_names],
                    tags=["travel", "tool_commit"],
                    source="GMAdventureToolService",
                    payload={"commit_key": commit_key, "result": result_payload},
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            if snapshot is not None:
                with runtime.transaction_lock:
                    self._restore(app, snapshot)
            return self._failure(
                "travel_party",
                "TRAVEL_REJECTED",
                str(exc) or "旅行规则未能结算。",
                "读取旅行状态，修正路线、交通、容量或付款条件后重试；不要声称队伍已经抵达。",
            )
        return GMToolReceipt(
            tool_name="travel_party",
            ok=True,
            result={**result_payload, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=self._travel_fallback(result_payload),
        )

    def continue_travel(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "continue_travel"
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
        blocking_error = self._blocking_window_error(app, tool_name)
        if blocking_error is not None:
            return blocking_error
        if app.conflict_manager.state.active:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "途中冲突尚未结束，旅程不能继续。",
                "先完成或正式结束当前冲突，再根据公开结果继续旅行。",
            )
        if app.dungeon_manager.state.active:
            return self._failure(
                tool_name,
                "DUNGEON_ACTIVE",
                f"队伍仍在地下城【{app.dungeon_manager.state.name}】中，旅程不能继续。",
                (
                    "先用finish_dungeon_exploration记录完成、撤退或放弃；"
                    "回到实际出口后，再提交途中发现如何解决并继续旅行。"
                ),
            )
        resolution = self._clean(arguments.get("event_resolution"))
        if not resolution:
            return self._failure(
                tool_name,
                "TRAVEL_EVENT_RESOLUTION_REQUIRED",
                "继续旅行前必须记录途中事件已经如何解决。",
                "根据已经公开成立的结果填写event_resolution；不能填写计划或假设。",
            )
        commit_key = self._commit_key(
            "travel_continue",
            context,
            self._clean(arguments.get("evidence")),
            resolution,
        )
        duplicate = self._find_commit(
            app,
            "travel_continue_tool_commit",
            commit_key,
        )
        if duplicate is not None:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=True,
                result=dict(duplicate.payload.get("result") or {}),
                state_changed=False,
                public_fallback_reply="这次途中事件已经处理过了，没有重复推进旅行。",
            )
        active = app.travel_manager.active_journey
        pending = app.travel_manager.pending_travel_event()
        if active is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_JOURNEY",
                "当前没有进行中的旅程。",
                "调用get_travel_state确认状态；若队伍尚未启程，应调用travel_party。",
            )
        if pending is None:
            return self._failure(
                tool_name,
                "NO_PENDING_TRAVEL_EVENT",
                "当前旅程没有等待处理的危险或发现。",
                "不要跳过正常旅行日或重复提交已经处理的事件。",
                result={"active_journey": self._primitive(active)},
            )

        snapshot = None
        try:
            with runtime.transaction_lock:
                snapshot = self._snapshot(app, context.campaign_id)
                step = app.continue_staged_travel(event_resolution=resolution)
                result_payload = self._staged_travel_result(step)
                app.world_state.record_memory_event(
                    f"旅行事件处理：{active.origin} -> {active.destination}",
                    kind="travel_continue_tool_commit",
                    entities=[active.origin, active.destination, *active.party_names],
                    tags=["travel", "tool_commit", "event_resolution"],
                    source="GMAdventureToolService",
                    payload={"commit_key": commit_key, "result": result_payload},
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            if snapshot is not None:
                with runtime.transaction_lock:
                    self._restore(app, snapshot)
            return self._failure(
                tool_name,
                "TRAVEL_CONTINUE_REJECTED",
                str(exc) or "途中事件处理后未能继续旅行。",
                "保留当前旅程，确认事件已经结算且没有冲突或待决选择后重试。",
            )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={**result_payload, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=self._travel_fallback(result_payload),
        )

    def abort_travel(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        tool_name = "abort_travel"
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
        blocking_error = self._blocking_window_error(app, tool_name)
        if blocking_error is not None:
            return blocking_error
        if app.conflict_manager.state.active:
            return self._failure(
                tool_name,
                "CONFLICT_ACTIVE",
                "途中冲突尚未结束，不能直接把队伍移出现场。",
                "先完成冲突与所有阻塞选择，再按真实结果中止或继续旅行。",
            )
        if app.dungeon_manager.state.active:
            return self._failure(
                tool_name,
                "DUNGEON_ACTIVE",
                "队伍正在途中发现的地下城内，不能跳过地下城出口直接中止旅行。",
                "先用finish_dungeon_exploration记录完成、撤退或放弃，再中止旅程。",
            )
        active = app.travel_manager.active_journey
        if active is None:
            return self._failure(
                tool_name,
                "NO_ACTIVE_JOURNEY",
                "当前没有可以中止的旅程。",
                "调用get_travel_state确认状态；不要重复提交已经结束的旅程。",
            )
        reason = self._clean(arguments.get("reason"))
        end_location = self._clean(arguments.get("end_location"))
        if not reason or not end_location:
            return self._failure(
                tool_name,
                "TRAVEL_ABORT_DETAILS_REQUIRED",
                "中止旅程需要明确已经成立的原因和实际停留位置。",
                "根据玩家决定与公开场景补全reason和end_location。",
            )
        current_message = str(context.metadata.get("current_message") or "")
        current_scene = app.scene_manager.current_scene
        allowed_locations = {
            str(active.origin or "").strip(),
            str(current_scene.location or "").strip() if current_scene else "",
        }
        if (
            end_location not in allowed_locations
            and end_location not in current_message
        ):
            return self._failure(
                tool_name,
                "TRAVEL_ABORT_LOCATION_UNSUPPORTED",
                f"当前信息不足以确认队伍已经停在【{end_location}】。",
                "使用原出发地、当前途中场景位置，或当前消息明确说出的地点。",
            )
        commit_key = self._commit_key(
            "travel_abort",
            context,
            self._clean(arguments.get("evidence")),
            reason,
            end_location,
        )
        duplicate = self._find_commit(
            app,
            "travel_abort_tool_commit",
            commit_key,
        )
        if duplicate is not None:
            return GMToolReceipt(
                tool_name=tool_name,
                ok=True,
                result=dict(duplicate.payload.get("result") or {}),
                state_changed=False,
                public_fallback_reply="这段旅程已经按该决定中止，没有重复移动队伍。",
            )
        snapshot = None
        try:
            with runtime.transaction_lock:
                snapshot = self._snapshot(app, context.campaign_id)
                step = app.abort_staged_travel(
                    reason=reason,
                    end_location=end_location,
                )
                result_payload = self._primitive(step)
                app.world_state.record_memory_event(
                    f"旅程中止：{active.origin} -> {active.destination}",
                    kind="travel_abort_tool_commit",
                    entities=[
                        active.origin,
                        active.destination,
                        end_location,
                        *active.party_names,
                    ],
                    tags=["travel", "interrupted", "tool_commit"],
                    source="GMAdventureToolService",
                    payload={
                        "commit_key": commit_key,
                        "result": result_payload,
                    },
                )
                saved_path = self.host._autosave_campaign(
                    runtime,
                    context.campaign_id,
                )
        except Exception as exc:
            if snapshot is not None:
                with runtime.transaction_lock:
                    self._restore(app, snapshot)
            return self._failure(
                tool_name,
                "TRAVEL_ABORT_REJECTED",
                str(exc) or "当前旅程未能安全中止。",
                "保留原旅程状态，修正地点或先处理冲突后重试。",
            )
        return GMToolReceipt(
            tool_name=tool_name,
            ok=True,
            result={**result_payload, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=reason,
        )

    def award_stage_reward(
        self,
        context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        evidence_error = self._validate_evidence(context, arguments.get("evidence"), "award_stage_reward")
        if evidence_error is not None:
            return evidence_error
        gate_error = self._require_adventure(context, "award_stage_reward")
        if gate_error is not None:
            return gate_error
        runtime = self.host._runtime(context.campaign_id)
        app = runtime.app
        blocking_error = self._blocking_window_error(app, "award_stage_reward")
        if blocking_error is not None:
            return blocking_error
        recipients, recipients_error = self._recipients(app, arguments.get("recipients"))
        if recipients_error is not None:
            return recipients_error
        rare_item = self._clean(arguments.get("rare_item"))
        if rare_item and rare_item not in EconomyManager.RARE_ITEMS and get_equipment_example(rare_item) is None:
            return self._failure(
                "award_stage_reward",
                "UNKNOWN_REWARD_ITEM",
                f"奖励物品【{rare_item}】没有规则条目。",
                "省略rare_item让规则层选择，或先使用规则参考确认已登记装备。",
            )
        source = self._clean(arguments.get("source"))
        if not source:
            return self._failure(
                "award_stage_reward",
                "REWARD_SOURCE_REQUIRED",
                "奖励必须对应一个已经解决的事件。",
                "填写已解决的战斗、委托、阶段或其他明确来源。",
            )
        commit_key = self._commit_key(
            "reward",
            context,
            self._clean(arguments.get("evidence")),
            source,
            *recipients,
        )
        duplicate = self._find_commit(app, "reward_tool_commit", commit_key)
        if duplicate is not None:
            return GMToolReceipt(
                tool_name="award_stage_reward",
                ok=True,
                result=dict(duplicate.payload.get("result") or {}),
                state_changed=False,
                public_fallback_reply="这份奖励已经发放过了，没有重复结算。",
            )
        party_level = max(app.character_manager.get(name).level for name in recipients)
        snapshot = self._snapshot(app, context.campaign_id)
        try:
            with runtime.transaction_lock:
                resolution = app.interceptor.resolve(
                    Action(
                        ActionType.AWARD_REWARD,
                        {
                            "recipients": recipients,
                            "party_level": party_level,
                            "difficulty": self._clean(arguments.get("difficulty")),
                            "rare_item": rare_item,
                        },
                    )
                )
                reward = resolution.payload.get("session_reward")
                if reward is None:
                    raise RuntimeError("奖励结算没有返回SessionReward。")
                result_payload = self._primitive(reward)
                result_payload["source"] = source
                app.world_state.record_memory_event(
                    f"奖励工具提交：{source}",
                    kind="reward_tool_commit",
                    entities=[*recipients, source],
                    tags=["reward", "tool_commit"],
                    source="GMAdventureToolService",
                    payload={"commit_key": commit_key, "result": result_payload},
                )
                saved_path = self.host._autosave_campaign(runtime, context.campaign_id)
        except Exception as exc:
            self._restore(app, snapshot)
            return self._failure(
                "award_stage_reward",
                "REWARD_REJECTED",
                str(exc) or "奖励规则未能结算。",
                "修正获得者、奖励档位或物品后重试；不要声称奖励已经发放。",
            )
        return GMToolReceipt(
            tool_name="award_stage_reward",
            ok=True,
            result={**result_payload, "saved_path": saved_path},
            state_changed=True,
            public_fallback_reply=str(getattr(reward, "summary", "") or "奖励已经结算。"),
        )

    def _validate_payer(
        self,
        runtime: Any,
        context: GMToolExecutionContext,
        payer: str,
    ) -> GMToolReceipt | None:
        if not runtime.app.character_manager.exists(payer) or "pc" not in runtime.app.character_manager.get(payer).traits:
            return self._failure(
                "travel_party",
                "UNKNOWN_TRAVEL_PAYER",
                f"【{payer}】不是可付款的玩家角色。",
                "从当前PC中选择付款者。",
            )
        controls = self.host._player_character_control_map(runtime)
        known_owners = [
            player
            for player, heroes in controls.items()
            if payer in heroes
        ]
        controlled = list(controls.get(context.speaker, []))
        if (
            known_owners
            and context.speaker not in known_owners
        ) or (controlled and payer not in controlled):
            return self._failure(
                "travel_party",
                "PAYER_NOT_CONTROLLED_BY_SPEAKER",
                f"【{context.speaker}】不能替【{payer}】决定花费金币。",
                "等待该角色操作者确认，或选择发言者控制的付款角色。",
            )
        return None

    @classmethod
    def _travel_participants(
        cls,
        app: Any,
        value: object,
    ) -> tuple[list[str], GMToolReceipt | None]:
        current = app.scene_manager.current_scene
        current_names = list(current.participants) if current is not None else []
        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        if value in (None, []):
            participants = [name for name in current_names if name in known_pcs]
            if not participants and current is None:
                participants = sorted(known_pcs)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            participants = list(
                dict.fromkeys(
                    cls._clean(item)
                    for item in value
                    if cls._clean(item)
                )
            )
            if current is not None:
                remote = [name for name in participants if name not in current_names]
                if remote:
                    return [], cls._failure(
                        "travel_party",
                        "TRAVEL_PARTICIPANT_NOT_PRESENT",
                        "以下人物不在当前场景，不能随队出发：" + "、".join(remote),
                        "只提交当前在场人物；先完成真实转场或切换分队。",
                    )
        else:
            return [], cls._failure(
                "travel_party",
                "TRAVEL_PARTICIPANTS_MUST_BE_ARRAY",
                "旅行参与者必须是人物名称数组。",
                "省略participants以使用当前场景中的PC，或提交当前在场人物数组。",
            )
        if not participants or not any(name in known_pcs for name in participants):
            return [], cls._failure(
                "travel_party",
                "TRAVEL_PC_REQUIRED",
                "旅行队伍至少需要一名当前在场的玩家角色。",
                "先建立玩家角色所在场景，再开始旅行。",
            )
        return participants, None

    @classmethod
    def _validate_travel_origin(
        cls,
        app: Any,
        origin: str,
        participants: list[str],
    ) -> GMToolReceipt | None:
        remote = []
        for name in participants:
            location = cls._clean(app.scene_manager.location_of(name))
            if location and not cls._locations_compatible(origin, location):
                remote.append(f"{name}（{location}）")
        if not remote:
            return None
        return cls._failure(
            "travel_party",
            "TRAVEL_ORIGIN_MISMATCH",
            f"以下人物并不在出发地【{origin}】：" + "、".join(remote),
            "使用人物当前所在地点，或先通过真实场景转场让队伍会合。",
        )

    @classmethod
    def _locations_compatible(cls, left: str, right: str) -> bool:
        def normalize(value: str) -> str:
            return "".join(
                char
                for char in cls._clean(value)
                if char not in " \t\r\n，,。；;：:·/\\（）()【】[]"
            )

        normalized_left = normalize(left)
        normalized_right = normalize(right)
        return bool(
            normalized_left
            and normalized_right
            and (
                normalized_left == normalized_right
                or normalized_left in normalized_right
                or normalized_right in normalized_left
            )
        )

    @classmethod
    def _recipients(cls, app: Any, value: object) -> tuple[list[str], GMToolReceipt | None]:
        if value in (None, []):
            current_scene = app.scene_manager.current_scene
            names = [
                name
                for name in list(getattr(current_scene, "participants", []) or [])
                if app.character_manager.exists(name)
                and "pc" in app.character_manager.get(name).traits
            ]
            if not names and app.session_ledger.active:
                names = [
                    name
                    for name in app.session_ledger.participating_pcs
                    if app.character_manager.exists(name)
                    and "pc" in app.character_manager.get(name).traits
                ]
            if not names:
                names = [
                    character.name
                    for character in app.character_manager.all()
                    if "pc" in character.traits
                ]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            names = list(dict.fromkeys(cls._clean(item) for item in value if cls._clean(item)))
        else:
            return [], cls._failure(
                "award_stage_reward",
                "RECIPIENTS_MUST_BE_ARRAY",
                "奖励获得者必须是角色名数组。",
                "省略recipients以选择全部PC，或提交明确的PC名称数组。",
            )
        invalid = [
            name
            for name in names
            if not app.character_manager.exists(name) or "pc" not in app.character_manager.get(name).traits
        ]
        if invalid or not names:
            return [], cls._failure(
                "award_stage_reward",
                "INVALID_REWARD_RECIPIENT",
                "奖励获得者中包含未知或非玩家角色：" + "、".join(invalid or ["无"]),
                "从当前已建档PC中选择至少一名获得者。",
            )
        return names, None

    @staticmethod
    def _find_commit(app: Any, kind: str, commit_key: str):
        for event in reversed(app.world_state.memory_events):
            if event.kind == kind and str(event.payload.get("commit_key") or "") == commit_key:
                return event
        return None

    @staticmethod
    def _commit_key(prefix: str, context: GMToolExecutionContext, *parts: str) -> str:
        message_id = str(context.metadata.get("message_id") or "").strip()
        raw = "\x1f".join(
            [prefix, context.campaign_id, context.session_id, message_id, *parts]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _snapshot(app: Any, campaign_id: str) -> CampaignStateSnapshot:
        return CampaignStateTransaction.capture(app, campaign_id)

    @staticmethod
    def _restore(app: Any, snapshot: CampaignStateSnapshot) -> None:
        CampaignStateTransaction.restore(app, snapshot)

    @staticmethod
    def _travel_fallback(result: dict[str, object]) -> str:
        days = list(result.get("day_results") or [])
        day_lines = [str(day.get("summary") or "") for day in days if isinstance(day, dict)]
        if str(result.get("status") or "") == "event_pending":
            pending = result.get("pending_event")
            pending_summary = (
                str(pending.get("summary") or "")
                if isinstance(pending, dict)
                else ""
            )
            return "\n".join(
                [*day_lines, pending_summary or "途中发生了需要先处理的事件。"]
            ).strip()
        completed = result.get("completed_journey")
        completed_summary = (
            str(completed.get("summary") or "")
            if isinstance(completed, dict)
            else str(result.get("summary") or "")
        )
        return "\n".join([completed_summary, *day_lines]).strip()

    @classmethod
    def _staged_travel_result(cls, step: dict[str, object]) -> dict[str, object]:
        payload = cls._primitive(step)
        completed = payload.get("completed_journey")
        if isinstance(completed, dict):
            # Keep the historical top-level journey fields for dashboards and
            # older clients while exposing the staged lifecycle explicitly.
            for key, value in completed.items():
                payload.setdefault(str(key), value)
            return payload
        progress = payload.get("progress")
        if isinstance(progress, dict):
            payload.setdefault("origin", progress.get("origin"))
            payload.setdefault("destination", progress.get("destination"))
            payload.setdefault("days", progress.get("total_days"))
            payload.setdefault("distance", progress.get("distance"))
            payload.setdefault("transport", progress.get("transport"))
            payload.setdefault("route_type", progress.get("route_type"))
            payload.setdefault("service_cost", progress.get("service_cost"))
        return payload

    @classmethod
    def _validate_evidence(
        cls,
        context: GMToolExecutionContext,
        value: object,
        tool_name: str,
    ) -> GMToolReceipt | None:
        if not is_current_message_evidence(context, value):
            return cls._failure(
                tool_name,
                "EVIDENCE_NOT_LITERAL",
                "evidence不是当前消息中的逐字连续片段。",
                "从current_message复制原句；不得使用路由摘要、改写或补全。",
            )
        return None

    @classmethod
    def _require_adventure(cls, context: GMToolExecutionContext, tool_name: str) -> GMToolReceipt | None:
        if context.gate_status == "adventure":
            return None
        return cls._failure(
            tool_name,
            "ADVENTURE_NOT_ACTIVE",
            "当前还没有进入可结算旅行或奖励的冒险阶段。",
            "先完成会话门控与第零章。",
        )

    @classmethod
    def _blocking_window_error(cls, app: Any, tool_name: str) -> GMToolReceipt | None:
        windows = [window for window in app.interceptor.decision_window_manager.pending() if window.blocking]
        if not windows:
            return None
        return cls._failure(
            tool_name,
            "BLOCKING_DECISION_PENDING",
            "仍有必须先处理的规则选择。",
            "先处理当前DecisionWindow，不能跳过玩家选择。",
            result={"pending_windows": [window.window_id for window in windows]},
        )

    @classmethod
    def _primitive(cls, value):
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
            public_fallback_reply="这一步还没有生效，我需要先确认路线或规则条件。",
        )
