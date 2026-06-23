from __future__ import annotations

from math import ceil, sqrt

from fu_gm.components.adventure_event_manager import AdventureEventManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    JourneyResult,
    MapLocation,
    MapRouteEdge,
    MapRouteSegment,
    TravelDayResult,
    TravelEventType,
    TravelRouteType,
    TravelThreatLevel,
    WorldRoutePlan,
)


class WorldMapManager:
    """管理世界地图地点与机读路线网络。

    地点坐标只用于仪表盘/地图卡展示布局；旅行日、威胁等级和路线选择优先来自
    WorldState.map_routes 中的 Graph 边，避免视觉模型或坐标推断影响硬规则。
    """

    TERRAIN_THREAT = {
        "村庄": TravelThreatLevel.MINOR,
        "城市": TravelThreatLevel.MINOR,
        "城镇": TravelThreatLevel.MINOR,
        "警戒地区": TravelThreatLevel.MINOR,
        "草原": TravelThreatLevel.LOW,
        "乡村": TravelThreatLevel.LOW,
        "巡逻道路": TravelThreatLevel.LOW,
        "森林": TravelThreatLevel.MEDIUM,
        "山坡": TravelThreatLevel.MEDIUM,
        "河流": TravelThreatLevel.MEDIUM,
        "高山": TravelThreatLevel.HIGH,
        "沼泽": TravelThreatLevel.HIGH,
        "大海": TravelThreatLevel.HIGH,
        "广袤森林": TravelThreatLevel.HIGH,
        "沙漠": TravelThreatLevel.EXTREME,
        "冰冻荒地": TravelThreatLevel.EXTREME,
        "丛林": TravelThreatLevel.EXTREME,
        "火山": TravelThreatLevel.EXTREME,
    }
    TERRAIN_ROUTE_TYPE = {
        "大海": TravelRouteType.WATER,
        "河流": TravelRouteType.WATER,
        "水下": TravelRouteType.UNDERWATER,
        "天空": TravelRouteType.AIR,
        "云海": TravelRouteType.AIR,
    }
    THREAT_ORDER = [
        TravelThreatLevel.MINOR,
        TravelThreatLevel.LOW,
        TravelThreatLevel.MEDIUM,
        TravelThreatLevel.HIGH,
        TravelThreatLevel.EXTREME,
    ]

    def __init__(self, world_state: WorldState, adventure_event_manager: AdventureEventManager | None = None) -> None:
        self.world_state = world_state
        self.adventure_event_manager = adventure_event_manager or AdventureEventManager(world_state)
        self.route_plans: list[WorldRoutePlan] = []
        self.sync_from_world_state()

    def sync_from_world_state(self) -> None:
        """把 Session 0/世界表里的关键地点补登记到地图坐标表。"""

        for name, description in self.world_state.map_notes.items():
            if name not in self.world_state.map_locations:
                x, y = self._next_auto_coordinate()
                self.add_location(name, x=x, y=y, description=description)
        if self.world_state.world_profile.major_locations:
            for name, description in self.world_state.world_profile.major_locations.items():
                if name not in self.world_state.map_locations:
                    x, y = self._next_auto_coordinate()
                    self.add_location(name, x=x, y=y, description=description)
        if self.world_state.world_sheet is not None:
            for name, description in self.world_state.world_sheet.major_locations.items():
                if name not in self.world_state.map_locations:
                    x, y = self._next_auto_coordinate()
                    self.add_location(name, x=x, y=y, description=description)

    def add_location(
        self,
        name: str,
        *,
        x: int | None = None,
        y: int | None = None,
        description: str = "",
        terrain: str = "",
        feature_type: str = "",
        position_hint: str = "",
        relative_to: str = "",
        relative_position: str = "",
        draw_icon: bool | None = None,
        icon_id: str = "",
        threat_level: TravelThreatLevel | str | None = None,
        route_type: TravelRouteType | str | None = None,
        faction: str = "",
        discovered: bool = True,
        tags: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> MapLocation:
        if x is None or y is None:
            auto_x, auto_y = self._next_auto_coordinate()
            x = auto_x if x is None else x
            y = auto_y if y is None else y
        terrain = terrain or self._terrain_for_feature(feature_type)
        threat_level = TravelThreatLevel(threat_level) if threat_level else self._threat_for_terrain(terrain)
        route_type = TravelRouteType(route_type) if route_type else self._route_type_for_terrain(terrain)
        location = self.world_state.upsert_map_location(
            name,
            x=x,
            y=y,
            description=description,
            terrain=terrain,
            feature_type=feature_type,
            position_hint=position_hint,
            relative_to=relative_to,
            relative_position=relative_position,
            draw_icon=draw_icon,
            icon_id=icon_id,
            threat_level=threat_level,
            route_type=route_type,
            faction=faction,
            discovered=discovered,
            tags=tags,
            notes=notes,
        )
        self.world_state.record_memory_event(
            f"地图地点登记：{self.world_state.format_map_location(location)}",
            kind="map_location",
            entities=[name, faction] if faction else [name],
            tags=["map", *(tags or [])],
            source="WorldMapManager",
        )
        return location

    def add_route(
        self,
        origin: str,
        destination: str,
        *,
        route_id: str = "",
        distance_days: int | None = None,
        default_threat_level: TravelThreatLevel | str = TravelThreatLevel.MEDIUM,
        route_type: TravelRouteType | str = TravelRouteType.LAND,
        terrain: str = "",
        description: str = "",
        bidirectional: bool = True,
        discovered: bool = True,
        segments: list[MapRouteSegment | dict] | None = None,
        tags: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> MapRouteEdge:
        return self.world_state.upsert_map_route(
            origin=origin,
            destination=destination,
            route_id=route_id,
            distance_days=distance_days,
            default_threat_level=default_threat_level,
            route_type=route_type,
            terrain=terrain,
            description=description,
            bidirectional=bidirectional,
            discovered=discovered,
            segments=segments,
            tags=tags,
            notes=notes,
        )

    def plan_route(
        self,
        origin: str,
        destination: str,
        *,
        transport: str = "徒步",
        party_size: int = 1,
        waypoints: list[str] | None = None,
        route_type: TravelRouteType | str | None = None,
        explicit_distance: int | None = None,
        default_threat_level: TravelThreatLevel | str | None = None,
        allow_undiscovered: bool = False,
        route_id: str = "",
        allow_coordinate_fallback: bool = False,
    ) -> WorldRoutePlan:
        self.sync_from_world_state()
        path_names = [origin, *(waypoints or []), destination]
        locations = [self._require_location(name) for name in path_names]
        if not allow_undiscovered:
            hidden = [location.name for location in locations if not location.discovered]
            if hidden:
                raise ValueError(f"地图地点尚未被发现：{'、'.join(hidden)}。")

        option = TravelManager.TRANSPORT_OPTIONS.get(transport)
        if option is None:
            raise ValueError(f"未知交通方式：{transport}")
        route_edges = self._route_edges_for_path(path_names, route_id=route_id)
        route_source = "graph" if route_edges else "explicit"
        route_edge_ids = [edge.route_id for edge in route_edges]
        if route_edges:
            units = self._route_units(route_edges)
            distance = sum(unit["distance_days"] for unit in units)
            route_type = TravelRouteType(route_type) if route_type else self._route_type_for_edges(route_edges, option.route_type)
            threat_levels, regions = self._daily_threats_from_units(units, option.travel_multiplier)
            travel_days = len(threat_levels)
        elif explicit_distance is not None:
            distance = explicit_distance
            travel_days = max(1, ceil(max(1, distance) / max(1, option.travel_multiplier)))
            route_type = TravelRouteType(route_type) if route_type else self._route_type_for_path(locations, option.route_type)
            threat_levels = self._threat_levels_for_path(
                locations,
                travel_days,
                default_threat_level=TravelThreatLevel(default_threat_level) if default_threat_level else None,
            )
            regions = self._regions_for_path(locations, travel_days)
        elif allow_coordinate_fallback:
            route_source = "legacy_coordinate"
            distance = self._path_distance(locations)
            travel_days = max(1, ceil(max(1, distance) / max(1, option.travel_multiplier)))
            route_type = TravelRouteType(route_type) if route_type else self._route_type_for_path(locations, option.route_type)
            threat_levels = self._threat_levels_for_path(
                locations,
                travel_days,
                default_threat_level=TravelThreatLevel(default_threat_level) if default_threat_level else None,
            )
            regions = self._regions_for_path(locations, travel_days)
        else:
            raise ValueError(
                f"地图路线网络中尚未登记从{origin}到{destination}的路线。"
                "请先添加 map route，或由 GM 明确提供 explicit_distance。"
            )
        event_tables_by_region = self._event_tables_for_regions(regions)
        service_cost = 0 if option.owned else option.price * max(1, party_size) * travel_days
        memory_hooks = self.world_state.retrieve_relevant_memory(
            f"从{origin}前往{destination}",
            include_private=False,
            extra_entities=path_names,
            limit=6,
        )
        plan = WorldRoutePlan(
            origin=origin,
            destination=destination,
            distance=distance,
            travel_days=travel_days,
            route_type=route_type,
            transport=transport,
            travel_multiplier=option.travel_multiplier,
            service_cost=service_cost,
            threat_levels=threat_levels,
            regions=regions,
            event_tables_by_region=event_tables_by_region,
            waypoints=list(waypoints or []),
            memory_hooks=memory_hooks,
            route_source=route_source,
            route_edge_ids=route_edge_ids,
            summary=(
                f"路线规划：{origin} -> {destination}，距离 {distance} 个徒步旅行日单位，"
                f"交通 {transport} x{option.travel_multiplier}，预计 {travel_days} 日，"
                f"主要威胁 {self._threat_label(self._max_threat(threat_levels))}，来源：{route_source}。"
            ),
        )
        self.route_plans.append(plan)
        self.world_state.record_memory_event(
            plan.summary,
            kind="route_plan",
            entities=path_names,
            tags=["map", "route", route_type.value],
            source="WorldMapManager",
            payload={
                "distance": distance,
                "travel_days": travel_days,
                "transport": transport,
                "route_source": route_source,
                "route_edge_ids": route_edge_ids,
            },
        )
        return plan

    def enrich_dungeon_state(self, state):
        return self.adventure_event_manager.enrich_dungeon_state(state)

    def record_journey(self, journey: JourneyResult, plan: WorldRoutePlan | None = None) -> None:
        entities = [journey.origin, journey.destination]
        if plan:
            entities.extend(plan.waypoints)
        self.world_state.record_memory_event(
            journey.summary,
            kind="journey",
            entities=entities,
            tags=["map", "travel", journey.route_type.value],
            source="WorldMapManager",
            payload={"distance": journey.distance, "days": journey.days, "transport": journey.transport},
        )

    def discover_from_travel_day(self, day: TravelDayResult) -> MapLocation | None:
        if day.event_type != TravelEventType.DISCOVERY or not day.discovered_location:
            return None
        region = self.world_state.map_locations.get(day.region)
        x, y = self._nearby_coordinate(region)
        terrain = region.terrain if region is not None else "草原"
        return self.world_state.discover_map_location(
            day.discovered_location,
            x=x,
            y=y,
            description=day.event_detail or day.summary,
            terrain=terrain,
            threat_level=day.threat_level,
            route_type=self._route_type_for_terrain(terrain),
            source="旅行发现",
            tags=["travel_discovery", *day.danger_tags],
        )

    def route_summary(self, plan: WorldRoutePlan) -> str:
        levels = "、".join(self._threat_label(level) for level in plan.threat_levels)
        hooks = "；".join(plan.memory_hooks) if plan.memory_hooks else "无相关旧记忆"
        return (
            f"{plan.summary}\n"
            f"每日区域：{'、'.join(plan.regions)}。\n"
            f"每日威胁：{levels}。\n"
            f"相关记忆：{hooks}。"
        )

    def format_map_status(self, *, discovered_only: bool = True) -> str:
        locations = [
            location
            for location in self.world_state.map_locations.values()
            if location.discovered or not discovered_only
        ]
        if not locations:
            return "世界地图暂未登记地点。"
        lines = ["世界地图："]
        for location in sorted(locations, key=lambda item: (item.y, item.x, item.name)):
            lines.append(f"- {self.world_state.format_map_location(location)}")
        routes = [route for route in self.world_state.map_routes.values() if route.discovered or not discovered_only]
        if routes:
            lines.append("机读路线网络：")
            for route in sorted(routes, key=lambda item: item.route_id):
                lines.append(f"- {self.world_state.format_map_route(route)}")
        return "\n".join(lines)

    def _require_location(self, name: str) -> MapLocation:
        if name not in self.world_state.map_locations:
            if name in self.world_state.map_notes:
                x, y = self._next_auto_coordinate()
                return self.add_location(name, x=x, y=y, description=self.world_state.map_notes[name])
            raise ValueError(f"地图中尚未登记地点：{name}")
        return self.world_state.map_locations[name]

    def _path_distance(self, locations: list[MapLocation]) -> int:
        total = 0
        for left, right in zip(locations, locations[1:]):
            total += self._distance(left, right)
        return max(1, total)

    def _distance(self, left: MapLocation, right: MapLocation) -> int:
        dx = right.x - left.x
        dy = right.y - left.y
        return max(1, ceil(sqrt(dx * dx + dy * dy)))

    def _route_edges_for_path(self, path_names: list[str], *, route_id: str = "") -> list[MapRouteEdge]:
        edges: list[MapRouteEdge] = []
        for index, (left, right) in enumerate(zip(path_names, path_names[1:])):
            edge = self.world_state.find_map_route(
                left,
                right,
                route_id=route_id if route_id and len(path_names) == 2 and index == 0 else "",
            )
            if edge is None:
                return []
            if not edge.discovered:
                return []
            edges.append(edge)
        return edges

    def _route_units(self, edges: list[MapRouteEdge]) -> list[dict]:
        units: list[dict] = []
        for edge in edges:
            segments = edge.segments or [
                MapRouteSegment(
                    region=edge.destination,
                    distance_days=edge.distance_days,
                    threat_level=edge.default_threat_level,
                    terrain=edge.terrain,
                    description=edge.description,
                )
            ]
            for segment in segments:
                days = max(1, int(segment.distance_days))
                units.append(
                    {
                        "region": segment.region or edge.destination,
                        "distance_days": days,
                        "threat_level": TravelThreatLevel(segment.threat_level),
                    }
                )
        return units

    def _daily_threats_from_units(self, units: list[dict], travel_multiplier: int) -> tuple[list[TravelThreatLevel], list[str]]:
        expanded: list[dict] = []
        for unit in units:
            for _ in range(max(1, int(unit["distance_days"]))):
                expanded.append({"region": unit["region"], "threat_level": unit["threat_level"]})
        if not expanded:
            expanded.append({"region": "未命名路线", "threat_level": TravelThreatLevel.MEDIUM})
        stride = max(1, travel_multiplier)
        threat_levels: list[TravelThreatLevel] = []
        regions: list[str] = []
        for start in range(0, len(expanded), stride):
            chunk = expanded[start : start + stride]
            threat_levels.append(self._max_threat([item["threat_level"] for item in chunk]))
            regions.append(self._region_summary([item["region"] for item in chunk]))
        return threat_levels, regions

    def _region_summary(self, regions: list[str]) -> str:
        deduped: list[str] = []
        for region in regions:
            if region and region not in deduped:
                deduped.append(region)
        return "、".join(deduped) if deduped else "未命名路线"

    def _regions_for_path(self, locations: list[MapLocation], travel_days: int) -> list[str]:
        destinations = locations[1:] or locations
        regions: list[str] = []
        for index in range(travel_days):
            location = destinations[min(len(destinations) - 1, index * len(destinations) // travel_days)]
            regions.append(location.name)
        return regions

    def _event_tables_for_regions(self, regions: list[str]) -> dict[str, dict]:
        tables: dict[str, dict] = {}
        for region in set(regions):
            tables[region] = self.adventure_event_manager.travel_event_tables_for_region(region)
        return tables

    def _threat_levels_for_path(
        self,
        locations: list[MapLocation],
        travel_days: int,
        *,
        default_threat_level: TravelThreatLevel | None,
    ) -> list[TravelThreatLevel]:
        destinations = locations[1:] or locations
        levels: list[TravelThreatLevel] = []
        for index in range(travel_days):
            location = destinations[min(len(destinations) - 1, index * len(destinations) // travel_days)]
            levels.append(self._max_threat([location.threat_level, default_threat_level] if default_threat_level else [location.threat_level]))
        return levels

    def _route_type_for_path(self, locations: list[MapLocation], fallback: TravelRouteType) -> TravelRouteType:
        route_types = [location.route_type for location in locations if location.route_type != TravelRouteType.LAND]
        return route_types[0] if route_types else fallback

    def _route_type_for_edges(self, edges: list[MapRouteEdge], fallback: TravelRouteType) -> TravelRouteType:
        route_types = [edge.route_type for edge in edges if edge.route_type != TravelRouteType.LAND]
        return route_types[0] if route_types else fallback

    def _max_threat(self, levels: list[TravelThreatLevel]) -> TravelThreatLevel:
        return max(levels, key=lambda level: self.THREAT_ORDER.index(level))

    def _threat_for_terrain(self, terrain: str) -> TravelThreatLevel:
        for key, level in self.TERRAIN_THREAT.items():
            if key in terrain:
                return level
        return TravelThreatLevel.MEDIUM

    def _route_type_for_terrain(self, terrain: str) -> TravelRouteType:
        for key, route_type in self.TERRAIN_ROUTE_TYPE.items():
            if key in terrain:
                return route_type
        return TravelRouteType.LAND

    def _terrain_for_feature(self, feature_type: str) -> str:
        return {
            "mountain_range": "山脉",
            "forest": "森林",
            "archipelago": "群岛",
            "inland_sea": "内陆湖",
            "lake": "内陆湖",
            "coast": "海岸",
            "settlement": "城市",
            "fortress": "城市",
        }.get(str(feature_type or "").strip().lower(), "草原")

    def _nearby_coordinate(self, region: MapLocation | None) -> tuple[int, int]:
        if region is None:
            return self._next_auto_coordinate()
        offset = len(self.world_state.map_locations) % 3 + 1
        return region.x + offset, region.y + 1

    def _next_auto_coordinate(self) -> tuple[int, int]:
        index = len(self.world_state.map_locations)
        return (index % 5) * 3, (index // 5) * 3

    def _threat_label(self, level: TravelThreatLevel) -> str:
        labels = {
            TravelThreatLevel.MINOR: "小",
            TravelThreatLevel.LOW: "低",
            TravelThreatLevel.MEDIUM: "中",
            TravelThreatLevel.HIGH: "高",
            TravelThreatLevel.EXTREME: "非常高",
        }
        return labels[level]
