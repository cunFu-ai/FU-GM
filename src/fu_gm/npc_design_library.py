from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.models import Affinity, EnemyRank, StatusEffect


DAMAGE_TYPES: tuple[str, ...] = (
    "physical",
    "wind",
    "lightning",
    "dark",
    "earth",
    "fire",
    "ice",
    "light",
    "poison",
)


DAMAGE_TYPE_ALIASES: dict[str, str] = {
    "physical": "physical",
    "物理": "physical",
    "wind": "wind",
    "风": "wind",
    "风系": "wind",
    "lightning": "lightning",
    "雷": "lightning",
    "雷系": "lightning",
    "电": "lightning",
    "dark": "dark",
    "暗": "dark",
    "暗系": "dark",
    "earth": "earth",
    "土": "earth",
    "土系": "earth",
    "fire": "fire",
    "火": "fire",
    "火系": "fire",
    "ice": "ice",
    "冰": "ice",
    "冰系": "ice",
    "light": "light",
    "光": "light",
    "光系": "light",
    "poison": "poison",
    "毒": "poison",
    "毒系": "poison",
}


ATTRIBUTE_SPREADS: dict[str, tuple[int, int, int, int]] = {
    "versatile": (8, 8, 8, 8),
    "多面手": (8, 8, 8, 8),
    "standard": (10, 8, 8, 6),
    "标准": (10, 8, 8, 6),
    "specialized": (10, 10, 6, 6),
    "专精": (10, 10, 6, 6),
    "extreme": (12, 8, 6, 6),
    "超级专精": (12, 8, 6, 6),
}


SPECIES_ALIASES: dict[str, str] = {
    "beast": "beast",
    "野兽": "beast",
    "construct": "construct",
    "构装体": "construct",
    "constructs": "construct",
    "demon": "demon",
    "恶魔": "demon",
    "elemental": "elemental",
    "元素": "elemental",
    "humanoid": "humanoid",
    "人型": "humanoid",
    "human": "humanoid",
    "monster": "monster",
    "怪物": "monster",
    "plant": "plant",
    "植物": "plant",
    "undead": "undead",
    "不死族": "undead",
    "不死": "undead",
}


STATUS_NAME_MAP: dict[str, StatusEffect] = {
    "slow": StatusEffect.SLOW,
    "迟缓": StatusEffect.SLOW,
    "dazed": StatusEffect.DAZED,
    "眩晕": StatusEffect.DAZED,
    "weak": StatusEffect.WEAKENED,
    "weakened": StatusEffect.WEAKENED,
    "虚弱": StatusEffect.WEAKENED,
    "shaken": StatusEffect.SHAKEN,
    "动摇": StatusEffect.SHAKEN,
    "enraged": StatusEffect.ENRAGED,
    "激怒": StatusEffect.ENRAGED,
    "poisoned": StatusEffect.POISONED,
    "中毒": StatusEffect.POISONED,
}


@dataclass(frozen=True)
class NPCSpeciesRule:
    slug: str
    name: str
    base_skill_count: int
    rules: tuple[str, ...] = ()
    default_affinities: dict[str, Affinity] = field(default_factory=dict)
    status_immunities: tuple[StatusEffect, ...] = ()
    weakness_options: tuple[str, ...] = ()
    can_use_equipment: bool = False
    cannot_use_equipment: bool = False


@dataclass(frozen=True)
class NPCSkillRule:
    name: str
    summary: str
    examples: tuple[str, ...] = ()
    repeatable: bool = True
    restricted: bool = False


@dataclass(frozen=True)
class NPCSpellRule:
    name: str
    mp_cost: str
    target: str
    duration: str
    effect: str
    requirements: str = ""


@dataclass(frozen=True)
class BattleMechanicRule:
    name: str
    category: str
    summary: str
    gm_guidance: str


