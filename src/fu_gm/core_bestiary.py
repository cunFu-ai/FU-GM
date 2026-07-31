from __future__ import annotations

from dataclasses import dataclass, field, replace

from fu_gm.models import Affinity, StatusEffect


DAMAGE_ORDER: tuple[str, ...] = ("physical", "wind", "lightning", "dark", "earth", "fire", "ice", "light", "poison")


@dataclass(frozen=True)
class BestiaryAttack:
    name: str
    range_type: str
    attributes: tuple[str, str]
    damage_bonus: int
    damage_type: str
    accuracy_modifier: int = 0
    effect: str = ""

    @property
    def summary(self) -> str:
        modifier = f"+{self.accuracy_modifier}" if self.accuracy_modifier else ""
        effect = f"，{self.effect}" if self.effect else ""
        return f"{self.range_type}{self.name}·【{self.attributes[0]}+{self.attributes[1]}】{modifier}·【高值+{self.damage_bonus}】{self.damage_type}{effect}"


@dataclass(frozen=True)
class BestiarySpell:
    name: str
    attributes: tuple[str, str] | tuple[()] = ()
    mp_cost: int = 0
    target: str = ""
    duration: str = "瞬发"
    effect: str = ""


@dataclass(frozen=True)
class BestiaryEntry:
    name: str
    level: int
    species: str
    description: str
    typical_traits: tuple[str, ...]
    attributes: dict[str, int]
    max_hp: int
    crisis: int
    max_mp: int
    initiative: int
    physical_defense_bonus: int = 0
    magic_defense_bonus: int = 0
    fixed_physical_defense: int | None = None
    affinities: dict[str, Affinity] = field(default_factory=dict)
    attacks: tuple[BestiaryAttack, ...] = ()
    spells: tuple[BestiarySpell, ...] = ()
    other_actions: tuple[str, ...] = ()
    traits_rules: tuple[str, ...] = ()
    status_immunities: tuple[StatusEffect, ...] = ()
    source_note: str = "核心规则书生物图鉴示例"

    def affinity_text(self) -> str:
        labels = {
            Affinity.WEAK: "弱",
            Affinity.RESIST: "抗",
            Affinity.IMMUNE: "免",
            Affinity.ABSORB: "吸",
        }
        parts = []
        for damage_type in DAMAGE_ORDER:
            affinity = self.affinities.get(damage_type, Affinity.NORMAL)
            if affinity != Affinity.NORMAL:
                parts.append(f"{damage_type}:{labels[affinity]}")
        return "、".join(parts) or "无特殊相性"

    def bestiary_header(self) -> str:
        return f"{self.name}\n{self.level}级·{self.species}"


def atk(
    name: str,
    range_type: str,
    attributes: tuple[str, str],
    damage_bonus: int,
    damage_type: str = "physical",
    *,
    accuracy_modifier: int = 0,
    effect: str = "",
) -> BestiaryAttack:
    return BestiaryAttack(name, range_type, attributes, damage_bonus, damage_type, accuracy_modifier, effect)


def spell(
    name: str,
    *,
    attributes: tuple[str, str] | tuple[()] = (),
    mp_cost: int = 0,
    target: str = "",
    duration: str = "瞬发",
    effect: str = "",
) -> BestiarySpell:
    return BestiarySpell(name, attributes, mp_cost, target, duration, effect)


def aff(**items: str) -> dict[str, Affinity]:
    aliases = {
        "weak": Affinity.WEAK,
        "resist": Affinity.RESIST,
        "immune": Affinity.IMMUNE,
        "absorb": Affinity.ABSORB,
        "弱": Affinity.WEAK,
        "抗": Affinity.RESIST,
        "免": Affinity.IMMUNE,
        "吸": Affinity.ABSORB,
    }
    return {key: aliases[value] for key, value in items.items()}


def entry(
    name: str,
    level: int,
    species: str,
    traits: tuple[str, ...],
    attrs: tuple[int, int, int, int],
    hp: int,
    crisis: int,
    mp: int,
    initiative: int,
    *,
    description: str = "",
    pdef: int = 0,
    mdef: int = 0,
    fixed_pdef: int | None = None,
    affinities: dict[str, Affinity] | None = None,
    status_immunities: tuple[StatusEffect, ...] = (),
    rules: tuple[str, ...] = (),
) -> BestiaryEntry:
    return BestiaryEntry(
        name=name,
        level=level,
        species=species,
        description=description or "核心规则书生物图鉴条目，已结构化基础数值；详细行动可继续补充。",
        typical_traits=traits,
        attributes={"DEX": attrs[0], "INS": attrs[1], "MIG": attrs[2], "WLP": attrs[3]},
        max_hp=hp,
        crisis=crisis,
        max_mp=mp,
        initiative=initiative,
        physical_defense_bonus=pdef,
        magic_defense_bonus=mdef,
        fixed_physical_defense=fixed_pdef,
        affinities=affinities or {},
        status_immunities=status_immunities,
        traits_rules=rules,
    )


