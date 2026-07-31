from __future__ import annotations

from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.models import (
    Clock,
    ClockChange,
    DungeonArea,
    DungeonAreaType,
    DungeonDesignBrief,
    DungeonExploreMode,
    DungeonExplorationResult,
    DungeonImportance,
    DungeonMap,
    DungeonPreparation,
    DungeonState,
)


class DungeonManager:
    """管理地下城探索模式与危险命刻。"""

    DUNGEON_CONCEPTS = [
        "金字塔",
        "巫师高塔",
        "洞窟或隧道网络",
        "沉船或飞空艇残骸",
        "魔导巨像内部",
        "失落的城市",
        "大教堂",
        "城堡或要塞",
        "蒸汽动力工厂",
        "豪宅",
        "怪物巢穴",
        "石化森林",
        "被遗忘的迷宫",
        "传说中的小岛",
        "无底深渊",
        "水下祭坛",
        "巨型生物的体内",
        "另一个星球或位面",
        "下水道",
        "监狱",
    ]
    DUNGEON_FOCUSES = [
        "一件神圣武器",
        "一部末日机器",
        "一只传奇怪物",
        "与一名阿卡纳沟通的途径",
        "一个神圣生物的蛋",
        "一座隐秘城市的入口",
        "可改变世界的强大遗物或魔法",
        "一名反派跳动的心脏",
        "一个被绑架的人",
        "一名强大的巫师",
        "英雄们需要的重要情报",
        "某种禁忌仪式或法术",
        "一个敌对势力的领袖",
        "一只充满智慧的古老生物",
        "一个饱受折磨的灵魂",
        "失落的知识",
        "一部魔导科技战争机器的原型机",
        "一枚元素裂片",
        "一名邪神或恶魔",
        "通向另一个世界的传送门",
    ]
    DUNGEON_INHABITANTS = [
        "故障的魔法实验品",
        "盗贼或叛军",
        "梦境或噩梦",
        "学者和研究者",
        "元素能量实体",
        "凶猛的野兽",
        "祭司和宗教信徒",
        "异界生物",
        "龙兽和巨型蜥蜴",
        "魔化动物",
        "天国生物",
        "魔导科技构装体",
        "古怪的变异植物",
        "训练有素的士兵或战士",
        "大量致命的昆虫",
        "魔像和石像鬼",
        "可怖的不死生物",
        "古神崇拜者",
        "恶魔和地狱怪兽",
        "忠诚的仆人或保镖",
    ]
    DUNGEON_PECULIARITIES = [
        "塌方处",
        "元素魔法能量喷涌",
        "蒸汽管道和熔炉",
        "移动的走廊和阶梯",
        "精美的装饰",
        "扭曲的时空",
        "符文和法阵",
        "秘密通道和升降机",
        "持续萦绕的旋律",
        "古墓或坟地",
        "重力异常或漂浮的区域",
        "高度警戒的系统",
        "魔法镜面",
        "河流和瀑布",
        "毒雾或孢子云",
        "极端高温或寒冷",
        "突然的猛烈气流",
        "水下区域",
        "酸液池或岩浆池",
        "植被覆盖",
    ]

    def __init__(self, clock_manager: ClockManager, rules_engine: RulesEngine | None = None) -> None:
        self.clock_manager = clock_manager
        self.rules_engine = rules_engine or RulesEngine()
        self.state = DungeonState(name="", mode=DungeonExploreMode.SCENE)
        self.history: list[DungeonState] = []
        self.design_history: list[DungeonDesignBrief] = []
        self.maps: dict[str, DungeonMap] = {}

    def design_dungeon(
        self,
        name: str = "",
        *,
        importance: DungeonImportance | str = DungeonImportance.MAJOR,
        preparation: DungeonPreparation | str = DungeonPreparation.PREPARED,
        purpose: str = "",
        concept: str = "",
        focus: str = "",
        inhabitants: str = "",
        peculiarity: str = "",
        rolls: dict[str, int] | None = None,
        mode: DungeonExploreMode | str | None = None,
    ) -> DungeonDesignBrief:
        """根据 GM 章节的地下城准则生成一份可跑的地下城蓝图。"""

        importance = DungeonImportance(importance)
        preparation = DungeonPreparation(preparation)
        rolls = rolls or {}
        concept = concept or self._pick_from_table(self.DUNGEON_CONCEPTS, rolls.get("concept"))
        focus = focus or self._pick_from_table(self.DUNGEON_FOCUSES, rolls.get("focus"))
        inhabitants = inhabitants or self._pick_from_table(self.DUNGEON_INHABITANTS, rolls.get("inhabitants"))
        peculiarity = peculiarity or self._pick_from_table(self.DUNGEON_PECULIARITIES, rolls.get("peculiarity"))
        recommended_mode = DungeonExploreMode(mode) if mode is not None else self._recommend_mode(importance, preparation)
        display_name = name or f"{concept}：{focus}"

        danger_clocks = self._suggest_danger_clocks(display_name, peculiarity, importance, preparation)
        threats = [inhabitants, peculiarity]
        obstacles = [
            f"通往【{focus}】的道路被【{peculiarity}】阻隔，解决方法留给玩家发挥。",
            f"【{inhabitants}】守卫奖励或阻挡关键路径，避免只堆大量弱小敌人。",
        ]
        rewards = [
            "分散布置 2-3 份小奖励，鼓励探索不同区域。",
            f"至少一份奖励应与【{focus}】或下一场挑战形成伏笔。",
        ]
        guidance = self._guidance_for(importance, preparation, recommended_mode)
        flow_checklist = self._exploration_flow_checklist(recommended_mode, importance)
        style = f"{concept}呈现出与【{peculiarity}】交织的奇幻风貌。"
        key_point = f"关键点是【{focus}】，建议放在远离入口但可通过创意路径抵达的位置。"

        brief = DungeonDesignBrief(
            name=display_name,
            importance=importance,
            preparation=preparation,
            recommended_mode=recommended_mode,
            concept=concept,
            focus=focus,
            inhabitants=inhabitants,
            peculiarity=peculiarity,
            purpose=purpose or "消耗资源、讲述地点故事，并用隐藏奖励回馈仔细探索。",
            style=style,
            threats=threats,
            obstacles=obstacles,
            rewards=rewards,
            danger_clocks=danger_clocks,
            key_point=key_point,
            guidance=guidance,
            flow_checklist=flow_checklist,
            summary=(
                f"地下城【{display_name}】：{concept}，核心为{focus}，"
                f"栖息者是{inhabitants}，特异点是{peculiarity}。"
            ),
        )
        self.design_history.append(brief)
        return brief

    def start_from_brief(self, brief: DungeonDesignBrief, *, location: str = "") -> DungeonState:
        state = self.start_dungeon(
            brief.name,
            brief.recommended_mode,
            location=location,
            danger_clocks=brief.danger_clocks,
        )
        dungeon_map = self.build_dungeon_map(brief)
        state.concept = brief.concept
        state.focus = brief.focus
        state.inhabitants = brief.inhabitants
        state.peculiarity = brief.peculiarity
        state.purpose = brief.purpose
        state.key_point = brief.key_point
        state.rewards = list(brief.rewards)
        state.obstacles = list(brief.obstacles)
        state.areas = list(dungeon_map.areas)
        state.current_area = dungeon_map.entrance
        state.boss_room = dungeon_map.boss_room
        state.notes.extend(brief.guidance)
        state.notes.extend(brief.flow_checklist)
        return state

    def build_dungeon_map(
        self,
        brief: DungeonDesignBrief,
        *,
        include_safe_room: bool = True,
    ) -> DungeonMap:
        """生成轻量区域结构：入口、挑战、宝箱、危险核心与 Boss 房。"""

        danger_clock = next(iter(brief.danger_clocks), "")
        areas = [
            DungeonArea(
                name="入口",
                area_type=DungeonAreaType.ENTRANCE,
                description=f"通向【{brief.name}】的入口，立刻展现出{brief.concept}与{brief.peculiarity}的气质。",
                exits=["前厅"],
                notes=["用一两句话建立氛围，并提醒玩家可调查环境。"],
            ),
            DungeonArea(
                name="前厅",
                area_type=DungeonAreaType.CHALLENGE,
                description=f"【{brief.inhabitants}】活动过的区域，墙面或地面保留着关于【{brief.focus}】的线索。",
                exits=["入口", "宝箱侧室", "危险走廊"],
                danger_clock=danger_clock,
                trap=f"{brief.peculiarity}引发的第一道障碍",
                notes=["失败可推进危险命刻；成功应给出关于核心区域的线索。"],
            ),
            DungeonArea(
                name="宝箱侧室",
                area_type=DungeonAreaType.TREASURE,
                description="一间偏离主路径的房间，奖励仔细探索或创造性绕路。",
                exits=["前厅"],
                treasure="隐藏宝箱或项目/仪式材料",
                notes=["奖励不要全部集中在这里；可以放置接下来 Boss 战有用的道具。"],
            ),
            DungeonArea(
                name="危险走廊",
                area_type=DungeonAreaType.CHALLENGE,
                description=f"通往核心的路径被【{brief.peculiarity}】强化，适合使用推进目标命刻处理。",
                exits=["前厅", "核心门厅"],
                danger_clock=danger_clock,
                trap="陷阱、谜题、巡逻或环境灾害",
                notes=["只定义障碍，不规定唯一解法。"],
            ),
        ]
        if include_safe_room and brief.importance == DungeonImportance.MAJOR:
            areas.append(
                DungeonArea(
                    name="短暂避风处",
                    area_type=DungeonAreaType.SAFE_ROOM,
                    description="一处能短暂停顿、整理线索或进行角色对话的安静区域。",
                    exits=["危险走廊", "核心门厅"],
                    notes=["不一定允许完整休息，但很适合羁绊对话或揭示旧日故事。"],
                )
            )
        areas.extend(
            [
                DungeonArea(
                    name="核心门厅",
                    area_type=DungeonAreaType.PASSAGE,
                    description=f"这里直接预示【{brief.focus}】的存在，并让玩家理解 Boss 房前的利害关系。",
                    exits=["危险走廊", "Boss房"],
                    danger_clock=danger_clock,
                    notes=["适合预示强敌、相性、蓄力机制或多部件 Boss。"],
                ),
                DungeonArea(
                    name="Boss房",
                    area_type=DungeonAreaType.BOSS,
                    description=f"【{brief.focus}】所在的核心区域，{brief.inhabitants}或反派势力在此守候。",
                    exits=["核心门厅"],
                    boss=f"守护【{brief.focus}】的首领或反派",
                    treasure=f"关键奖励：{brief.focus}",
                    notes=["Boss 应至少有一个清晰目标、一个可互动机制，以及足够透明的战术信息。"],
                ),
            ]
        )
        dungeon_map = DungeonMap(
            dungeon_name=brief.name,
            areas=areas,
            entrance="入口",
            boss_room="Boss房",
            notes=[
                "区域结构是可即兴调整的骨架，不是强迫玩家按顺序移动的地图。",
                "若玩家用物语点或仪式绕开区域，尊重其效果并重写路径。",
            ],
        )
        self.maps[brief.name] = dungeon_map
        return dungeon_map

    def start_dungeon(
        self,
        name: str,
        mode: DungeonExploreMode | str,
        *,
        location: str = "",
        danger_clocks: dict[str, int] | None = None,
    ) -> DungeonState:
        mode = DungeonExploreMode(mode)
        if self.state.active:
            self.end_dungeon(
                "地下城探索被新的地点切换。",
                outcome="abandoned",
            )

        clock_names = []
        for clock_name, max_segments in (danger_clocks or {}).items():
            segments = self._coerce_int(max_segments, 6, minimum=1, maximum=99)
            self.clock_manager.add(
                Clock(
                    name=clock_name,
                    max_segments=segments,
                    clock_type="threat",
                    stakes="地下城危机命刻；填满时危机降临，而不是代表玩家目标完成。",
                    completion_consequence=f"【{clock_name}】所代表的危机已经爆发。",
                    gm_note="由 GM 根据失败、延误、噪音或地下城事件推进。",
                )
            )
            clock_names.append(clock_name)

        self.state = DungeonState(
            name=name,
            mode=mode,
            active=True,
            location=location,
            danger_clocks=clock_names,
        )
        return self.state

    def enter_area(self, area_name: str) -> DungeonArea:
        if not self.state.active:
            raise ValueError("当前没有进行中的地下城。")
        area = self._find_area(area_name)
        self.state.current_area = area.name
        self.state.notes.append(f"进入区域【{area.name}】：{area.description}")
        return area

    def mark_area_cleared(self, area_name: str, note: str = "") -> DungeonArea:
        area = self._find_area(area_name)
        area.cleared = True
        if note:
            area.notes.append(note)
        self.state.notes.append(f"区域【{area.name}】已解决。{note}")
        return area

    def add_area_treasure(
        self,
        area_name: str,
        treasure: str,
        *,
        reward_item: str = "",
        reward_zenit: int | None = None,
        reward_rarity: str = "standard",
    ) -> DungeonArea:
        area = self._find_area(area_name)
        area.treasure = treasure
        area.reward_item = reward_item
        area.reward_zenit = reward_zenit
        area.reward_rarity = reward_rarity
        if treasure not in self.state.rewards:
            self.state.rewards.append(treasure)
        return area

    def set_area_reward(
        self,
        area_name: str,
        *,
        reward_item: str = "",
        reward_zenit: int | None = None,
        reward_rarity: str = "standard",
        treasure: str = "",
    ) -> DungeonArea:
        area = self._find_area(area_name)
        if treasure:
            area.treasure = treasure
        area.reward_item = reward_item
        area.reward_zenit = reward_zenit
        area.reward_rarity = reward_rarity
        if area.treasure and area.treasure not in self.state.rewards:
            self.state.rewards.append(area.treasure)
        return area

    def add_area_trap(self, area_name: str, trap: str, danger_clock: str = "") -> DungeonArea:
        area = self._find_area(area_name)
        area.trap = trap
        if danger_clock:
            area.danger_clock = danger_clock
        return area

    def explore_area(
        self,
        area_name: str | None = None,
        *,
        actor: str = "",
        action: str = "enter",
        success: bool | None = None,
        collect_treasure: bool = False,
        trigger_trap: bool = False,
        danger_segments: int = 1,
        clear_area: bool | None = None,
        note: str = "",
    ) -> DungeonExplorationResult:
        """统一处理地下城房间事件。

        这个方法不替玩家决定唯一解法，只负责把“进入、搜索、解除陷阱、
        开宝箱、面对 Boss”等结果写回地下城状态，并在必要时推进危险命刻。
        """

        if not self.state.active:
            raise ValueError("当前没有进行中的地下城。")
        if not self.state.areas:
            raise ValueError("当前地下城没有区域地图。")

        normalized_action = self._normalize_action(action)
        danger_segments = self._coerce_int(danger_segments, 1, minimum=1, maximum=6)
        area = self.enter_area(area_name or self.state.current_area or self.state.areas[0].name)
        area.discovered = True
        notes: list[str] = []
        danger_change: ClockChange | None = None
        trap_triggered = False
        trap_disarmed = area.trap_disarmed
        treasure_found = bool(area.treasure)
        treasure_collected = False
        boss_revealed = bool(area.boss or area.area_type == DungeonAreaType.BOSS)
        event_template = self._pick_area_event(area, normalized_action)
        event_name = ""
        event_detail = ""
        event_tags: list[str] = []
        if event_template is not None:
            event_name = event_template.name
            event_detail = event_template.description
            event_tags = list(event_template.tags)
            notes.append(f"事件【{event_template.name}】：{event_template.description}")
            if event_template.mechanical_hint:
                notes.append(f"事件提示：{event_template.mechanical_hint}")

        if area.trap and not area.trap_disarmed:
            if normalized_action == "disarm_trap":
                if success is True:
                    area.trap_disarmed = True
                    trap_disarmed = True
                    notes.append(f"陷阱【{area.trap}】被解除。")
                elif success is False:
                    trap_triggered = True
                    notes.append(f"解除陷阱【{area.trap}】失败，危险被触发。")
                    danger_change = self._advance_area_danger(area, danger_segments, "解除陷阱失败。")
                else:
                    notes.append(f"发现陷阱【{area.trap}】，需要检定或创造性行动来解除。")
            elif trigger_trap:
                trap_triggered = True
                notes.append(f"区域陷阱【{area.trap}】被触发。")
                danger_change = self._advance_area_danger(area, danger_segments, "区域陷阱被触发。")
            else:
                notes.append(f"这里存在尚未处理的威胁：【{area.trap}】。")

        if normalized_action in {"search", "open_treasure"} and area.treasure:
            notes.append(f"发现奖励线索：【{area.treasure}】。")

        should_collect = collect_treasure or normalized_action == "open_treasure"
        if should_collect and area.treasure:
            if area.treasure_collected:
                notes.append(f"区域【{area.name}】的奖励已经被取得。")
            else:
                area.treasure_collected = True
                treasure_collected = True
                notes.append(f"取得区域奖励：【{area.treasure}】。")

        if boss_revealed:
            notes.append(area.boss or "Boss 房已经揭示，适合切入首领战或最终目标命刻。")

        if clear_area is None:
            clear_area = normalized_action in {"clear", "open_treasure"} or (
                normalized_action == "disarm_trap" and area.trap_disarmed
            )
        if clear_area:
            area.cleared = True
            notes.append(f"区域【{area.name}】已标记为解决。")

        if note:
            notes.append(note)
            area.notes.append(note)

        summary_parts = [
            f"{actor or '队伍'}在地下城【{self.state.name}】探索区域【{area.name}】。",
            area.description,
        ]
        summary_parts.extend(notes)
        if area.exits:
            summary_parts.append(f"可前往：{'、'.join(area.exits)}。")
        summary = " ".join(part for part in summary_parts if part)
        self.state.notes.append(summary)
        hard_rule_summary = self._dungeon_hard_rule_summary(
            actor=actor,
            area=area,
            normalized_action=normalized_action,
            trap_triggered=trap_triggered,
            trap_disarmed=trap_disarmed,
            treasure_collected=treasure_collected,
            boss_revealed=boss_revealed,
            danger_change=danger_change,
            area_cleared=area.cleared,
        )
        llm_narrative_prompt = self._dungeon_llm_prompt(
            actor=actor,
            area=area,
            normalized_action=normalized_action,
            event_name=event_name,
            event_detail=event_detail,
            event_tags=event_tags,
            trap_triggered=trap_triggered,
            trap_disarmed=trap_disarmed,
            treasure_found=treasure_found,
            treasure_collected=treasure_collected,
            boss_revealed=boss_revealed,
            danger_change=danger_change,
        )

        return DungeonExplorationResult(
            actor=actor,
            dungeon_name=self.state.name,
            area_name=area.name,
            area_type=area.area_type,
            action=normalized_action,
            description=area.description,
            exits=list(area.exits),
            trap=area.trap,
            trap_triggered=trap_triggered,
            trap_disarmed=trap_disarmed,
            treasure=area.treasure,
            reward_item=area.reward_item,
            reward_zenit=area.reward_zenit,
            reward_rarity=area.reward_rarity,
            treasure_found=treasure_found,
            treasure_collected=treasure_collected,
            boss=area.boss,
            boss_revealed=boss_revealed,
            event_name=event_name,
            event_detail=event_detail,
            event_tags=event_tags,
            danger_change=danger_change,
            area_cleared=area.cleared,
            notes=notes,
            summary=summary,
            hard_rule_summary=hard_rule_summary,
            llm_narrative_prompt=llm_narrative_prompt,
        )

    def trigger_danger(self, clock_name: str, segments: int = 1, reason: str = "") -> ClockChange:
        segments = self._coerce_int(segments, 1, minimum=1, maximum=6)
        before, after = self.clock_manager.advance(clock_name, segments)
        clock = self.clock_manager.get(clock_name)
        change = ClockChange(
            clock_name=clock.name,
            before=before,
            after=after,
            delta=after - before,
            max_segments=clock.max_segments,
            reason=reason or "地下城中的噪音、延误或失败推进了危险命刻。",
            clock_type=clock.clock_type,
            stakes=clock.stakes,
            completion_consequence=clock.completion_consequence,
        )
        if self.state.active:
            self.state.notes.append(
                f"危险命刻【{clock.name}】推进到 {clock.current}/{clock.max_segments}。"
            )
        return change

    def _advance_area_danger(self, area: DungeonArea, segments: int, reason: str) -> ClockChange | None:
        clock_name = area.danger_clock or (self.state.danger_clocks[0] if self.state.danger_clocks else "")
        if not clock_name:
            return None
        return self.trigger_danger(clock_name, max(1, segments), reason)

    def _exploration_flow_checklist(
        self,
        mode: DungeonExploreMode,
        importance: DungeonImportance,
    ) -> list[str]:
        if mode == DungeonExploreMode.SKIP:
            return [
                "简化地下城：用一次团队检定或一个目标命刻概括穿越过程；成功给奖励/线索，失败推进危险或消耗资源。",
                "即使跳过逐房探索，也要给玩家一个可选择的代价或发现，不要只宣布结果。",
            ]
        checklist = [
            "入口：先给可行动信息，包括可见出口、危险征兆、潜在线索；不要把关键线索藏成猜谜。",
            "探索循环：玩家进入区域、选择调查/解除/绕过/开宝箱/Boss 对峙，GM 再结算区域事件。",
            "失败处理：失败优先推进区域危险命刻、触发陷阱、暴露巡逻或改变位置；不要让谜团卡死。",
            "奖励分布：宝箱、侧室或安全房提供小奖励/材料/线索；关键奖励放在核心目标或 Boss 后。",
            "Boss 前预示：核心门厅应展示相性、蓄力、守卫、环境或多阶段线索，让玩家能制定战术。",
        ]
        if importance == DungeonImportance.MAJOR:
            checklist.append("大型地下城：至少安排一个短暂停顿点，让角色整理线索、恢复少量节奏或进行羁绊对话。")
        return checklist

    def _dungeon_hard_rule_summary(
        self,
        *,
        actor: str,
        area: DungeonArea,
        normalized_action: str,
        trap_triggered: bool,
        trap_disarmed: bool,
        treasure_collected: bool,
        boss_revealed: bool,
        danger_change: ClockChange | None,
        area_cleared: bool,
    ) -> str:
        parts = [
            f"地下城硬结算：{actor or '队伍'}在【{self.state.name}】的【{area.name}】执行 {normalized_action}。",
            f"区域类型：{area.area_type.value}。",
        ]
        if area.exits:
            parts.append(f"可用出口：{'、'.join(area.exits)}。")
        if area.trap:
            trap_state = "已触发" if trap_triggered else ("已解除" if trap_disarmed else "仍存在")
            parts.append(f"陷阱【{area.trap}】状态：{trap_state}。")
        if area.treasure:
            treasure_state = "已取得" if treasure_collected or area.treasure_collected else "可作为奖励线索存在"
            parts.append(f"奖励【{area.treasure}】状态：{treasure_state}。")
        if boss_revealed:
            parts.append(f"Boss/核心威胁已揭示：{area.boss or '此区域适合切入首领战或目标命刻'}。")
        if danger_change is not None:
            parts.append(
                f"危险命刻【{danger_change.clock_name}】变化：{danger_change.before}->{danger_change.after}/{danger_change.max_segments}。"
            )
        parts.append(f"区域解决状态：{'已解决' if area_cleared else '未解决'}。")
        parts.append("不要在叙事中自行发放额外奖励、推进命刻、造成伤害、施加状态或改变 Boss 数值。")
        return " ".join(parts)

    def _dungeon_llm_prompt(
        self,
        *,
        actor: str,
        area: DungeonArea,
        normalized_action: str,
        event_name: str,
        event_detail: str,
        event_tags: list[str],
        trap_triggered: bool,
        trap_disarmed: bool,
        treasure_found: bool,
        treasure_collected: bool,
        boss_revealed: bool,
        danger_change: ClockChange | None,
    ) -> str:
        creative_scope = [
            "可以自由描写房间氛围、视觉听觉细节、线索呈现、NPC/怪物姿态、角色动作和玩家选择带来的情绪反馈。",
            "可以提出 1-2 个下一步可选方向，但不要替玩家决定路线。",
        ]
        if event_name:
            creative_scope.append(f"可把事件种子【{event_name}】改写成更贴合当前世界观的表现：{event_detail}")
        if area.trap and not trap_disarmed:
            if trap_triggered:
                creative_scope.append("陷阱已经触发；可以描述压力和后果预兆，但具体伤害/状态需另走硬规则。")
            else:
                creative_scope.append("陷阱仍是悬而未决的风险；可以提示可解除、绕开、用仪式或物语点改写。")
        if treasure_found:
            if treasure_collected:
                creative_scope.append("奖励已被取得；可以描写宝箱、遗物或素材的故事感，但不要额外加奖。")
            else:
                creative_scope.append("奖励只是被发现；如果玩家要取得或鉴定，应继续使用对应硬规则。")
        if boss_revealed:
            creative_scope.append("Boss/核心目标已揭示；可以强化压迫感、目标冲突和战术预告，但不要默认每个 Boss 都有多部件。")
        if danger_change is not None:
            creative_scope.append("危险命刻已经被 Python 推进；可以描述环境如何变得更危险。")
        tags = f"事件标签：{', '.join(event_tags)}。" if event_tags else ""
        return (
            f"请 GM LLM 根据地下城硬结果创作叙事。探索者：{actor or '队伍'}；"
            f"地下城：{self.state.name}；区域：{area.name}；动作：{normalized_action}；"
            f"区域骨架：{area.description}；出口：{'、'.join(area.exits) if area.exits else '无'}。"
            f"{tags}{''.join(creative_scope)}"
        )

    def exploration_failure(self, clock_name: str, margin: int = 0) -> ClockChange:
        if margin <= -6:
            segments = 3
        elif margin <= -3:
            segments = 2
        else:
            segments = 1
        return self.trigger_danger(clock_name, segments, "探索失败推进危险命刻。")

    def end_dungeon(
        self,
        summary: str = "",
        *,
        outcome: str = "completed",
    ) -> DungeonState | None:
        if not self.state.active:
            return None
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"completed", "retreated", "abandoned"}:
            raise ValueError(
                "地下城结束结果必须是completed、retreated或abandoned。"
            )
        if summary:
            self.state.notes.append(summary)
        self.state.completion_status = normalized_outcome
        self.state.completion_summary = summary
        for clock_name in list(self.state.danger_clocks):
            if not self.clock_manager.exists(clock_name):
                continue
            clock = self.clock_manager.get(clock_name)
            if clock.current >= clock.max_segments:
                self.clock_manager.resolve(
                    clock_name,
                    note=summary or "地下城危机已经兑现。",
                    archive=True,
                )
            else:
                self.clock_manager.abandon(
                    clock_name,
                    note=summary or "地下城探索结束，该局部危机不再推进。",
                )
        self.state.active = False
        ended = self.state
        self.history.append(ended)
        self.state = DungeonState(name="", mode=DungeonExploreMode.SCENE)
        return ended

    def format_status(self) -> str:
        if not self.state.active:
            return "无进行中的地下城探索"
        clocks = "、".join(self.state.danger_clocks) if self.state.danger_clocks else "无危险命刻"
        focus = f"，核心：{self.state.focus}" if self.state.focus else ""
        area = f"，当前位置：{self.state.current_area}" if self.state.current_area else ""
        return f"地下城【{self.state.name}】模式：{self.state.mode.value}{focus}{area}，危险：{clocks}"

    def _find_area(self, area_name: str) -> DungeonArea:
        requested = (area_name or "").strip()
        if not requested and self.state.current_area:
            requested = self.state.current_area
        for area in self.state.areas:
            if area.name == requested:
                return area
        normalized_requested = self._normalize_area_lookup(requested)
        for area in self.state.areas:
            normalized_area = self._normalize_area_lookup(area.name)
            if normalized_area and (
                normalized_area == normalized_requested
                or normalized_area in normalized_requested
                or normalized_requested in normalized_area
            ):
                return area
        inferred = self._infer_area_by_alias(normalized_requested)
        if inferred is not None:
            return inferred
        available = "、".join(area.name for area in self.state.areas)
        raise ValueError(f"地下城中不存在区域：{area_name}。可用区域：{available}")

    def _coerce_int(self, value, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
        if value is None or value == "":
            number = default
        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = default
        if minimum is not None:
            number = max(minimum, number)
        if maximum is not None:
            number = min(maximum, number)
        return number

    def _normalize_area_lookup(self, area_name: str) -> str:
        text = (area_name or "").strip().lower()
        for separator in ("：", ":", "/", "\\", ">", "→", "-"):
            if separator in text:
                text = text.split(separator)[-1].strip()
        for token in ("【", "】", "[", "]", "（", "）", "(", ")", " ", "\t", "\n"):
            text = text.replace(token, "")
        return text

    def _infer_area_by_alias(self, normalized_requested: str) -> DungeonArea | None:
        if not normalized_requested:
            return None
        alias_groups = [
            (("boss", "首领", "核心威胁", "决战", "王房"), ("Boss房", "首领房", "核心门厅")),
            (("宝箱", "宝藏", "奖励", "侧室", "藏品"), ("宝箱侧室", "藏宝室")),
            (("走廊", "通道", "镜面", "旋转", "危险", "陷阱", "机关"), ("危险走廊", "机关走廊")),
            (("入口", "门口", "遗迹口"), ("入口",)),
            (("前厅", "大厅", "门厅"), ("前厅", "核心门厅")),
            (("避风", "安全", "休息"), ("短暂避风处", "休息点")),
        ]
        for keywords, preferred_names in alias_groups:
            if any(keyword in normalized_requested for keyword in keywords):
                for preferred_name in preferred_names:
                    for area in self.state.areas:
                        if area.name == preferred_name:
                            return area
                for area in self.state.areas:
                    normalized_area = self._normalize_area_lookup(area.name)
                    if any(keyword in normalized_area for keyword in keywords):
                        return area
        return None

    def _pick_area_event(self, area: DungeonArea, action: str):
        if not area.event_templates:
            return None
        preferred_tags = {
            "open_treasure": {"treasure"},
            "confront_boss": {"boss"},
            "disarm_trap": {"challenge", "clock"},
            "search": {"memory", "faction", "foreshadow", "lore"},
            "enter": {"entrance", "foreshadow", "faction", "memory"},
            "clear": {"challenge", "clock"},
        }.get(action, set())
        for template in area.event_templates:
            if preferred_tags.intersection(template.tags):
                return template
        return area.event_templates[0]

    def _normalize_action(self, action: str) -> str:
        aliases = {
            "进入": "enter",
            "移动": "enter",
            "前往": "enter",
            "探索": "search",
            "搜索": "search",
            "调查": "search",
            "解除陷阱": "disarm_trap",
            "拆除陷阱": "disarm_trap",
            "开宝箱": "open_treasure",
            "打开宝箱": "open_treasure",
            "取得宝藏": "open_treasure",
            "清理": "clear",
            "解决": "clear",
            "面对Boss": "confront_boss",
            "面对首领": "confront_boss",
            "boss": "confront_boss",
        }
        normalized = (action or "enter").strip()
        return aliases.get(normalized, normalized)

    def _pick_from_table(self, table: list[str], roll: int | None) -> str:
        if roll is None:
            roll = self.rules_engine.roll_die(len(table))
        index = max(1, min(len(table), roll)) - 1
        return table[index]

    def _recommend_mode(
        self,
        importance: DungeonImportance,
        preparation: DungeonPreparation,
    ) -> DungeonExploreMode:
        if importance == DungeonImportance.MAJOR and preparation == DungeonPreparation.PREPARED:
            return DungeonExploreMode.DETAILED
        if importance == DungeonImportance.MAJOR:
            return DungeonExploreMode.SCENE
        if preparation == DungeonPreparation.PREPARED:
            return DungeonExploreMode.SCENE
        return DungeonExploreMode.SKIP

    def _suggest_danger_clocks(
        self,
        dungeon_name: str,
        peculiarity: str,
        importance: DungeonImportance,
        preparation: DungeonPreparation,
    ) -> dict[str, int]:
        clocks: dict[str, int] = {}
        if preparation == DungeonPreparation.IMPROVISED and importance == DungeonImportance.MINOR:
            clocks[f"{dungeon_name}：突发危机"] = 4
            return clocks
        if "警戒" in peculiarity or "监控" in peculiarity:
            clocks[f"{dungeon_name}：高度警戒"] = 4
        elif "毒雾" in peculiarity or "高温" in peculiarity or "寒冷" in peculiarity or "岩浆" in peculiarity:
            clocks[f"{dungeon_name}：环境失控"] = 6
        else:
            clocks[f"{dungeon_name}：游荡威胁"] = 6

        if importance == DungeonImportance.MAJOR:
            clocks[f"{dungeon_name}：核心危机逼近"] = 8
        return clocks

    def _guidance_for(
        self,
        importance: DungeonImportance,
        preparation: DungeonPreparation,
        mode: DungeonExploreMode,
    ) -> list[str]:
        guidance = [
            "地下城应至少服务于资源消耗、地点叙事或探索奖励之一；不要为了地下城而地下城。",
            "障碍只定义问题，不预设唯一解法，让玩家用职业、仪式、项目或物语点发挥创意。",
        ]
        if mode == DungeonExploreMode.DETAILED:
            guidance.append("建议绘制简要草图，分散宝箱和线索，安排两到三场有意义的挑战。")
        elif mode == DungeonExploreMode.SCENE:
            guidance.append("建议以连续场景推进，重点放在关键障碍、危险命刻和一两处奖励。")
        else:
            guidance.append("建议用幕间或单次团队检定快速处理，只保留一个核心挑战。")
        if importance == DungeonImportance.MAJOR and preparation == DungeonPreparation.IMPROVISED:
            guidance.append("这是重要但即兴的地点：直奔核心原因；若需要复杂细节，可以在悬念处暂停准备。")
        return guidance