@dataclass
class NPCDesignDraft:
    name: str
    level: int
    species: NPCSpeciesRule
    rank: EnemyRank
    traits: list[str]
    attributes: dict[str, int]
    max_hp: int
    crisis_threshold: int
    max_mp: int
    initiative: int
    defenses: dict[str, int]
    affinities: dict[str, Affinity]
    status_immunities: list[StatusEffect]
    check_bonus: int
    extra_damage: int
    skill_budget: int
    selected_skills: list[NPCSkillRule] = field(default_factory=list)
    action_count: int = 1
    soldier_equivalent: int = 1
    base_attack_template: str = "招式名·【属性+属性】·【高值+5】伤害类型"
    rank_notes: list[str] = field(default_factory=list)
    transparency_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def bestiary_header(self) -> str:
        rank_names = {
            EnemyRank.SOLDIER: "小兵",
            EnemyRank.ELITE: "精英",
            EnemyRank.CHAMPION: "悍将",
            EnemyRank.VILLAIN: "反派",
        }
        rank_text = "" if self.rank == EnemyRank.SOLDIER else f"（{rank_names.get(self.rank, self.rank.value)}，等效 {self.soldier_equivalent} 名小兵）"
        return f"{self.name}\n{self.level}级·{self.species.name}{rank_text}"


SPECIES_RULES: dict[str, NPCSpeciesRule] = {
    "beast": NPCSpeciesRule(
        slug="beast",
        name="野兽",
        base_skill_count=4,
        rules=("无法习得使用装备技能。", "适合动物般本能、领地意识、变异野兽或魔法兽。"),
        cannot_use_equipment=True,
    ),
    "construct": NPCSpeciesRule(
        slug="construct",
        name="构装体",
        base_skill_count=2,
        rules=("由灵魂能量驱动的人造生命。", "免疫毒系伤害，对土系伤害抵抗，免疫中毒。"),
        default_affinities={"poison": Affinity.IMMUNE, "earth": Affinity.RESIST},
        status_immunities=(StatusEffect.POISONED,),
    ),
    "demon": NPCSpeciesRule(
        slug="demon",
        name="恶魔",
        base_skill_count=3,
        rules=("传说和信仰中邪恶力量的化身。", "应选择两种伤害类型获得抵抗相性。"),
    ),
    "elemental": NPCSpeciesRule(
        slug="elemental",
        name="元素",
        base_skill_count=2,
        rules=("自然之力的实体显现。", "免疫毒系伤害和另一种伤害类型，免疫中毒。"),
        default_affinities={"poison": Affinity.IMMUNE},
        status_immunities=(StatusEffect.POISONED,),
    ),
    "humanoid": NPCSpeciesRule(
        slug="humanoid",
        name="人型",
        base_skill_count=3,
        rules=("群居、会使用工具和装备的智慧生物。", "自带使用装备能力。"),
        can_use_equipment=True,
    ),
    "monster": NPCSpeciesRule(
        slug="monster",
        name="怪物",
        base_skill_count=4,
        rules=("魔法怪兽或怪异智慧生物。", "没有固定物种规则，适合承载独特机制。"),
    ),
    "plant": NPCSpeciesRule(
        slug="plant",
        name="植物",
        base_skill_count=3,
        rules=("变异或魔法进化出的危险掠食植物。", "免疫眩晕、动摇和激怒；应从风、雷、火、冰中选择一种弱点。"),
        status_immunities=(StatusEffect.DAZED, StatusEffect.SHAKEN, StatusEffect.ENRAGED),
        weakness_options=("wind", "lightning", "fire", "ice"),
    ),
    "undead": NPCSpeciesRule(
        slug="undead",
        name="不死族",
        base_skill_count=2,
        rules=(
            "无法正常回归灵魂之河的死尸或鬼魂。",
            "免疫暗系和毒系伤害，免疫中毒，对光系伤害弱点。",
            "恢复生命值的效果可由控制者改为令其损失一半数值的生命值。",
        ),
        default_affinities={"dark": Affinity.IMMUNE, "poison": Affinity.IMMUNE, "light": Affinity.WEAK},
        status_immunities=(StatusEffect.POISONED,),
    ),
}


