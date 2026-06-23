from __future__ import annotations

from fu_gm.components.world_state import WorldState
from fu_gm.gm_guidance import PreparedLocationSeed, build_gm_guidance
from fu_gm.models import (
    AdventureEventContext,
    DungeonArea,
    DungeonAreaType,
    DungeonEventTemplate,
    DungeonState,
    MapLocation,
    MemoryVisibility,
    TravelEventTemplate,
    TravelRouteType,
    TravelThreatLevel,
)


class AdventureEventManager:
    """根据地点、地形、阵营和长期记忆生成冒险事件模板。"""

    TERRAIN_DANGERS = {
        "森林": TravelEventTemplate(
            "迷雾兽径",
            "树影和雾气把道路拆成数条相似的小径，附近野兽似乎正在驱赶队伍偏离正路。",
            "适合要求【INS+INS】或【DEX+INS】团队检定；失败时迷路或推进威胁命刻。",
            ("terrain", "forest", "beast"),
        ),
        "广袤森林": TravelEventTemplate(
            "古林低语",
            "古老树冠下传来像人声一样的低语，灵魂流在这里变得粘稠而敏感。",
            "适合让魔法或情感相关角色获得线索；失败可施加动摇。",
            ("terrain", "forest", "spirit"),
        ),
        "高山": TravelEventTemplate(
            "断崖落石",
            "山道忽然崩塌，碎石像钟声一样滚入深谷。",
            "适合建立 4 格命刻来抢修道路、绕路或保护交通工具。",
            ("terrain", "mountain"),
        ),
        "沙漠": TravelEventTemplate(
            "蜃楼岔路",
            "热浪把远处遗迹折成虚假的重影，水源和方向感都开始背叛队伍。",
            "适合消耗 IP、要求团队检定，或引出被埋藏的地点。",
            ("terrain", "desert"),
        ),
        "火山": TravelEventTemplate(
            "熔岩脉动",
            "地底的火光沿裂缝呼吸，空气中有硫磺和古代魔力混杂的味道。",
            "可造成即兴火伤害；也可让玩家利用地形反制敌人。",
            ("terrain", "fire"),
        ),
        "大海": TravelEventTemplate(
            "黑潮暗流",
            "海面颜色突然变深，船底传来像巨兽翻身一样的低鸣。",
            "适合要求航行检定；失败时偏航、损失时间或遭遇海兽。",
            ("terrain", "water"),
        ),
        "云海": TravelEventTemplate(
            "空域乱流",
            "云层像被看不见的巨手搅动，飞艇或飞行坐骑被卷向未知高度。",
            "适合考验交通工具；失败时改变降落点或发现漂浮遗迹。",
            ("terrain", "air"),
        ),
        "警戒地区": TravelEventTemplate(
            "边境盘查",
            "道路上的岗哨正在逐个盘查旅行者，通行证、旗帜和谎言都可能变得很重要。",
            "适合社交检定、潜入检定或触发短冲突。",
            ("faction", "checkpoint"),
        ),
    }
    TERRAIN_DISCOVERIES = {
        "森林": TravelEventTemplate(
            "林间路标",
            "藤蔓下露出旧文明留下的路标，上面的符号指向一处未标记的遗迹。",
            "可在地图上添加新地点，或给予下一次地下城探索 +2 环境优势。",
            ("location", "forest", "clue"),
        ),
        "高山": TravelEventTemplate(
            "旧矿脉",
            "岩壁裂缝中闪出稀有矿物的光，旁边还有前人留下的开采记号。",
            "可作为项目材料抵扣，或作为地下城/工坊线索。",
            ("material", "mountain"),
        ),
        "沙漠": TravelEventTemplate(
            "半埋方尖碑",
            "风沙短暂退去，露出刻有星图和王名的黑色方尖碑。",
            "写入世界谜团；也可作为仪式定位或古代王国线索。",
            ("memory", "desert", "ruin"),
        ),
        "火山": TravelEventTemplate(
            "结晶火泉",
            "岩浆旁有一眼不会灼人的火色泉水，灵魂能量在其中结成晶壳。",
            "可作为元素仪式材料，或给下一次火/土相关检定优势。",
            ("material", "fire"),
        ),
        "大海": TravelEventTemplate(
            "漂流瓶星图",
            "一只密封瓶被浪推到甲板边，里面的星图指向一座传说小岛。",
            "可添加海上地点或触发海盗/幽灵船支线。",
            ("location", "water", "clue"),
        ),
        "云海": TravelEventTemplate(
            "漂浮残骸",
            "云层里浮出一截古代飞艇残骸，仍有微弱的魔导灯在闪烁。",
            "可添加空中地下城入口，或获得魔科技项目材料。",
            ("dungeon", "air", "magitech"),
        ),
    }

    def __init__(self, world_state: WorldState) -> None:
        self.world_state = world_state

    def build_context(self, region: str, *, include_private: bool = True) -> AdventureEventContext:
        location = self.world_state.map_locations.get(region)
        public_memory = self.world_state.retrieve_relevant_memory(
            region,
            include_private=False,
            limit=4,
            extra_entities=self._context_entities(location, region),
        )
        private_hooks: list[str] = []
        if include_private:
            private_candidates = self.world_state.retrieve_relevant_memory(
                region,
                include_private=True,
                limit=8,
                extra_entities=self._context_entities(location, region),
            )
            private_hooks = [memory for memory in private_candidates if memory not in public_memory][:3]
        if location is None:
            return AdventureEventContext(
                region=region,
                public_memory=public_memory,
                private_hooks=private_hooks,
                summary=f"{region}：未登记地点；公开记忆 {len(public_memory)} 条。",
            )
        return AdventureEventContext(
            region=region,
            description=location.description,
            terrain=location.terrain,
            faction=location.faction,
            threat_level=location.threat_level,
            route_type=location.route_type,
            public_memory=public_memory,
            private_hooks=private_hooks,
            tags=list(location.tags),
            summary=(
                f"{region}：{location.terrain}，威胁 {location.threat_level.value}"
                + (f"，势力 {location.faction}" if location.faction else "")
                + f"；公开记忆 {len(public_memory)} 条，GM暗线 {len(private_hooks)} 条。"
            ),
        )

    def travel_event_tables_for_region(self, region: str) -> dict[str, list[TravelEventTemplate]]:
        context = self.build_context(region)
        danger = self.travel_danger_templates(context)
        discovery = self.travel_discovery_templates(context)
        return {"danger": danger, "discovery": discovery}

    def travel_danger_templates(self, context: AdventureEventContext) -> list[TravelEventTemplate]:
        templates: list[TravelEventTemplate] = []
        terrain_template = self._terrain_template(context.terrain, self.TERRAIN_DANGERS)
        if terrain_template:
            templates.append(terrain_template)
        if context.faction:
            templates.append(
                TravelEventTemplate(
                    f"{context.faction}的巡逻",
                    f"{context.faction}的人马正在这里行动，他们的目标不一定是英雄，但会改变局势。",
                    "可用作短冲突、交涉或反派命刻推进；若玩家有相关羁绊，应优先召回。",
                    ("faction", "patrol"),
                )
            )
        if context.public_memory:
            templates.append(
                TravelEventTemplate(
                    "旧怨回声",
                    f"旅途中出现与旧事相关的痕迹：{context.public_memory[0]}",
                    "让旧 NPC、旧地点或旧承诺回到当前场景；不要泄露 GM 私密暗线。",
                    ("memory", "callback"),
                )
            )
        if context.private_hooks:
            templates.append(
                TravelEventTemplate(
                    "暗线阴影",
                    "某个尚未公开的势力在附近留下了不完整的痕迹，玩家只能看到表层异常。",
                    "GM 私密提示存在于事件上下文中；表达时只给线索，不直接揭露真相。",
                    ("secret", "foreshadow"),
                )
            )
        if not templates:
            templates.append(
                TravelEventTemplate(
                    "地区压力",
                    "此地的环境、居民或历史让旅途变得不安，哪怕道路本身并未封死。",
                    "可要求一次团队检定，或用作威胁命刻的前兆。",
                    ("region",),
                )
            )
        return self._dedupe_templates(templates)

    def travel_discovery_templates(self, context: AdventureEventContext) -> list[TravelEventTemplate]:
        templates: list[TravelEventTemplate] = []
        terrain_template = self._terrain_template(context.terrain, self.TERRAIN_DISCOVERIES)
        if terrain_template:
            templates.append(terrain_template)
        if context.faction:
            templates.append(
                TravelEventTemplate(
                    f"{context.faction}的遗留物",
                    f"队伍发现一件带有{context.faction}标记的物品、信件或废弃营地。",
                    "可添加阵营线索、NPC 关系或下一处地图地点。",
                    ("faction", "clue"),
                )
            )
        if context.public_memory:
            templates.append(
                TravelEventTemplate(
                    "旧日线索",
                    f"一个细节把队伍带回旧事：{context.public_memory[0]}",
                    "写入世界记忆，并让相关 NPC/地点重新进入上下文。",
                    ("memory", "clue"),
                )
            )
        if context.route_type == TravelRouteType.AIR:
            templates.append(
                TravelEventTemplate(
                    "云上海图",
                    "云层裂开时，下方地貌组成一张天然地图，指向一条更快但更危险的航线。",
                    "可记录新路线，或降低下一次同路旅行 1 日。",
                    ("route", "air"),
                )
            )
        if context.threat_level in {TravelThreatLevel.HIGH, TravelThreatLevel.EXTREME}:
            templates.append(
                TravelEventTemplate(
                    "险地补给点",
                    "在危险区域深处，队伍找到前人留下的补给、符标或隐蔽避难处。",
                    "可允许休整片刻、恢复少量 IP，或降低下一日威胁等级。",
                    ("rest", "resource"),
                )
            )
        seed = self._prepared_location_seed(context)
        if seed is not None:
            templates.append(
                TravelEventTemplate(
                    f"预备地点线索：{seed.name}",
                    f"旅途中出现通向【{seed.name}】的传闻、地图碎片或可疑标记。{seed.brief}",
                    "这是 GM 预备地点库给出的候选；只有玩家追踪或剧情需要时才把它正式登记到地图。",
                    ("prepared_location", *seed.inspiration_tags),
                )
            )
        if not templates:
            templates.append(
                TravelEventTemplate(
                    "旅人传闻",
                    "路过的旅人分享一条关于附近地点、怪物或宝藏的可靠传闻。",
                    "可添加新地点、NPC 或地下城入口。",
                    ("rumor",),
                )
            )
        return self._dedupe_templates(templates)

    def prepared_location_candidates(self, *, limit: int | None = None) -> list[PreparedLocationSeed]:
        guidance = build_gm_guidance(self.world_state.world_profile)
        if limit is None:
            return list(guidance.location_seeds)
        return list(guidance.location_seeds[: max(1, limit)])

    def enrich_dungeon_state(self, state: DungeonState) -> DungeonState:
        if not state.areas:
            return state
        context = self.build_context(state.location or state.name)
        for area in state.areas:
            for template in self.dungeon_area_templates(state, area, context):
                if template.name not in {existing.name for existing in area.event_templates}:
                    area.event_templates.append(template)
            if area.event_templates:
                names = "、".join(template.name for template in area.event_templates[:3])
                note = f"上下文事件模板：{names}"
                if note not in area.notes:
                    area.notes.append(note)
        self.world_state.record_memory_event(
            f"地下城【{state.name}】已根据地图上下文生成房间事件模板。",
            kind="dungeon_event_templates",
            visibility=MemoryVisibility.PRIVATE,
            entities=[state.name, state.location],
            tags=["dungeon", "event_template"],
            source="AdventureEventManager",
        )
        return state

    def dungeon_area_templates(
        self,
        state: DungeonState,
        area: DungeonArea,
        context: AdventureEventContext,
    ) -> list[DungeonEventTemplate]:
        templates: list[DungeonEventTemplate] = []
        if area.area_type == DungeonAreaType.ENTRANCE:
            templates.append(
                DungeonEventTemplate(
                    "入口预兆",
                    f"入口处的痕迹把【{state.concept or state.name}】和{context.region}的旧事联系起来。",
                    "让玩家看到危险类型、栖息者痕迹或与世界地图相关的线索。",
                    ("entrance", "foreshadow"),
                )
            )
        if area.area_type == DungeonAreaType.CHALLENGE:
            templates.append(
                DungeonEventTemplate(
                    "可变障碍",
                    f"这里的障碍并非单纯机关，而是由【{state.peculiarity or area.trap or '地下城异象'}】推动。",
                    "允许多种解法；失败时推进危险命刻，不要空转。",
                    ("challenge", "clock"),
                )
            )
        if area.area_type == DungeonAreaType.TREASURE:
            templates.append(
                DungeonEventTemplate(
                    "带故事的宝箱",
                    "宝箱旁有刻痕、徽记或旧日留言，暗示这份奖励曾属于谁。",
                    "奖励可关联阵营、旧 NPC 或下一场战斗的相性伏笔。",
                    ("treasure", "lore"),
                )
            )
        if area.area_type == DungeonAreaType.SAFE_ROOM:
            templates.append(
                DungeonEventTemplate(
                    "短暂营火",
                    "此处安静得不合常理，适合角色谈论恐惧、羁绊或下一步计划。",
                    "可触发羁绊变化、补充线索或让项目/仪式获得叙事材料。",
                    ("rest", "bond"),
                )
            )
        if area.area_type == DungeonAreaType.BOSS:
            templates.append(
                DungeonEventTemplate(
                    "首领映照",
                    f"核心区域把【{state.focus or area.boss or '关键目标'}】塑造成英雄主题的黑暗镜像。",
                    "Boss 应公开关键战术信息；若是反派，考虑终结点和升格。",
                    ("boss", "mirror"),
                )
            )
        if context.faction:
            templates.append(
                DungeonEventTemplate(
                    f"{context.faction}痕迹",
                    f"区域里出现{context.faction}的标记、补给或战斗痕迹。",
                    "可把阵营目标接回当前地下城，并为 NPCAct 提供动机。",
                    ("faction",),
                )
            )
        if context.public_memory:
            templates.append(
                DungeonEventTemplate(
                    "记忆回声",
                    f"某个细节呼应了旧记忆：{context.public_memory[0]}",
                    "让旧 NPC、羁绊、地点承诺回到场景；这是公开记忆，可供表达。",
                    ("memory",),
                )
            )
        if context.private_hooks:
            templates.append(
                DungeonEventTemplate(
                    "暗线伏笔",
                    "房间里有一处异常细节，其真正含义暂时只属于 GM 私密暗线。",
                    "表达时只描述可见异常，不揭露私密内容。",
                    ("secret",),
                )
            )
        return self._dedupe_dungeon_templates(templates)

    def _context_entities(self, location: MapLocation | None, region: str) -> list[str]:
        entities = [region]
        if location and location.faction:
            entities.append(location.faction)
        return entities

    def _prepared_location_seed(self, context: AdventureEventContext) -> PreparedLocationSeed | None:
        candidates = self.prepared_location_candidates()
        if not candidates:
            return None
        context_text = " ".join(
            [
                context.region,
                context.description,
                context.terrain,
                context.faction,
                " ".join(context.tags),
            ]
        )
        scored = []
        for seed in candidates:
            score = sum(1 for tag in seed.inspiration_tags if tag in context.tags)
            if seed.name in self.world_state.map_locations:
                score -= 10
            for token in (
                seed.name,
                seed.archetype,
                *seed.keywords,
                *seed.terrain,
                *seed.themes,
                *seed.hooks,
            ):
                if token and token in context_text:
                    score += 2
            scored.append((score, seed))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return candidates[0]
        top_score = scored[0][0]
        top_candidates = [seed for score, seed in scored if score == top_score]
        stable_index = sum(ord(char) for char in context_text) % len(top_candidates)
        return top_candidates[stable_index]

    def _terrain_template(
        self,
        terrain: str,
        table: dict[str, TravelEventTemplate],
    ) -> TravelEventTemplate | None:
        for key, template in table.items():
            if key in terrain:
                return template
        return None

    def _dedupe_templates(self, templates: list[TravelEventTemplate]) -> list[TravelEventTemplate]:
        deduped: list[TravelEventTemplate] = []
        names: set[str] = set()
        for template in templates:
            if template.name not in names:
                deduped.append(template)
                names.add(template.name)
        return deduped

    def _dedupe_dungeon_templates(self, templates: list[DungeonEventTemplate]) -> list[DungeonEventTemplate]:
        deduped: list[DungeonEventTemplate] = []
        names: set[str] = set()
        for template in templates:
            if template.name not in names:
                deduped.append(template)
                names.add(template.name)
        return deduped