_CORE_BESTIARY_BASE_ENTRIES: tuple[BestiaryEntry, ...] = (
    BestiaryEntry(
        name="巨齿百足虫",
        level=5,
        species="野兽",
        description="巨大的蜈蚣，能蜷成球形抵御攻击，随后突然暴起撕咬对手。",
        typical_traits=("沉重", "坚韧", "迟缓", "领地意识"),
        attributes={"DEX": 8, "INS": 6, "MIG": 10, "WLP": 8},
        max_hp=60,
        crisis=30,
        max_mp=45,
        initiative=7,
        physical_defense_bonus=2,
        magic_defense_bonus=1,
        affinities=aff(physical="抗", lightning="抗", ice="弱"),
        attacks=(
            atk("巨颚横斩", "近战", ("DEX", "MIG"), 5, "poison", effect="对目标施加虚弱"),
            atk("曲面切割", "近战", ("DEX", "MIG"), 5, "physical", effect="若上回合执行防御行动，造成 5 点额外伤害"),
        ),
        traits_rules=("蜷缩：执行防御行动后，对物理伤害免疫，直到下个回合开始。",),
    ),
    BestiaryEntry(
        name="硕鼠",
        level=5,
        species="野兽",
        description="栖息在下水道和隧道中的巨大老鼠，撕咬会造成严重发热症状，火焰能轻易吓退它们。",
        typical_traits=("畏火", "迅捷", "饥饿", "凶狠"),
        attributes={"DEX": 12, "INS": 8, "MIG": 6, "WLP": 6},
        max_hp=40,
        crisis=20,
        max_mp=35,
        initiative=14,
        affinities=aff(physical="抗", fire="弱"),
        attacks=(atk("恶毒撕咬", "近战", ("DEX", "MIG"), 5, "physical", effect="对目标施加中毒"),),
        traits_rules=("鼠急跳墙：危机状态下所有检定 +3。", "疾速：逃跑或追击命刻额外填充或擦除 1 格。"),
    ),
    BestiaryEntry(
        name="灰嚎怪",
        level=5,
        species="野兽",
        description="大型犬科动物，常被训练成守卫战兽，对主人和同伴极为忠诚。",
        typical_traits=("忠诚", "敏锐", "聪明", "警觉"),
        attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
        max_hp=50,
        crisis=25,
        max_mp=45,
        initiative=9,
        affinities=aff(physical="抗", ice="弱"),
        attacks=(atk("凶猛撕咬", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=3),),
        spells=(spell("集结嚎叫", mp_cost=10, target="至多三个生物", duration="场景", effect="目标命中检定 +1。"),),
        traits_rules=("绝对忠诚：可代替另一个遇险生物承受效果，类似挺身守护。",),
    ),
    BestiaryEntry(
        name="吸血蝙蝠",
        level=5,
        species="野兽",
        description="超大号掠食动物，经常袭击人和牲畜，并具有不可思议的智力。",
        typical_traits=("畏光", "好斗", "嘈杂", "聪明"),
        attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 8},
        max_hp=50,
        crisis=25,
        max_mp=45,
        initiative=9,
        affinities=aff(physical="弱", dark="抗", poison="抗"),
        attacks=(
            atk("吸血", "近战", ("DEX", "DEX"), 5, "physical", effect="恢复等同于目标损失 HP 一半的 HP"),
            atk("刺耳尖啸", "远程", ("DEX", "WLP"), 5, "wind", effect="对目标施加眩晕"),
        ),
        traits_rules=("飞行。",),
    ),
    BestiaryEntry(
        name="轰炮蚁",
        level=10,
        species="野兽",
        description="有人类大小的蚂蚁，是蚁后意志的延伸。",
        typical_traits=("爆炸", "易燃", "无心智", "领地意识"),
        attributes={"DEX": 10, "INS": 6, "MIG": 10, "WLP": 6},
        max_hp=70,
        crisis=35,
        max_mp=40,
        initiative=12,
        affinities=aff(physical="抗", fire="弱", poison="抗"),
        attacks=(
            atk("巨蚁冲撞", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1),
            atk("蚁力炮击", "远程", ("DEX", "INS"), 5, "physical", accuracy_modifier=1, effect="对目标施加眩晕"),
        ),
        status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED),
        traits_rules=("掘地：在泥地、沙地或岩石上防御时物防 +2，并对土系伤害获得弱点，直到下回合开始。",),
    ),
    BestiaryEntry(
        name="棘刺鱼",
        level=10,
        species="野兽",
        description="仅比伸直的人手略长，能用鱼鳍短暂飞跃一段距离后凶狠撕咬。",
        typical_traits=("好斗", "迅捷", "小巧", "厚皮"),
        attributes={"DEX": 10, "INS": 10, "MIG": 6, "WLP": 6},
        max_hp=50,
        crisis=25,
        max_mp=40,
        initiative=14,
        affinities=aff(physical="弱", lightning="抗", earth="抗", ice="抗", poison="抗"),
        attacks=(
            atk("棘刺俯冲", "近战", ("DEX", "DEX"), 10, "physical", accuracy_modifier=1),
            atk("海洋喷流", "远程", ("DEX", "INS"), 5, "ice", accuracy_modifier=1, effect="对目标施加迟缓"),
        ),
        traits_rules=("飞行。",),
    ),
    BestiaryEntry(
        name="日光熊",
        level=15,
        species="野兽",
        description="世界上最庞大且最具智慧的野兽之一，据说某些日光熊能用心灵感应交流。",
        typical_traits=("多毛", "巨大", "和平", "聪明"),
        attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
        max_hp=80,
        crisis=40,
        max_mp=45,
        initiative=8,
        physical_defense_bonus=1,
        magic_defense_bonus=2,
        affinities=aff(physical="抗", lightning="弱", dark="弱", ice="抗", poison="抗"),
        attacks=(atk("熊抱", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1, effect="对目标施加虚弱"),),
        status_immunities=(StatusEffect.SLOW, StatusEffect.WEAKENED),
        traits_rules=("阳炎之体：危机状态下熊抱造成 5 点额外伤害，且全部变为火系。",),
    ),
    BestiaryEntry(
        name="白嚎怪",
        level=20,
        species="野兽",
        description="喜欢在山区和森林游荡的奇珍异兽。",
        typical_traits=("勇敢", "狡猾", "威严", "警觉"),
        attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
        max_hp=90,
        crisis=45,
        max_mp=60,
        initiative=8,
        physical_defense_bonus=2,
        magic_defense_bonus=1,
        affinities=aff(wind="抗", ice="弱", poison="抗"),
        attacks=(atk("苍白之喉", "近战", ("DEX", "MIG"), 15, "physical", accuracy_modifier=5, effect="对目标施加虚弱"),),
        spells=(
            spell("冰山", attributes=("MIG", "WLP"), mp_cost=20, target="一个生物", effect="造成【高值+30】冰系伤害，无视抵抗相性。"),
            spell("舔舐伤口", mp_cost=5, target="自身", effect="恢复 30 HP；40级起恢复 40，60级起恢复 50。"),
        ),
        traits_rules=("威严光环：白嚎怪的盟友免疫动摇。",),
    ),
    BestiaryEntry(
        name="魔法提灯",
        level=5,
        species="构装体",
        description="法师常用作魔力储存器，紧急时也能充当战力。",
        typical_traits=("发光", "乐于相助", "魔法造物", "微小"),
        attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
        max_hp=40,
        crisis=20,
        max_mp=55,
        initiative=8,
        physical_defense_bonus=1,
        magic_defense_bonus=2,
        affinities=aff(physical="弱", earth="抗", poison="免"),
        attacks=(atk("元素释放", "远程", ("DEX", "INS"), 5, "variable", effect="掷 d6 决定伤害类型：1-2 雷，3-4 火，5-6 冰"),),
        other_actions=("传递魔力：消耗至多 10 MP，让一个可见生物恢复等量 MP。",),
        status_immunities=(StatusEffect.POISONED,),
        traits_rules=("构装体：免疫中毒。",),
    ),
    BestiaryEntry(
        name="碎响小丑",
        level=10,
        species="构装体",
        description="被人丢弃后又成了恶灵附身的躯壳，也许只是想给自己找玩伴。",
        typical_traits=("骇人", "吵闹", "小巧", "心怀仇恨"),
        attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
        max_hp=60,
        crisis=30,
        max_mp=50,
        initiative=13,
        affinities=aff(physical="弱", earth="抗", poison="免"),
        attacks=(atk("小丑飞踢", "近战", ("DEX", "INS"), 5, "physical", accuracy_modifier=1, effect="对眩晕或动摇目标造成 5 点额外伤害"),),
        spells=(spell("舞动小丑", mp_cost=20, target="特殊", effect="对能看见的所有敌人施加迟缓。"),),
        status_immunities=(StatusEffect.POISONED,),
        traits_rules=("构装体：免疫中毒。",),
    ),
    BestiaryEntry(
        name="石像鬼",
        level=10,
        species="构装体",
        description="身体沉重，但能借部分魔力漂浮，适合作为守卫。",
        typical_traits=("定于一处", "狡猾", "漂浮", "警觉"),
        attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
        max_hp=70,
        crisis=35,
        max_mp=55,
        initiative=9,
        affinities=aff(physical="弱", wind="抗", lightning="抗", earth="抗", ice="弱", poison="免"),
        attacks=(atk("石质利爪", "近战", ("DEX", "MIG"), 5, "physical", accuracy_modifier=1, effect="攻击目标魔防"),),
        spells=(spell("碎石弹幕", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】土系伤害，并施加眩晕。"),),
        status_immunities=(StatusEffect.POISONED,),
        traits_rules=("构装体：免疫中毒。", "飞行。"),
    ),
    BestiaryEntry(
        name="魔导机兵",
        level=10,
        species="构装体",
        description="由灵魂能量驱动的铠甲，战斗威力巨大但策略容易被预判。",
        typical_traits=("忠诚", "死板", "无情", "警觉"),
        attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
        max_hp=80,
        crisis=40,
        max_mp=40,
        initiative=5,
        fixed_physical_defense=11,
        affinities=aff(physical="弱", lightning="弱", earth="抗", fire="抗", ice="抗", poison="免"),
        attacks=(atk("战斧劈砍", "近战", ("MIG", "MIG"), 14, "physical", accuracy_modifier=1, effect="对目标施加迟缓"),),
        status_immunities=(StatusEffect.POISONED,),
        traits_rules=("构装体：免疫中毒。", "核心暴露：危机时失去火/冰抗性，战斧劈砍变为雷系。"),
    ),
    BestiaryEntry(
        name="青铜魔像",
        level=15,
        species="构装体",
        description="齿轮驱动的自动机械，常被工匠和商人用作守卫。",
        typical_traits=("哐当作响", "庞然大物", "身强力壮", "迟缓"),
        attributes={"DEX": 6, "INS": 8, "MIG": 12, "WLP": 6},
        max_hp=100,
        crisis=50,
        max_mp=45,
        initiative=7,
        physical_defense_bonus=2,
        magic_defense_bonus=1,
        affinities=aff(physical="弱", fire="弱", earth="抗", poison="免"),
        attacks=(
            atk("拳击", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1, effect="对目标施加眩晕"),
            atk("旋风", "近战", ("DEX", "MIG"), 5, "wind", accuracy_modifier=1, effect="多重攻击(2)"),
        ),
        status_immunities=(StatusEffect.POISONED,),
        traits_rules=("构装体：免疫中毒。",),
    ),
    BestiaryEntry(
        name="锋翼鸟",
        level=15,
        species="构装体",
        description="搭载多种强力魔导兵器，常出现在大型帝国空军部队中。",
        typical_traits=("迅捷", "飞行", "全副武装", "忠诚"),
        attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
        max_hp=80,
        crisis=40,
        max_mp=45,
        initiative=9,
        affinities=aff(physical="弱", wind="弱", earth="抗", ice="免", poison="免"),
        attacks=(
            atk("锋翼俯冲", "近战", ("DEX", "DEX"), 10, "physical", accuracy_modifier=1, effect="攻击后失去飞行增益直到下回合开始"),
            atk("加特林机枪", "远程", ("DEX", "INS"), 5, "physical", accuracy_modifier=1, effect="多重攻击(2)"),
            atk("焦炎火箭", "远程", ("DEX", "INS"), 10, "fire", accuracy_modifier=1),
        ),
        status_immunities=(StatusEffect.SLOW, StatusEffect.WEAKENED, StatusEffect.POISONED),
        traits_rules=("构装体：免疫中毒。", "迅捷而勇猛：免疫迟缓和虚弱。", "飞行。"),
    ),
    entry(
        "森林魔像", 20, "构装体", ("冷漠", "易燃", "孤僻", "高耸"), (6, 6, 12, 10), 110, 55, 80, 6,
        affinities=aff(physical="抗", lightning="抗", earth="抗", fire="弱", ice="弱", poison="免"),
        status_immunities=(StatusEffect.POISONED,),
        rules=("构装体：免疫中毒。",),
    ),
    entry("小鬼", 5, "恶魔", ("怯懦", "狡猾", "顽劣", "堕落"), (8, 8, 6, 10), 50, 25, 55, 8),
    entry("电光轮", 10, "恶魔", ("刺眼", "尖啸", "残忍", "迅捷"), (12, 6, 6, 8), 60, 30, 60, 9),
    entry("影嚎怪", 15, "恶魔", ("恐怖", "硕大", "静默", "超自然"), (8, 6, 10, 8), 80, 40, 55, 7, pdef=1, mdef=2),
    entry("蛇足女妖", 20, "恶魔", ("聪慧", "博学", "滑行", "难以揣测"), (8, 10, 6, 10), 70, 35, 80, 9),
    entry(
        "橡实妖精", 5, "元素", ("好奇", "发光", "友善", "活泼"), (10, 6, 6, 10), 40, 20, 55, 8,
        status_immunities=(StatusEffect.POISONED,), rules=("元素：免疫中毒。", "飞行。"),
    ),
    entry(
        "混沌裂片", 5, "元素", ("异种", "饥饿", "小巧", "散布黑暗"), (8, 10, 8, 6), 50, 25, 35, 9,
        status_immunities=(StatusEffect.POISONED, StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN),
        rules=("元素：免疫中毒。", "空洞心灵：免疫眩晕、激怒和动摇。"),
    ),
    entry(
        "爆炎元素", 10, "元素", ("明亮", "激动", "灼热", "不稳定"), (8, 6, 8, 10), 60, 30, 60, 7,
        pdef=1, mdef=2, status_immunities=(StatusEffect.POISONED,),
        rules=("元素：免疫中毒。", "引爆：HP 归零时爆炸；若因冰系伤害归零则不会爆炸。"),
    ),
    entry(
        "静电软泥怪", 10, "元素", ("饥饿", "迟缓", "柔软", "静电"), (6, 6, 10, 10), 70, 35, 70, 6,
        pdef=1, mdef=2, status_immunities=(StatusEffect.POISONED,), rules=("元素：免疫中毒。",),
    ),
    entry(
        "宁芙", 15, "元素", ("迅捷", "领地意识", "谨慎", "睿智"), (8, 10, 6, 8), 70, 35, 65, 9,
        status_immunities=(StatusEffect.POISONED,), rules=("元素：免疫中毒。",),
    ),
    entry(
        "尖刺雪花", 15, "元素", ("畏热", "漂浮", "冰冷", "旋转"), (8, 10, 6, 8), 70, 35, 65, 9,
        pdef=1, mdef=2, status_immunities=(StatusEffect.POISONED,), rules=("元素：免疫中毒。",),
    ),
    entry(
        "岩躯野猪", 20, "元素", ("破坏者", "暴躁", "巨大", "岩石躯体"), (8, 6, 12, 8), 110, 55, 60, 7,
        status_immunities=(StatusEffect.POISONED,), rules=("元素：免疫中毒。",),
    ),
    entry("强盗", 5, "人型", ("虚张声势", "暴躁", "骄傲", "强壮"), (6, 8, 10, 8), 60, 30, 45, 10, pdef=3, mdef=1),
    entry("守卫", 5, "人型", ("勇敢", "守纪", "忠诚", "训练有素"), (8, 8, 8, 8), 60, 30, 45, 5, fixed_pdef=11),
    entry("狗头人斥候", 5, "人型", ("迅捷", "多毛", "敏锐", "小巧"), (10, 8, 6, 8), 40, 20, 45, 8, pdef=1, mdef=1),
    entry("狗头人巫师", 5, "人型", ("诡秘", "多毛", "小巧", "睿智"), (8, 8, 6, 10), 50, 25, 75, 6, pdef=1, mdef=2),
    entry("蜂巢族", 10, "人型", ("嗡鸣", "勤劳", "热爱美学", "隐秘"), (10, 8, 8, 6), 70, 35, 50, 11, pdef=1, mdef=2, rules=("飞行。",)),
    entry("雇佣兵", 10, "人型", ("强悍", "冷酷", "倦怠", "无情"), (8, 8, 8, 8), 60, 30, 50, 5, fixed_pdef=11, mdef=1),
    entry("狙击手", 15, "人型", ("精准", "守纪", "隐蔽", "善于观察"), (10, 10, 6, 6), 60, 30, 45, 13, pdef=1, mdef=1),
    entry("战斗法师", 20, "人型", ("雄心勃勃", "狡猾", "守纪", "博学"), (8, 8, 8, 10), 80, 40, 90, 9, fixed_pdef=11, mdef=1),
    entry("猫妖", 5, "怪物", ("好奇", "活泼", "聪明", "小巧"), (8, 8, 6, 10), 40, 20, 65, 12, pdef=1, mdef=2),
    entry("恐怖飞蛾", 5, "怪物", ("骇人", "飞行", "多毛", "难闻"), (10, 6, 8, 8), 60, 30, 55, 8, rules=("飞行。",)),
    entry("圆润软泥怪", 5, "怪物", ("发光", "柔软", "巨可爱", "温暖"), (8, 6, 10, 8), 60, 30, 55, 7, pdef=1, mdef=2),
    entry("龙兽", 10, "怪物", ("沉重", "饥饿", "懒惰", "长有鳞片"), (8, 8, 10, 6), 70, 35, 50, 8, pdef=2, mdef=1),
    entry("魔眼", 10, "怪物", ("狡猾", "催眠", "静默", "有翼"), (10, 6, 8, 8), 60, 30, 60, 12, rules=("飞行。",)),
    entry("浮空水母", 10, "怪物", ("漂浮", "发光", "静默", "透明"), (8, 8, 10, 6), 70, 35, 40, 8, pdef=1, mdef=2, rules=("飞行。",)),
    entry("鸡蛇怪", 15, "怪物", ("敏捷", "小巧", "难闻", "难以预料"), (8, 10, 8, 6), 70, 35, 45, 9, pdef=1, mdef=2),
    entry("拟形怪", 15, "怪物", ("狡猾", "无定型", "贪婪", "隐秘"), (10, 8, 8, 6), 70, 35, 45, 9, pdef=1, mdef=2, rules=("改变形体：处在变形状态时几乎与模仿物体一样，GM 必须描述少数异常细节。",)),
    entry("曼德拉草", 5, "植物", ("骇人", "迅捷", "恶毒", "小巧"), (10, 8, 6, 8), 50, 25, 45, 9, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("诅咒南瓜", 5, "植物", ("愤怒", "腐烂", "小巧", "难闻"), (8, 8, 8, 8), 50, 25, 55, 8, pdef=1, mdef=2, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("缠根藤", 10, "植物", ("好斗", "诅咒之物", "迅捷", "多刺"), (10, 8, 8, 6), 60, 30, 40, 9, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("幻菇人", 10, "植物", ("无眼", "喜欢泥地", "和平", "迟缓"), (6, 8, 10, 8), 70, 35, 60, 7, pdef=2, mdef=1, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("仙人掌巨魔", 15, "植物", ("骇人", "巨硕", "领地意识", "对水敏感"), (8, 6, 12, 6), 90, 45, 55, 7, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("陷龙花", 20, "植物", ("超巨型", "饥饿", "耐心", "植根于地"), (8, 8, 10, 8), 90, 45, 60, 8, status_immunities=(StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("植物：免疫眩晕、激怒和动摇。",)),
    entry("惧灵瓮", 5, "不死族", ("尖笑", "脆弱", "漂浮", "微小"), (10, 6, 6, 10), 50, 25, 55, 8, pdef=1, mdef=2, status_immunities=(StatusEffect.POISONED,), rules=("不死族：免疫中毒，生命值恢复效果可对其造成伤害。",)),
    entry("丧尸", 5, "不死族", ("骇人", "无心智", "腐烂", "迟缓"), (6, 6, 12, 8), 70, 35, 45, 6, pdef=2, mdef=1, status_immunities=(StatusEffect.POISONED, StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("空洞心灵：免疫眩晕、激怒和动摇。", "不死族：免疫中毒，生命值恢复效果可对其造成伤害。")),
    entry("骷髅法师", 10, "不死族", ("野心勃勃", "聪慧", "博学", "无情"), (6, 8, 8, 10), 60, 30, 70, 5, pdef=2, mdef=4, status_immunities=(StatusEffect.POISONED,), rules=("不死族：免疫中毒，生命值恢复效果可对其造成伤害。",)),
    entry("骷髅士兵", 10, "不死族", ("残忍", "无心智", "嗜杀", "静默"), (8, 8, 10, 6), 70, 35, 40, 6, fixed_pdef=12, status_immunities=(StatusEffect.POISONED, StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("空洞心灵：免疫眩晕、激怒和动摇。", "不死族：免疫中毒，生命值恢复效果可对其造成伤害。")),
    entry("骨嚎怪", 15, "不死族", ("永恒饥渴", "迅捷", "坚韧", "凶狠"), (10, 6, 10, 6), 80, 40, 55, 8, status_immunities=(StatusEffect.POISONED,), rules=("不死族：免疫中毒，生命值恢复效果可对其造成伤害。",)),
    entry("食尸鬼", 15, "不死族", ("好斗", "硕大", "身强力壮", "恐怖"), (8, 6, 12, 6), 90, 45, 45, 7, status_immunities=(StatusEffect.POISONED,), rules=("不死族：免疫中毒，生命值恢复效果可对其造成伤害。",)),
    entry("木乃伊", 20, "不死族", ("诅咒之物", "永恒忠诚", "易燃", "静默"), (6, 8, 10, 10), 90, 45, 70, 7, status_immunities=(StatusEffect.POISONED, StatusEffect.DAZED, StatusEffect.ENRAGED, StatusEffect.SHAKEN), rules=("古老诅咒：HP 归零时化为尘埃，并对所有活物施加动摇和虚弱。", "空洞心灵：免疫眩晕、激怒和动摇。", "不死族：免疫中毒，生命值恢复效果可对其造成伤害。")),
    entry("禁锢之魂", 20, "不死族", ("深陷痛苦", "诅咒之物", "灵体", "复仇心切"), (12, 8, 6, 8), 70, 35, 70, 10, status_immunities=(StatusEffect.POISONED,), rules=("不死族：免疫中毒，生命值恢复效果可对其造成伤害。",)),
)


_BESTIARY_DETAIL_OVERLAY: dict[str, dict[str, object]] = {
    "森林魔像": {
        "attacks": (
            atk("树皮利爪", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=2, effect="多重攻击(2)"),
            atk("生命冲击", "远程", ("DEX", "MIG"), 15, "light", accuracy_modifier=2),
        ),
        "spells": (
            spell("驱散魔法", mp_cost=10, target="一个生物", effect="移除目标身上一个或多个持续时间为“场景”的法术效果。"),
            spell("孢子之息", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】毒系伤害，并施加虚弱。"),
            spell("藤蔓暴生", mp_cost=20, target="特殊", effect="对能看见的所有敌人施加动摇。"),
        ),
    },
    "小鬼": {
        "affinities": aff(dark="抗", fire="免", ice="抗", light="弱"),
        "attacks": (atk("寒冰爪击", "近战", ("DEX", "WLP"), 5, "ice", effect="攻击目标魔防"),),
        "spells": (
            spell("激怒", attributes=("INS", "WLP"), mp_cost=10, target="一个生物", effect="对目标施加激怒，且目标下回合不能执行防御行动。"),
            spell("硬化躯壳", mp_cost=10, target="自身", duration="场景", effect="对物理伤害获得抵抗。"),
        ),
    },
    "电光轮": {
        "affinities": aff(lightning="吸", dark="抗", earth="弱"),
        "attacks": (atk("急转弯", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1),),
        "spells": (spell("闪电击", attributes=("INS", "WLP"), mp_cost=10, target="至多三个生物", effect="对每个目标造成【高值+15】雷系伤害；机会：对每个目标施加眩晕。"),),
        "traits_rules": ("疾速：逃跑或追击命刻额外填充或擦除 1 格。",),
    },
    "影嚎怪": {
        "affinities": aff(lightning="弱", dark="抗", fire="抗"),
        "attacks": (atk("幽魂撕咬", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=4, effect="攻击目标魔防"),),
        "spells": (
            spell("余烬之息", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】火系伤害，并施加虚弱。"),
            spell("悲丧厉嚎", attributes=("MIG", "WLP"), mp_cost=5, target="一个生物", effect="对目标施加动摇。"),
        ),
    },
    "蛇足女妖": {
        "affinities": aff(physical="弱", dark="抗", fire="免", ice="抗", light="弱"),
        "attacks": (
            atk("毒蛇缠绕", "近战", ("DEX", "INS"), 15, "poison", accuracy_modifier=5),
            atk("冰冷凝视", "远程", ("DEX", "WLP"), 10, "ice", accuracy_modifier=5, effect="目标下回合无法进行推进目标行动；攻击目标魔防"),
        ),
        "spells": (spell("化脑术", attributes=("INS", "WLP"), mp_cost=5, target="至多三个生物", effect="每个目标失去【高值+10】MP。"),),
        "traits_rules": ("舌灿莲花：与交涉相关的对抗检定 +3。",),
    },
    "橡实妖精": {
        "affinities": aff(physical="弱", dark="弱", earth="免", light="免", poison="免"),
        "attacks": (atk("犀利针刺", "近战", ("DEX", "DEX"), 5, "physical", effect="对迟缓目标造成 5 点额外伤害"),),
        "spells": (
            spell("纠缠", attributes=("INS", "WLP"), mp_cost=5, target="一个生物", effect="对目标施加迟缓。"),
            spell("治愈术", mp_cost=10, target="至多三个生物", effect="每个目标恢复 40 HP；20级起 50，40级起 60。"),
        ),
    },
    "混沌裂片": {
        "affinities": aff(physical="弱", dark="免", fire="抗", ice="抗", light="弱", poison="免"),
        "attacks": (atk("混沌放射", "远程", ("DEX", "INS"), 5, "dark", effect="对目标施加虚弱"),),
    },
    "爆炎元素": {
        "affinities": aff(earth="弱", fire="吸", ice="弱", poison="免"),
        "attacks": (atk("火焰射流", "近战", ("DEX", "WLP"), 10, "fire", accuracy_modifier=1, effect="目标失去火系抵抗，直到爆炎元素下回合结束"),),
    },
    "静电软泥怪": {
        "affinities": aff(physical="抗", lightning="吸", earth="弱", fire="抗", poison="免"),
        "attacks": (atk("软泥猛砸", "近战", ("DEX", "MIG"), 5, "physical", accuracy_modifier=1),),
        "spells": (spell("静电波", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】雷系伤害，并施加迟缓。"),),
    },
    "宁芙": {
        "affinities": aff(dark="弱", earth="免", fire="抗", ice="抗", poison="免"),
        "attacks": (atk("四季之触", "近战", ("DEX", "INS"), 10, "wind", accuracy_modifier=1, effect="春眩晕、夏虚弱、秋迟缓、冬动摇"),),
        "spells": (spell("飞叶漩涡", attributes=("INS", "WLP"), mp_cost=5, target="一个生物", effect="造成【高值+10】物理伤害。"),),
    },
    "尖刺雪花": {
        "affinities": aff(lightning="弱", fire="弱", ice="吸", poison="免"),
        "attacks": (atk("霜冻噬咬", "近战", ("DEX", "INS"), 5, "ice", accuracy_modifier=1),),
        "spells": (spell("霜风吐息", attributes=("INS", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】冰系伤害，并施加虚弱。"),),
    },
    "岩躯野猪": {
        "affinities": aff(physical="弱", lightning="抗", dark="弱", earth="免", fire="抗", ice="抗", poison="免"),
        "attacks": (
            atk("巨岩暴冲", "近战", ("DEX", "MIG"), 15, "physical", accuracy_modifier=2, effect="多重攻击(2)；若未命中至少一个目标，自身失去 20 HP"),
            atk("石牙", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=2),
        ),
        "spells": (
            spell("岩石弹幕", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+20】土系伤害，并施加眩晕。"),
            spell("地动", attributes=("MIG", "WLP"), mp_cost=10, target="至多三个生物", effect="对地面目标造成【高值+20】土系伤害；机会：目标下回合少执行一次行动。"),
        ),
        "other_actions": ("愤怒鼻息：下个回合必须进行巨岩暴冲，且所有被命中的目标被施加动摇。",),
    },
    "强盗": {
        "affinities": aff(earth="抗", ice="抗", poison="弱"),
        "attacks": (
            atk("路霸之斧", "近战", ("MIG", "MIG"), 10, "physical"),
            atk("蛮力肘击", "近战", ("DEX", "MIG"), 5, "physical", effect="目标失去 10 MP"),
        ),
        "traits_rules": ("暴躁脾气：每场景首次进入危机时，解除自身除激怒以外的所有异常状态。",),
    },
    "守卫": {
        "affinities": aff(physical="抗", lightning="弱", light="抗"),
        "attacks": (
            atk("重矛", "近战", ("DEX", "MIG"), 12, "physical"),
            atk("十字弩", "远程", ("DEX", "INS"), 8, "physical"),
        ),
        "traits_rules": ("防御阵形：若有其他守卫参与冲突，物防和魔防 +1。", "时刻警戒：可代替另一个遇险生物承受效果，类似挺身守护。"),
    },
    "狗头人斥候": {
        "affinities": aff(physical="抗", dark="弱", fire="抗", ice="抗", poison="抗"),
        "attacks": (
            atk("钢匕首", "近战", ("DEX", "INS"), 4, "physical", accuracy_modifier=1, effect="对处于异常状态的目标造成 5 点额外伤害"),
            atk("投掷石块", "远程", ("DEX", "MIG"), 5, "physical", effect="对目标施加眩晕"),
        ),
    },
    "狗头人巫师": {
        "affinities": aff(physical="弱", wind="抗", light="抗"),
        "attacks": (atk("橡木杖", "近战", ("WLP", "WLP"), 6, "physical", effect="攻击目标魔防"),),
        "spells": (
            spell("觉醒", mp_cost=20, target="一个生物", duration="场景", effect="选择敏捷、洞察、力量或意志之一，目标该属性骰提升一级，最高 d12。"),
            spell("恶臭之息", attributes=("INS", "WLP"), mp_cost=5, target="一个生物", effect="造成【高值+10】毒系伤害。"),
        ),
    },
    "蜂巢族": {
        "affinities": aff(physical="抗", fire="弱", poison="抗"),
        "attacks": (atk("蜂巢利刃", "近战", ("DEX", "INS"), 10, "physical", accuracy_modifier=5),),
        "spells": (spell("蜂之舞", mp_cost=20, target="一个生物", effect="目标用当前装备武器进行一次顺势攻击；NPC 则进行一次基础攻击。"),),
    },
    "雇佣兵": {
        "affinities": aff(physical="抗", earth="弱", fire="抗"),
        "attacks": (
            atk("青铜剑", "近战", ("DEX", "MIG"), 11, "physical", accuracy_modifier=5),
            atk("手枪", "远程", ("DEX", "INS"), 8, "physical", accuracy_modifier=4),
        ),
        "other_actions": ("攻击蓄力：下次攻击具有多重攻击(2)，并无视抵抗相性。",),
    },
    "狙击手": {
        "affinities": aff(lightning="抗", fire="抗", ice="弱"),
        "attacks": (
            atk("匕首", "近战", ("DEX", "INS"), 4, "physical", accuracy_modifier=5),
            atk("狙击弓", "远程", ("DEX", "INS"), 8, "physical", accuracy_modifier=4, effect="对目标施加眩晕或迟缓，由狙击手选择"),
        ),
    },
    "战斗法师": {
        "affinities": aff(dark="抗", earth="弱", fire="抗", ice="抗", light="抗", poison="弱"),
        "attacks": (atk("雕纹法杖", "近战", ("WLP", "WLP"), 11, "physical", accuracy_modifier=2),),
        "spells": (
            spell("闪电击", attributes=("INS", "WLP"), mp_cost=10, target="至多三个生物", effect="对每个目标造成【高值+20】雷系伤害；机会：对每个目标施加眩晕。"),
            spell("治愈术", mp_cost=10, target="至多三个生物", effect="每个目标恢复 40 HP；40级起 50，60级起 60。"),
        ),
    },
    "猫妖": {
        "affinities": aff(lightning="弱", fire="抗", ice="抗", poison="弱"),
        "attacks": (
            atk("抓挠", "近战", ("DEX", "MIG"), 5, "physical"),
            atk("鬼火", "远程", ("DEX", "WLP"), 5, "fire_or_ice", effect="造成火系或冰系伤害，攻击目标魔防"),
        ),
        "spells": (spell("热量控制", attributes=("INS", "WLP"), mp_cost=15, target="一个生物", effect="选择火系或冰系；场景内所选伤害来源对目标额外造成 5 点伤害。"),),
    },
    "恐怖飞蛾": {
        "affinities": aff(fire="弱", ice="抗", poison="抗"),
        "attacks": (atk("飞蛾啃咬", "近战", ("DEX", "MIG"), 10, "physical"),),
        "spells": (spell("毒雾", attributes=("MIG", "WLP"), mp_cost=10, target="至多三个生物", effect="对每个目标施加中毒。"),),
    },
    "圆润软泥怪": {
        "affinities": aff(physical="抗", lightning="抗", fire="抗", ice="抗", poison="弱"),
        "attacks": (
            atk("软萌舔舔", "近战", ("DEX", "MIG"), 10, "physical"),
            atk("圆润吹息", "远程", ("DEX", "INS"), 5, "wind"),
        ),
        "spells": (spell("弹弹舞", mp_cost=10, target="一个生物", effect="目标恢复 30 HP；高等级恢复量提升；并解除一项异常状态。"),),
    },
    "龙兽": {
        "affinities": aff(fire="免", ice="弱", poison="弱"),
        "attacks": (
            atk("撕咬", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=4),
            atk("扫尾", "近战", ("DEX", "MIG"), 5, "physical", accuracy_modifier=4, effect="多重攻击(2)"),
        ),
        "spells": (spell("龙息", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】火系伤害，并施加动摇。"),),
    },
    "魔眼": {
        "affinities": aff(physical="抗", lightning="弱", earth="抗", light="弱"),
        "attacks": (
            atk("利爪", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1),
            atk("混乱凝视", "远程", ("DEX", "WLP"), 5, "dark", accuracy_modifier=1, effect="若目标眩晕，下回合少执行一次行动"),
        ),
        "spells": (spell("毁灭凝视", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="对目标施加眩晕和虚弱。"),),
    },
    "浮空水母": {
        "affinities": aff(lightning="弱", ice="抗", poison="抗"),
        "attacks": (
            atk("酸蚀之触", "近战", ("MIG", "MIG"), 10, "poison", accuracy_modifier=1, effect="所有命中目标失去 1 IP"),
            atk("蛰刺触手", "近战", ("DEX", "MIG"), 5, "lightning", accuracy_modifier=1, effect="对目标施加激怒"),
        ),
    },
    "鸡蛇怪": {
        "affinities": aff(lightning="抗", earth="抗", ice="弱"),
        "attacks": (
            atk("石化啄击", "近战", ("DEX", "INS"), 0, "none", accuracy_modifier=4, effect="攻击目标魔防；命中施加迟缓；若目标已迟缓，需通过难度等级10【力量+意志】检定，否则石化"),
            atk("毒性啄击", "近战", ("DEX", "MIG"), 10, "poison", accuracy_modifier=4),
        ),
    },
    "拟形怪": {
        "affinities": aff(physical="抗", wind="抗", dark="抗", earth="弱", light="抗", poison="弱"),
        "attacks": (
            atk("拟形怪之爪", "近战", ("DEX", "MIG"), 5, "physical", accuracy_modifier=4, effect="偷袭毫无防备目标时伤害翻倍"),
            atk("偷取物品", "远程", ("DEX", "INS"), 5, "physical", accuracy_modifier=4, effect="所有命中目标失去 2 IP"),
        ),
    },
    "曼德拉草": {
        "affinities": aff(physical="抗", earth="抗", ice="弱", poison="弱"),
        "attacks": (
            atk("藤蔓拍击", "近战", ("DEX", "MIG"), 5, "physical", effect="对动摇目标造成 5 点额外伤害"),
            atk("曼德拉尖啸", "远程", ("DEX", "WLP"), 0, "none", effect="攻击目标魔防；对目标施加动摇；对听不见声音的目标无效"),
        ),
    },
    "诅咒南瓜": {
        "affinities": aff(physical="弱", dark="抗", earth="弱", fire="抗"),
        "attacks": (atk("腐烂撕咬", "近战", ("DEX", "MIG"), 5, "poison", accuracy_modifier=3),),
        "spells": (spell("呕吐南瓜", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="场景内毒系伤害来源对目标额外造成 5 点伤害。"),),
    },
    "缠根藤": {
        "affinities": aff(physical="弱", dark="免", earth="抗", fire="弱", poison="抗"),
        "attacks": (
            atk("腐化藤蔓", "近战", ("DEX", "DEX"), 5, "physical", accuracy_modifier=1, effect="对目标施加虚弱；目标虚弱期间无法防御"),
            atk("黑暗撕咬", "近战", ("DEX", "MIG"), 10, "dark", accuracy_modifier=1),
        ),
    },
    "幻菇人": {
        "affinities": aff(dark="抗", earth="抗", ice="弱"),
        "attacks": (atk("幻菇拍击", "近战", ("DEX", "MIG"), 5, "physical", accuracy_modifier=1),),
        "spells": (spell("孢子喷吐", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】毒系伤害，并施加眩晕。"),),
        "traits_rules": ("宽帽子：防御时可选择另一个生物，该生物对所有伤害获得抵抗，直到幻菇人下回合开始。",),
    },
    "仙人掌巨魔": {
        "affinities": aff(physical="抗", earth="抗", fire="抗", ice="弱", light="抗"),
        "attacks": (
            atk("穿刺拥抱", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=1),
            atk("棘刺弹幕", "远程", ("DEX", "MIG"), 5, "physical", accuracy_modifier=1),
        ),
        "spells": (spell("水分榨取", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】毒系伤害，并恢复等同于目标损失 HP 一半的 HP。"),),
        "other_actions": ("仙人掌汁液：解除自身迟缓和虚弱，随后以顺势攻击发动“棘刺弹幕”。",),
    },
    "陷龙花": {
        "affinities": aff(physical="抗", lightning="弱", earth="弱", fire="抗", light="抗", poison="弱"),
        "attacks": (
            atk("吞龙巨口", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=5, effect="若命中虚弱目标则吞噬；被吞噬者每回合开始受 20 物理伤害，只能推进脱困命刻"),
            atk("藤蔓挥击", "远程", ("DEX", "MIG"), 15, "wind", accuracy_modifier=5, effect="对目标施加虚弱"),
        ),
        "spells": (
            spell("麻痹气体", attributes=("MIG", "WLP"), mp_cost=10, target="至多三个生物", effect="所有目标失去当前 MP 的一半。"),
            spell("预消化", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="场景内物理伤害来源对目标额外造成 5 点伤害。"),
        ),
        "traits_rules": ("吞噬脱困：解救被吞噬目标是 4 格命刻。", "松弛之攫：每当陷龙花失去 HP，每个被吞噬生物的脱困命刻填充 1 格。"),
    },
    "惧灵瓮": {
        "affinities": aff(physical="弱", dark="免", earth="弱", light="弱", poison="免"),
        "attacks": (
            atk("瓮身冲撞", "近战", ("DEX", "MIG"), 5, "physical"),
            atk("混乱尖笑", "远程", ("DEX", "MIG"), 5, "dark", effect="对目标施加动摇；攻击目标魔防"),
        ),
    },
    "丧尸": {
        "affinities": aff(dark="免", earth="弱", fire="弱", light="弱", poison="免"),
        "attacks": (
            atk("饥渴撕咬", "近战", ("DEX", "MIG"), 5, "dark", effect="对目标施加虚弱"),
            atk("丧尸猛击", "近战", ("DEX", "MIG"), 5, "physical"),
        ),
    },
    "骷髅法师": {
        "affinities": aff(dark="免", earth="弱", fire="抗", ice="抗", light="弱", poison="免"),
        "attacks": (atk("法杖", "近战", ("WLP", "WLP"), 6, "physical", accuracy_modifier=1, effect="骷髅法师恢复 5 MP"),),
        "spells": (spell("影袭", attributes=("INS", "WLP"), mp_cost=10, target="至多三个生物", effect="对每个目标造成【高值+15】暗系伤害；机会：对每个目标施加动摇。"),),
    },
    "骷髅士兵": {
        "affinities": aff(physical="弱", dark="免", earth="弱", light="弱", poison="免"),
        "attacks": (atk("青铜剑", "近战", ("DEX", "MIG"), 11, "physical", accuracy_modifier=5),),
    },
    "骨嚎怪": {
        "affinities": aff(physical="抗", wind="弱", dark="免", ice="抗", light="弱", poison="免"),
        "attacks": (atk("嶙峋利齿", "近战", ("DEX", "MIG"), 10, "physical", accuracy_modifier=1, effect="对目标施加迟缓"),),
        "spells": (spell("腐臭之息", attributes=("MIG", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+15】暗系伤害，并施加虚弱。"),),
    },
    "食尸鬼": {
        "affinities": aff(dark="免", light="弱", poison="免"),
        "attacks": (
            atk("凶残利爪", "近战", ("MIG", "MIG"), 10, "physical", accuracy_modifier=1, effect="多重攻击(2)"),
            atk("剧毒之息", "近战", ("DEX", "MIG"), 5, "poison", accuracy_modifier=1, effect="对目标施加中毒"),
        ),
    },
    "木乃伊": {
        "affinities": aff(physical="弱", dark="免", fire="弱", light="弱", poison="免"),
        "attacks": (atk("古墓利爪", "近战", ("MIG", "WLP"), 10, "earth", accuracy_modifier=5, effect="所有命中目标施加迟缓；迟缓解除前对所有伤害类型处于弱点"),),
    },
    "禁锢之魂": {
        "affinities": aff(physical="免", wind="弱", dark="免", earth="抗", fire="弱", ice="抗", light="弱", poison="免"),
        "attacks": (atk("狂怒利爪", "近战", ("DEX", "WLP"), 10, "dark", accuracy_modifier=5, effect="对目标施加激怒"),),
        "spells": (spell("凄厉亡嚎", attributes=("INS", "WLP"), mp_cost=10, target="一个生物", effect="造成【高值+20】冰系伤害，并施加动摇。"),),
    },
}


def _overlay_bestiary_entry(entry_: BestiaryEntry) -> BestiaryEntry:
    data = _BESTIARY_DETAIL_OVERLAY.get(entry_.name)
    if not data:
        return entry_
    merged = dict(data)
    if "traits_rules" in merged:
        merged["traits_rules"] = tuple(dict.fromkeys((*entry_.traits_rules, *tuple(merged["traits_rules"]))))
    return replace(entry_, **merged)


CORE_BESTIARY_ENTRIES: tuple[BestiaryEntry, ...] = tuple(
    _overlay_bestiary_entry(entry_) for entry_ in _CORE_BESTIARY_BASE_ENTRIES
)


CORE_BESTIARY_BY_NAME: dict[str, BestiaryEntry] = {entry.name: entry for entry in CORE_BESTIARY_ENTRIES}


def bestiary_entry_by_name(name: str) -> BestiaryEntry | None:
    return CORE_BESTIARY_BY_NAME.get(str(name or "").strip())


def search_bestiary_entries(*, text: str = "", species: str = "", max_level: int | None = None) -> list[BestiaryEntry]:
    text = str(text or "").strip()
    species = str(species or "").strip()
    results = []
    for entry in CORE_BESTIARY_ENTRIES:
        if text and text not in entry.name and text not in entry.description and all(text not in trait for trait in entry.typical_traits):
            continue
        if species and species != entry.species:
            continue
        if max_level is not None and entry.level > max_level:
            continue
        results.append(entry)
    return results


__all__ = [
    "BestiaryAttack",
    "BestiaryEntry",
    "BestiarySpell",
    "CORE_BESTIARY_BY_NAME",
    "CORE_BESTIARY_ENTRIES",
    "bestiary_entry_by_name",
    "search_bestiary_entries",
]