NPC_SKILL_RULES: tuple[NPCSkillRule, ...] = (
    NPCSkillRule("危机效果", "危机状态下获得特殊修正值或能力。", ("改变相性。", "造成伤害无视抵抗甚至免疫。", "一种攻击获得多重攻击(2)。")),
    NPCSkillRule("伤害吸收", "把一种已抵抗或免疫的伤害类型升级为吸收；通常应额外给出一到两个弱点供玩家利用。"),
    NPCSkillRule("伤害免疫", "对一种非弱点伤害类型免疫；谨慎赋予物理免疫。"),
    NPCSkillRule("伤害抵抗", "对两种伤害类型获得抵抗；也可抵消来自物种的弱点。"),
    NPCSkillRule("最后一搏", "HP 归零时触发特殊行动或攻击；若造成伤害，通常按少量即兴伤害处理。"),
    NPCSkillRule("飞行", "飞行/浮空；无飞行者通常不能近战命中，受到弱点伤害或机会效果可暂时落地。"),
    NPCSkillRule("强化伤害", "选择一种攻击或法术，造成 5 点额外伤害。", restricted=True),
    NPCSkillRule("强化防御", "获得 +2 物防/+1 魔防或 +1 物防/+2 魔防；最多选择两次。", restricted=True),
    NPCSkillRule("强化生命", "最大 HP 增加 10 点。"),
    NPCSkillRule("强化先攻", "先攻获得 +4 修正值。", restricted=True),
    NPCSkillRule("反应", "对特定触发条件作出反应。", ("被近战攻击但未命中时反击。", "被攻击法术命中时反伤。", "受到弱点伤害后改变相性。")),
    NPCSkillRule("特殊攻击", "为一种攻击添加特殊效果。", ("多重攻击(2)。", "攻击魔防。", "施加异常状态。", "困住/吞食目标，需要 4-6 格命刻挣脱。", "对异常目标追加效果。")),
    NPCSkillRule("专精", "命中、施法或特定对抗检定获得 +3 修正；最多三次且不可重复同一种检定。"),
    NPCSkillRule("施法者", "学习一个法术并增加 10 MP，或学习两个法术。"),
    NPCSkillRule("异常状态免疫", "免疫任意两种异常状态。"),
    NPCSkillRule("特殊行动", "执行技能行动产生独特效果。", ("下次攻击/法术 +10 伤害。", "改变姿态或相性。", "召唤弱小增援。")),
    NPCSkillRule("使用装备", "获得饰品、防具、主手和副手栏位；小兵通常只用基础物品，精英/悍将更适合稀有物品。", repeatable=False, restricted=True),
)


NPC_SKILL_INDEX: dict[str, NPCSkillRule] = {rule.name: rule for rule in NPC_SKILL_RULES}


