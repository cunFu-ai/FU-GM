from __future__ import annotations

from fu_gm.prepared_locations.models import PreparedLocationSeed, story_hook


def _core_seed(
    *,
    name: str,
    archetype: str,
    tags: tuple[str, ...],
    brief: str,
    use_when: str,
    questions: tuple[str, ...],
    hooks: tuple[str, ...],
    icon_name: str,
) -> PreparedLocationSeed:
    story_hooks = tuple(
        story_hook(title, "seed", f"围绕“{title}”展开一段可由玩家选择塑形的地点事件。")
        for title in hooks
    )
    return PreparedLocationSeed(
        name=name,
        archetype=archetype,
        inspiration_tags=tags,
        brief=brief,
        use_when=use_when,
        questions=questions,
        hooks=hooks,
        story_hooks=story_hooks,
        icon_name=icon_name,
    )


CORE_LOCATION_SEEDS: tuple[PreparedLocationSeed, ...] = (
    _core_seed(
        name="噬神古林",
        archetype="吞没神话的黑暗森林",
        tags=("epic_myth", "natural_home", "dungeon_mystery"),
        brief="森林是古代神灵、失落王朝或失败仪式留下的活体记忆。",
        use_when="世界中出现古森林、自然灵、神祇、遗失道路或被封印的力量。",
        questions=("森林想让外来者记住什么？", "谁曾经为了保护这里而犯下错误？"),
        hooks=("被树根吞没的神殿", "会说谎的路标", "守林者的禁令"),
        icon_name="噬神古林",
    ),
    _core_seed(
        name="天空圣国",
        archetype="高处的理想乡",
        tags=("epic_myth", "techno_pressure"),
        brief="悬于云上的城市或宫殿令人惊叹，但它的洁净与繁荣可能依赖地上的牺牲。",
        use_when="世界中出现浮岛、飞艇、天空、神国、上层城市或光辉统治者。",
        questions=("它为什么能漂浮？代价由谁承担？", "下面的人如何看待这座高处的奇迹？"),
        hooks=("破损空港", "坠落的天使机器", "禁飞命令"),
        icon_name="天空圣国",
    ),
    _core_seed(
        name="企业星城",
        archetype="被公司统治的城市",
        tags=("techno_pressure",),
        brief="城市用广告、债务、安保和灵魂能源维持秩序；反派更像完美领袖而非怪物。",
        use_when="世界中出现公司、财阀、工业污染、上层/下层、能源垄断或媒体操控。",
        questions=("这座城市向居民许诺了什么？", "谁看起来受益，谁在账单背面付出代价？"),
        hooks=("停电的下层街区", "公开演讲", "秘密实验楼"),
        icon_name="企业星城",
    ),
    _core_seed(
        name="风铃村",
        archetype="可反复回访的温柔村庄",
        tags=("natural_home",),
        brief="村庄是一张关系网；每次回访都应有季节、居民、传闻或伤痕的变化。",
        use_when="世界中出现同乡英雄、家园、村庄、年轻主角、导师或邻里关系。",
        questions=("这里最平凡、最值得珍惜的日常是什么？", "黑暗慢慢靠近时，第一个异样会出现在哪里？"),
        hooks=("失踪的学徒", "坏掉的水车", "祖辈留下的预言"),
        icon_name="风铃村",
    ),
    _core_seed(
        name="潮烛岛",
        archetype="被旧事纠缠的海岛",
        tags=("natural_home", "ocean_roads", "dungeon_mystery"),
        brief="阳光、海湾和灯塔之下藏着无法安息的旧承诺，温情与诡异共存。",
        use_when="世界中出现海湾、群岛、灯塔、幽灵、失踪船只或家族秘密。",
        questions=("岛民不愿提起哪一晚？", "潮水退去时会露出什么不该存在的路？"),
        hooks=("闹鬼灯塔", "退潮密道", "海边的无名墓"),
        icon_name="潮烛岛",
    ),
    _core_seed(
        name="蔚蓝深林",
        archetype="永恒森林",
        tags=("natural_home", "dungeon_mystery"),
        brief="森林的美丽与危险同样真实；深处的古代设施可能正在破坏自然循环。",
        use_when="世界中出现巨木、自然灵、古代遗迹、变异植物或生态失衡。",
        questions=("这片森林如何照顾附近居民？", "它最近为什么开始拒绝熟悉它的人？"),
        hooks=("不会腐烂的落叶", "沉睡的机械兽", "林中导师"),
        icon_name="蔚蓝深林",
    ),
    _core_seed(
        name="雪眩峰",
        archetype="冰封山脉",
        tags=("natural_home", "epic_myth"),
        brief="山峰隔绝世界，也保存世界；适合放置古老誓言、巨兽、隐修者或冰封真相。",
        use_when="世界中出现高山、边境、朝圣、龙、冰封神庙或无法绕行的旅途。",
        questions=("登上山顶会证明什么？", "谁选择留在冰雪里守住一个错误？"),
        hooks=("雪崩命刻", "冰中遗迹", "山民的试炼"),
        icon_name="雪眩峰",
    ),
)
