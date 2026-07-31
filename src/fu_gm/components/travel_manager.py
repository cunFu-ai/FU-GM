from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import (
    JourneyProgress,
    JourneyResult,
    TransportationOption,
    TravelDayResult,
    TravelEventTemplate,
    TravelEventType,
    TravelRouteType,
    TravelRouteRecord,
    TravelThreatLevel,
)


@dataclass
class JourneyAdvance:
    day_results: list[TravelDayResult] = field(default_factory=list)
    pending_event: TravelDayResult | None = None
    completed_journey: JourneyResult | None = None


THREAT_DICE = {
    TravelThreatLevel.MINOR: 6,
    TravelThreatLevel.LOW: 8,
    TravelThreatLevel.MEDIUM: 10,
    TravelThreatLevel.HIGH: 12,
    TravelThreatLevel.EXTREME: 20,
}
TRAVEL_DIE_STEPS = (6, 8, 10, 12, 20)


class TravelManager:
    """按旅行日结算威胁骰、危险与发现。"""

    DANGER_TABLE = [
        TravelEventTemplate("极端天气", "沙尘暴、暴雪、暴雨或酷热迫使队伍改变路线。", "可要求团队检定；失败时造成即兴伤害或推进威胁命刻。", ("weather",)),
        TravelEventTemplate("污染地带", "毒雾、瘴气或受损灵魂流笼罩道路。", "可施加中毒或要求消耗库存道具。", ("hazard", "poison")),
        TravelEventTemplate("领地怪物", "凶猛生物把队伍视为入侵者。", "可进入冲突，或用目标命刻安抚/绕开。", ("monster",)),
        TravelEventTemplate("敌对遭遇", "反派手下、巡逻队或追兵突然出现。", "适合触发短冲突或推进反派计划命刻。", ("enemy",)),
        TravelEventTemplate("物资损失", "恶劣路况、盗窃或事故让重要物资受损。", "可损失 IP、Zenit 或一件叙事物品。", ("resource",)),
        TravelEventTemplate("地形事故", "塌桥、山崩、暗流或空中乱流阻断路径。", "适合建立 4-6 格目标命刻。", ("terrain",)),
    ]
    DISCOVERY_TABLE = [
        TravelEventTemplate("古代废墟入口", "队伍发现一处被遗忘的遗迹或地下城入口。", "可创建地下城蓝图或地图标记。", ("dungeon",)),
        TravelEventTemplate("友善商队", "旅行商、巡礼者或移动工坊愿意交换情报和物资。", "可开放商店、补充 IP 或获得委托。", ("shop",)),
        TravelEventTemplate("偏僻村庄", "地图上未标记的小村落出现在地平线尽头。", "可添加新地点和 NPC。", ("location",)),
        TravelEventTemplate("安全避风港", "一处神殿、泉眼、洞穴或浮空平台可供休整。", "可允许休息，或降低下一日威胁等级。", ("rest",)),
        TravelEventTemplate("珍贵材料", "队伍找到适合仪式或项目的稀有素材。", "可作为仪式半价材料或项目材料抵扣。", ("material",)),
        TravelEventTemplate("旧日线索", "壁画、残碑或旅人传闻指向一条长期谜团。", "写入世界记忆并关联对应实体。", ("memory",)),
    ]
    TRANSPORT_OPTIONS = {
        "徒步": TransportationOption("徒步", TravelRouteType.LAND, 0, 0, 1, description="默认旅行方式。"),
        "陆地旅行服务": TransportationOption("陆地旅行服务", TravelRouteType.LAND, 10, 1, 1, description="雇佣陆路交通，每人每日 10Z。"),
        "水面旅行服务": TransportationOption("水面旅行服务", TravelRouteType.WATER, 20, 1, 1, description="雇佣水路交通，每人每日 20Z。"),
        "空中旅行服务": TransportationOption("空中旅行服务", TravelRouteType.AIR, 40, 1, 1, description="雇佣空中交通，每人每日 40Z。"),
        "地面坐骑": TransportationOption("地面坐骑", TravelRouteType.LAND, 200, 2, 2, owned=True),
        "水面坐骑": TransportationOption("水面坐骑", TravelRouteType.WATER, 500, 6, 2, owned=True),
        "水下坐骑": TransportationOption("水下坐骑", TravelRouteType.UNDERWATER, 1000, 6, 2, owned=True),
        "飞行坐骑": TransportationOption("飞行坐骑", TravelRouteType.AIR, 2000, 6, 3, owned=True),
        "地面载具": TransportationOption("地面载具", TravelRouteType.LAND, 600, 6, 2, owned=True),
        "水面载具": TransportationOption("水面载具", TravelRouteType.WATER, 2000, 10, 2, owned=True),
        "水下载具": TransportationOption("水下载具", TravelRouteType.UNDERWATER, 4000, 10, 2, owned=True),
        "飞行载具": TransportationOption("飞行载具", TravelRouteType.AIR, 8000, 20, 3, owned=True),
    }

    def __init__(self, rules_engine: RulesEngine) -> None:
        self.rules_engine = rules_engine
        self.last_journey: JourneyResult | None = None
        self.history: list[JourneyResult] = []
        self.routes: dict[str, TravelRouteRecord] = {}
        self.owned_transports: set[str] = set()
        self.active_journey: JourneyProgress | None = None
        self.interrupted_journeys: list[JourneyProgress] = []

    def begin_journey(
        self,
        *,
        journey_id: str,
        origin: str,
        destination: str,
        threat_levels: list[TravelThreatLevel],
        regions: list[str] | None = None,
        distance: int | None = None,
        default_threat_level: TravelThreatLevel | str = TravelThreatLevel.MEDIUM,
        route_type: TravelRouteType | str = TravelRouteType.LAND,
        transport: str = "徒步",
        party_size: int = 1,
        party_names: list[str] | None = None,
        enforce_owned_transport: bool = False,
        threat_die_step_reduction: int = 0,
        discovery_threshold: int = 1,
    ) -> JourneyProgress:
        if self.active_journey is not None and self.active_journey.status in {
            "traveling",
            "event_pending",
        }:
            raise ValueError(
                f"队伍仍在从{self.active_journey.origin}前往"
                f"{self.active_journey.destination}的旅途中。"
            )
        option = self.transport_option(transport)
        if (
            enforce_owned_transport
            and option.owned
            and not self.has_owned_transport(transport)
        ):
            raise ValueError(f"队伍尚未拥有【{transport}】，不能免费使用该交通工具。")
        normalized_route_type = TravelRouteType(route_type)
        self.validate_transport_route(transport, normalized_route_type)
        normalized_threats = [
            TravelThreatLevel(level) for level in threat_levels
        ]
        if not normalized_threats:
            raise ValueError("旅行至少需要 1 个旅行日的威胁等级。")
        normalized_regions = [
            self._region_for_day(regions, day, destination)
            for day in range(1, len(normalized_threats) + 1)
        ]
        progress = JourneyProgress(
            journey_id=str(journey_id or "").strip(),
            origin=origin,
            destination=destination,
            total_days=len(normalized_threats),
            threat_levels=normalized_threats,
            regions=normalized_regions,
            route_type=normalized_route_type,
            distance=distance or len(normalized_threats),
            transport=transport,
            travel_multiplier=option.travel_multiplier,
            service_cost=self.service_cost(
                transport,
                len(normalized_threats),
                party_size,
            ),
            party_size=max(1, int(party_size or 1)),
            party_names=list(dict.fromkeys(party_names or [])),
            default_threat_level=TravelThreatLevel(default_threat_level),
            threat_die_step_reduction=max(0, int(threat_die_step_reduction or 0)),
            discovery_threshold=max(1, int(discovery_threshold or 1)),
            summary=f"队伍从{origin}前往{destination}。",
        )
        self.active_journey = progress
        return progress

    def advance_active_journey(self) -> JourneyAdvance:
        progress = self._require_active_journey()
        if progress.status == "event_pending":
            pending = self.pending_travel_event()
            raise ValueError(
                "当前旅行事件尚未处理："
                + (pending.summary if pending is not None else "未知事件")
            )

        advanced: list[TravelDayResult] = []
        while progress.current_day < progress.total_days:
            index = progress.current_day + 1
            day = self.resolve_travel_day(
                index,
                progress.regions[index - 1],
                progress.threat_levels[index - 1],
                discovery_threshold=progress.discovery_threshold,
                threat_die_step_reduction=progress.threat_die_step_reduction,
            )
            progress.current_day = index
            progress.day_results.append(day)
            advanced.append(day)
            if day.event_type != TravelEventType.QUIET:
                progress.pending_event_day = day.day
                progress.status = "event_pending"
                return JourneyAdvance(
                    day_results=advanced,
                    pending_event=day,
                )

        completed = self._complete_active_journey()
        return JourneyAdvance(
            day_results=advanced,
            completed_journey=completed,
        )

    def resolve_pending_travel_event(self, resolution: str) -> TravelDayResult:
        progress = self._require_active_journey()
        pending = self.pending_travel_event()
        if progress.status != "event_pending" or pending is None:
            raise ValueError("当前旅程没有等待处理的旅行事件。")
        note = str(resolution or "").strip()
        if not note:
            raise ValueError("继续旅行前需要记录这次危险或发现如何被处理。")
        progress.event_resolution_notes.append(
            f"第{pending.day}日【{pending.event_detail or pending.event_type.value}】：{note}"
        )
        progress.pending_event_day = 0
        progress.status = "traveling"
        return pending

    def pending_travel_event(self) -> TravelDayResult | None:
        progress = self.active_journey
        if progress is None or progress.pending_event_day <= 0:
            return None
        for day in progress.day_results:
            if day.day == progress.pending_event_day:
                return day
        return None

    def cancel_active_journey(
        self,
        *,
        reason: str,
        end_location: str,
    ) -> JourneyProgress:
        progress = self._require_active_journey()
        clean_reason = str(reason or "").strip()
        clean_location = str(end_location or "").strip()
        if not clean_reason:
            raise ValueError("中止旅程需要记录已经发生的原因。")
        if not clean_location:
            raise ValueError("中止旅程需要明确队伍实际停留的位置。")
        pending = self.pending_travel_event()
        if pending is not None:
            progress.event_resolution_notes.append(
                f"第{pending.day}日【{pending.event_detail or pending.event_type.value}】："
                f"队伍没有继续行程；{clean_reason}"
            )
        progress.pending_event_day = 0
        progress.status = "interrupted"
        progress.interruption_reason = clean_reason
        progress.end_location = clean_location
        progress.summary = (
            f"队伍从{progress.origin}前往{progress.destination}的旅程在"
            f"{clean_location}中止：{clean_reason}"
        )
        self.interrupted_journeys.append(progress)
        self.active_journey = None
        return progress

    def _require_active_journey(self) -> JourneyProgress:
        if self.active_journey is None or self.active_journey.status not in {
            "traveling",
            "event_pending",
        }:
            raise ValueError("当前没有进行中的旅程。")
        return self.active_journey

    def _complete_active_journey(self) -> JourneyResult:
        progress = self._require_active_journey()
        if progress.pending_event_day:
            raise ValueError("旅行事件尚未处理，不能抵达目的地。")
        if progress.current_day < progress.total_days:
            raise ValueError("旅行日尚未全部结算。")
        result = JourneyResult(
            origin=progress.origin,
            destination=progress.destination,
            days=progress.total_days,
            day_results=list(progress.day_results),
            route_type=progress.route_type,
            distance=progress.distance,
            transport=progress.transport,
            travel_multiplier=progress.travel_multiplier,
            service_cost=progress.service_cost,
            summary=(
                f"队伍从{progress.origin}抵达{progress.destination}，"
                f"路线 {progress.route_type.value}，交通：{progress.transport}，"
                f"用时 {progress.total_days} 个旅行日。"
            ),
        )
        progress.status = "completed"
        progress.summary = result.summary
        self.last_journey = result
        self.history.append(result)
        self.routes[
            self.route_key(result.origin, result.destination)
        ] = self._record_route(
            result,
            default_threat_level=progress.default_threat_level,
            regions=progress.regions,
        )
        self.active_journey = None
        return result

    def travel(
        self,
        *,
        origin: str,
        destination: str,
        threat_levels: list[TravelThreatLevel] | None = None,
        regions: list[str] | None = None,
        distance: int | None = None,
        default_threat_level: TravelThreatLevel | str = TravelThreatLevel.MEDIUM,
        route_type: TravelRouteType | str = TravelRouteType.LAND,
        transport: str = "徒步",
        party_size: int = 1,
        enforce_owned_transport: bool = False,
        event_tables_by_region: dict[str, dict[str, list[TravelEventTemplate]]] | None = None,
        discovery_threshold: int = 1,
        threat_die_step_reduction: int = 0,
    ) -> JourneyResult:
        route_type = TravelRouteType(route_type)
        option = self.transport_option(transport)
        self.validate_transport_route(transport, route_type)
        if enforce_owned_transport and option.owned and not self.has_owned_transport(transport):
            raise ValueError(f"队伍尚未拥有【{transport}】，不能免费使用该交通工具。")
        travel_multiplier = option.travel_multiplier
        if distance is not None and threat_levels is None:
            days = self.calculate_travel_days(distance, transport=transport)
            threat_levels = [TravelThreatLevel(default_threat_level)] * days
        if not threat_levels:
            raise ValueError("旅行至少需要 1 个旅行日的威胁等级。")

        day_results = []
        for index, threat_level in enumerate(threat_levels, start=1):
            threat_level = TravelThreatLevel(threat_level)
            region = self._region_for_day(regions, index, destination)
            region_tables = (event_tables_by_region or {}).get(region, {})
            day_results.append(
                self.resolve_travel_day(
                    index,
                    region,
                    threat_level,
                    danger_table=region_tables.get("danger"),
                    discovery_table=region_tables.get("discovery"),
                    discovery_threshold=discovery_threshold,
                    threat_die_step_reduction=threat_die_step_reduction,
                )
            )

        service_cost = self.service_cost(transport, len(threat_levels), party_size)
        result = JourneyResult(
            origin=origin,
            destination=destination,
            days=len(threat_levels),
            day_results=day_results,
            route_type=route_type,
            distance=distance or len(threat_levels),
            transport=transport,
            travel_multiplier=travel_multiplier,
            service_cost=service_cost,
            summary=(
                f"队伍从{origin}前往{destination}，路线 {route_type.value}，"
                f"交通：{transport}，用时 {len(threat_levels)} 个旅行日。"
            ),
        )
        self.last_journey = result
        self.history.append(result)
        self.routes[self.route_key(origin, destination)] = self._record_route(
            result,
            default_threat_level=TravelThreatLevel(default_threat_level),
            regions=regions or [],
        )
        return result

    def resolve_travel_day(
        self,
        day: int,
        region: str,
        threat_level: TravelThreatLevel | str,
        *,
        danger_table: list[TravelEventTemplate] | None = None,
        discovery_table: list[TravelEventTemplate] | None = None,
        discovery_threshold: int = 1,
        threat_die_step_reduction: int = 0,
    ) -> TravelDayResult:
        threat_level = TravelThreatLevel(threat_level)
        die_size = self._reduce_travel_die(
            THREAT_DICE[threat_level],
            threat_die_step_reduction,
        )
        roll = self.rules_engine.roll_die(die_size)
        event_detail = ""
        mechanical_hint = ""
        danger_tags: list[str] = []
        discovered_location = ""
        if roll <= max(1, int(discovery_threshold)):
            event_type = TravelEventType.DISCOVERY
            template = self._pick_template(discovery_table or self.DISCOVERY_TABLE, day=day, roll=roll)
            event_detail = f"{template.name}：{template.description}"
            mechanical_hint = template.mechanical_hint
            danger_tags = list(template.tags)
            if "location" in template.tags or "dungeon" in template.tags:
                discovered_location = f"{region}的{template.name}"
            summary = f"第 {day} 个旅行日，队伍在{region}发现【{template.name}】。"
        elif roll >= 6:
            event_type = TravelEventType.DANGER
            template = self._pick_template(danger_table or self.DANGER_TABLE, day=day, roll=roll)
            event_detail = f"{template.name}：{template.description}"
            mechanical_hint = template.mechanical_hint
            danger_tags = list(template.tags)
            summary = f"第 {day} 个旅行日，队伍在{region}遭遇【{template.name}】。"
        else:
            event_type = TravelEventType.QUIET
            summary = f"第 {day} 个旅行日，队伍穿越{region}，没有遭遇重大事件。"
        hard_rule_summary = self._travel_hard_rule_summary(
            day=day,
            region=region,
            threat_level=threat_level,
            die_size=die_size,
            roll=roll,
            event_type=event_type,
            event_detail=event_detail,
            mechanical_hint=mechanical_hint,
            discovered_location=discovered_location,
        )
        llm_narrative_prompt = self._travel_llm_prompt(
            day=day,
            region=region,
            threat_level=threat_level,
            event_type=event_type,
            event_detail=event_detail,
            mechanical_hint=mechanical_hint,
            danger_tags=danger_tags,
            discovered_location=discovered_location,
        )
        return TravelDayResult(
            day=day,
            region=region,
            threat_level=threat_level,
            die_size=die_size,
            roll=roll,
            event_type=event_type,
            summary=summary,
            event_detail=event_detail,
            mechanical_hint=mechanical_hint,
            discovered_location=discovered_location,
            danger_tags=danger_tags,
            hard_rule_summary=hard_rule_summary,
            llm_narrative_prompt=llm_narrative_prompt,
        )

    @staticmethod
    def _reduce_travel_die(die_size: int, steps: int) -> int:
        index = TRAVEL_DIE_STEPS.index(die_size)
        return TRAVEL_DIE_STEPS[max(0, index - max(0, int(steps or 0)))]

    def calculate_travel_days(self, distance: int, *, transport: str = "徒步") -> int:
        if distance <= 0:
            return 0
        option = self.transport_option(transport)
        return max(1, ceil(distance / max(1, option.travel_multiplier)))

    def service_cost(self, transport: str, days: int, party_size: int = 1) -> int:
        option = self.transport_option(transport)
        if option.owned:
            return 0
        return max(0, option.price * max(1, party_size) * max(0, days))

    def transport_option(self, name: str) -> TransportationOption:
        if name not in self.TRANSPORT_OPTIONS:
            raise ValueError(f"未知交通方式：{name}")
        return self.TRANSPORT_OPTIONS[name]

    @classmethod
    def validate_transport_route(
        cls,
        transport: str,
        route_type: TravelRouteType | str,
    ) -> TravelRouteType:
        option = cls.TRANSPORT_OPTIONS.get(transport)
        if option is None:
            raise ValueError(f"未知交通方式：{transport}")
        route = TravelRouteType(route_type)
        allowed_routes = {
            TravelRouteType.LAND: {TravelRouteType.LAND},
            TravelRouteType.WATER: {TravelRouteType.WATER},
            TravelRouteType.UNDERWATER: {
                TravelRouteType.WATER,
                TravelRouteType.UNDERWATER,
            },
            TravelRouteType.AIR: {
                TravelRouteType.LAND,
                TravelRouteType.WATER,
                TravelRouteType.AIR,
            },
        }[option.route_type]
        if route not in allowed_routes:
            raise ValueError(
                f"交通方式【{transport}】不能用于{route.value}路线。"
            )
        return route

    def register_owned_transport(self, name: str) -> TransportationOption:
        option = self.transport_option(name)
        if not option.owned:
            raise ValueError(f"【{name}】不是可登记为长期资产的交通工具。")
        self.owned_transports.add(option.name)
        return option

    def has_owned_transport(self, name: str) -> bool:
        option = self.transport_option(name)
        return option.name in self.owned_transports

    def map_distance_days(
        self,
        *,
        hexes: int = 0,
        explicit_days: int = 0,
        transport: str = "徒步",
    ) -> int:
        distance = explicit_days or hexes
        return self.calculate_travel_days(distance, transport=transport)

    def route_key(self, origin: str, destination: str) -> str:
        return f"{origin}->{destination}"

    def known_route(self, origin: str, destination: str) -> TravelRouteRecord | None:
        return self.routes.get(self.route_key(origin, destination))

    def route_summary(self, origin: str, destination: str) -> str:
        route = self.known_route(origin, destination)
        if route is None:
            return f"尚未记录从{origin}到{destination}的路线。"
        discoveries = "、".join(route.discoveries) if route.discoveries else "无"
        dangers = "、".join(route.dangers) if route.dangers else "无"
        return (
            f"{origin}->{destination}：{route.route_type.value}路线，距离 {route.distance} 个旅行日单位，"
            f"交通 {route.transport}，预计 {route.travel_days} 日。发现：{discoveries}。危险：{dangers}。"
        )

    def _region_for_day(self, regions: list[str] | None, day: int, destination: str) -> str:
        if not regions:
            return destination
        if day - 1 < len(regions):
            return regions[day - 1]
        return regions[-1]

    def _pick_template(self, table: list[TravelEventTemplate], *, day: int, roll: int) -> TravelEventTemplate:
        # 不额外掷骰，避免旅行事件消耗下一天的威胁骰。
        index = (day + roll - 2) % len(table)
        return table[index]

    def _travel_hard_rule_summary(
        self,
        *,
        day: int,
        region: str,
        threat_level: TravelThreatLevel,
        die_size: int,
        roll: int,
        event_type: TravelEventType,
        event_detail: str,
        mechanical_hint: str,
        discovered_location: str,
    ) -> str:
        parts = [
            f"第 {day} 个旅行日硬结算：威胁等级 {threat_level.value}，威胁骰 d{die_size}={roll}，事件类型 {event_type.value}。",
        ]
        if event_detail:
            parts.append(f"事件种子：{event_detail}")
        if mechanical_hint:
            parts.append(f"可用机制边界：{mechanical_hint}")
        if discovered_location:
            parts.append(f"可登记发现地点：{discovered_location}")
        parts.append("不要在叙事中自行改动骰值、旅行天数、奖励、伤害、状态或命刻。")
        return " ".join(parts)

    def _travel_llm_prompt(
        self,
        *,
        day: int,
        region: str,
        threat_level: TravelThreatLevel,
        event_type: TravelEventType,
        event_detail: str,
        mechanical_hint: str,
        danger_tags: list[str],
        discovered_location: str,
    ) -> str:
        if event_type == TravelEventType.QUIET:
            creative_scope = "可以创作一段短旅行蒙太奇、角色互动、远景伏笔或风土细节，但不要添加需要硬结算的危险或奖励。"
        elif event_type == TravelEventType.DISCOVERY:
            creative_scope = (
                "可以把发现包装成符合当前世界观的地点、NPC、线索、素材或休整机会；"
                "若要真正发放金币、装备、IP、长期设施或新地下城，请后续使用对应硬规则 Action。"
            )
        else:
            creative_scope = (
                "可以设计具体危险来源、敌人意图、环境压力和可选择的解决路径；"
                "若危险会造成伤害、异常状态、资源损失、命刻推进或冲突，请后续使用对应硬规则 Action。"
            )
        seed = event_detail or "无重大事件"
        hint = f"机制提示：{mechanical_hint}" if mechanical_hint else "机制提示：无。"
        tags = f"标签：{', '.join(danger_tags)}。" if danger_tags else "标签：无。"
        discovery = f"发现地点候选：{discovered_location}。" if discovered_location else ""
        return (
            f"请 GM LLM 根据旅行日硬结果创作叙事。第 {day} 日，地区：{region}，"
            f"威胁等级：{threat_level.value}，事件类型：{event_type.value}，种子：{seed}。"
            f"{hint}{tags}{discovery}{creative_scope}"
        )

    def _record_route(
        self,
        result: JourneyResult,
        *,
        default_threat_level: TravelThreatLevel,
        regions: list[str],
    ) -> TravelRouteRecord:
        discoveries = [
            day.discovered_location or day.event_detail
            for day in result.day_results
            if day.event_type == TravelEventType.DISCOVERY
        ]
        dangers = [
            day.event_detail
            for day in result.day_results
            if day.event_type == TravelEventType.DANGER
        ]
        return TravelRouteRecord(
            origin=result.origin,
            destination=result.destination,
            route_type=result.route_type,
            distance=result.distance,
            transport=result.transport,
            travel_days=result.days,
            default_threat_level=default_threat_level,
            regions=list(regions),
            discoveries=discoveries,
            dangers=dangers,
            notes=[result.summary],
        )