NPC_SPELL_RULES: tuple[NPCSpellRule, ...] = (
    NPCSpellRule("吐息", "20", "特殊", "瞬发", "选择任意数量可见生物，对其施加迟缓、动摇、虚弱或眩晕之一。"),
    NPCSpellRule("诅咒", "5", "一个生物", "瞬发", "对目标造成【高值+10】的自选伤害类型。"),
    NPCSpellRule("恶毒诅咒", "5", "一个生物", "瞬发", "对目标施加迟缓、动摇、虚弱或眩晕之一。"),
    NPCSpellRule("诅咒吐息", "10", "一个生物", "瞬发", "对目标施加迟缓、动摇、虚弱或眩晕中的两项。"),
    NPCSpellRule("毁灭", "10", "一个生物", "瞬发", "对目标造成【高值+15】的自选伤害类型，并施加迟缓、动摇、虚弱或眩晕之一。"),
    NPCSpellRule("终极毁灭", "30", "特殊", "瞬发", "对所有可见敌人造成 30 点自选伤害类型。", "仅建议 30 级以上精英或悍将学习，且只在每轮最后一个回合施放。"),
    NPCSpellRule("舔舐伤口", "5", "自身", "瞬发", "恢复 20 HP；20/40/60 级时改为 30/40/50 HP。"),
    NPCSpellRule("偷取生命", "10", "一个生物", "瞬发", "造成【高值+15】自选伤害类型，并恢复目标此次损失 HP 的一半。"),
    NPCSpellRule("偷取精神", "10", "一个生物", "瞬发", "造成【高值+15】自选伤害类型，并恢复目标此次损失 MP 的一半。"),
    NPCSpellRule("侵染", "10×目标数", "至多三个生物", "瞬发", "对每个目标施加中毒。"),
    NPCSpellRule("抢攻", "20", "一个生物", "瞬发", "目标立刻用装备武器进行一次顺势攻击；NPC 使用常规攻击。"),
    NPCSpellRule("暴怒", "10×目标数", "至多三个生物", "瞬发", "对每个目标施加激怒。"),
    NPCSpellRule("硬壳", "10", "自身", "场景", "对物理伤害获得抵抗，持续至法术结束。"),
    NPCSpellRule("战吼", "10×目标数", "至多三个生物", "场景", "每个目标命中检定获得 +1 修正，持续至法术结束。"),
    NPCSpellRule("削弱", "10", "一个生物", "场景", "选择一种伤害类型；该类型来源对目标造成 5 点额外伤害，持续至法术结束。"),
)


NPC_SPELL_INDEX: dict[str, NPCSpellRule] = {rule.name: rule for rule in NPC_SPELL_RULES}


BATTLE_DESIGN_PRINCIPLES: tuple[str, ...] = (
    "有意义的战斗：只有当双方目标对立且暴力会推动故事时，才启动战斗。",
    "以人为本：敌人也有感情、个性、目标和理由，不应只是数值包。",
    "接受变数：玩家创意、环境、骰运、仪式和命刻都可能改变战局，GM 不需要掌控一切。",
    "平衡但不机械：偶尔简单战斗或艰难 Boss 都可以，但不要让过易或过难成为常态。",
    "从容处理：若没有数据，GM 可以暂停几分钟设计或改皮继承现有生物。",
)


RESOURCE_PRESSURE_NOTES: tuple[str, ...] = (
    "平均而言，队伍在休息/补充前可承受三场简单战斗。",
    "或两场普通战斗，或一场普通战斗加一场简单战斗。",
    "或一场困难战斗；困难战斗后通常应给休整、补给或剧情缓冲。",
)


LEVEL_RELATIONSHIP_NOTES: tuple[str, ...] = (
    "敌人等级低于队伍等级：可能会太弱。",
    "敌人等级在队伍等级 +5 以内：较为合适。",
    "敌人等级在队伍等级 +10 以内：算大威胁。",
    "敌人等级高出队伍等级 11 级以上：通常可能过强，除非有明确逃跑、谈判或机制解法。",
)


BATTLE_MECHANIC_RULES: tuple[BattleMechanicRule, ...] = (
    BattleMechanicRule("守卫", "obstacle", "某些生物可替盟友完全抵挡攻击。", "玩家必须先击败守卫，或用 6-8 格命刻绕过防御。"),
    BattleMechanicRule("限制条件", "obstacle", "敌人只受特定行动影响，直到玩家改变战局。", "把限制做成可发现、可解除的谜题，不要单纯否定玩家技能。"),
    BattleMechanicRule("特殊机制", "objective", "需要按顺序行动或用特定伤害类型打断强攻。", "例如雷系伤害打断元素线圈充能。"),
    BattleMechanicRule("相性变化", "affinity", "敌人相性按回合、危机或事件改变。", "规律应能被玩家记住和利用；识破后不要强行否定。"),
    BattleMechanicRule("波次", "pacing", "击败一波后下一波抵达。", "每波约 3-5 名敌人；需要缓冲时可在波次间给一轮休整。"),
    BattleMechanicRule("增援", "pacing", "每轮结束或特定条件下加入弱小敌人。", "增援应简单易处理，重点是制造压力而非拖慢战斗。"),
    BattleMechanicRule("元素光环", "environment", "全场对特定伤害类型获得抵抗或弱点。", "允许玩家用目标命刻解除或反向利用。"),
    BattleMechanicRule("危险升级", "environment", "每轮结束施加更强危险，如扣 MP、异常或伤害。", "作为倒计时，迫使英雄冒险加速。"),
    BattleMechanicRule("陷阱和灾害", "environment", "雷暴、毒雾、魔法异象等影响特定目标或行动。", "伤害参考即兴伤害表，且应能被规避或利用。"),
    BattleMechanicRule("不稳定区域", "environment", "战场对特定行动反应，如火焰引爆油桶。", "公开预兆，让玩家能主动触发或避免。"),
    BattleMechanicRule("蓄力攻击", "telegraph", "敌人预示强力攻击。", "必须在轮开始时说明何时发动，给防御、打断或推进目标窗口。"),
    BattleMechanicRule("固定模式", "boss", "Boss 每轮按清晰模式行动。", "可预测性会提高参与感，让玩家能制定策略。"),
    BattleMechanicRule("多阶段", "boss", "Boss HP 到 0 后变成新形态并恢复。", "第一阶段宜热身；阶段间建议给英雄额外一轮准备。"),
    BattleMechanicRule("多部件", "boss", "巨型 Boss 的躯干和肢体分别处理。", "仅在概念适合巨大生物、载具、构装体或群体意识时使用，不要默认套用。"),
)


def normalize_species(species: str) -> NPCSpeciesRule:
    key = SPECIES_ALIASES.get(species.strip().lower(), SPECIES_ALIASES.get(species.strip(), species.strip().lower()))
    if key not in SPECIES_RULES:
        raise ValueError(f"未知 NPC 物种：{species}")
    return SPECIES_RULES[key]


def normalize_status(status: str | StatusEffect) -> StatusEffect:
    if isinstance(status, StatusEffect):
        return status
    key = status.strip().lower()
    if key not in STATUS_NAME_MAP:
        raise ValueError(f"未知异常状态：{status}")
    return STATUS_NAME_MAP[key]


def normalize_affinity(value: str | Affinity) -> Affinity:
    if isinstance(value, Affinity):
        return value
    text = value.strip().lower()
    aliases = {
        "normal": Affinity.NORMAL,
        "-": Affinity.NORMAL,
        "弱": Affinity.WEAK,
        "weak": Affinity.WEAK,
        "vulnerable": Affinity.WEAK,
        "抗": Affinity.RESIST,
        "resist": Affinity.RESIST,
        "resistant": Affinity.RESIST,
        "免": Affinity.IMMUNE,
        "immune": Affinity.IMMUNE,
        "吸": Affinity.ABSORB,
        "absorb": Affinity.ABSORB,
    }
    if text not in aliases:
        raise ValueError(f"未知伤害相性：{value}")
    return aliases[text]


def normalize_damage_type(damage_type: str) -> str:
    key = damage_type.strip().lower()
    if key not in DAMAGE_TYPE_ALIASES:
        raise ValueError(f"未知伤害类型：{damage_type}")
    return DAMAGE_TYPE_ALIASES[key]


def npc_skill_rule(name: str) -> NPCSkillRule:
    if name not in NPC_SKILL_INDEX:
        raise ValueError(f"未知 NPC 技能：{name}")
    return NPC_SKILL_INDEX[name]


def npc_spell_rule(name: str) -> NPCSpellRule:
    if name not in NPC_SPELL_INDEX:
        raise ValueError(f"未知 NPC 法术：{name}")
    return NPC_SPELL_INDEX[name]
