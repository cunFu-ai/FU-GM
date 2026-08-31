from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SkillChoiceSpec:
    """A choice opened by learning a skill, separate from using that skill.

    ``guidance`` is private semantic guidance for the GM.  It describes what
    must be decided without prescribing a player-facing sentence.
    """

    key: str
    label: str
    storage_field: str
    timing: str = "on_acquire"
    count_mode: str = "once"
    required_for_creation: bool = True
    option_source: str = ""
    options: tuple[str, ...] = ()
    guidance: str = ""


@dataclass(frozen=True)
class ClassReference:
    """Compact, player-facing metadata for one core class.

    Mechanical benefits mirror the authoritative character-creation rules;
    ``summary`` and ``tags`` exist only to help the GM search and explain the
    catalog without loading every skill description.
    """

    name: str
    summary: str
    tags: tuple[str, ...] = ()
    hp_bonus: int = 0
    mp_bonus: int = 0
    ip_bonus: int = 0
    abilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillReference:
    """技能参考条目。

    这里的文本供 AI GM 检索和叙事参考；真正涉及消耗、掷骰、伤害和状态时，
    仍由对应 Python 组件按已实现的硬规则结算。
    """

    name: str
    kind: str
    class_name: str = ""
    max_ranks: int = 1
    summary: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    choice_specs: tuple[SkillChoiceSpec, ...] = ()

    @property
    def display_name(self) -> str:
        rank_text = f"（+{self.max_ranks}）" if self.max_ranks > 1 else ""
        class_text = f"{self.class_name}：" if self.class_name else ""
        return f"{class_text}{self.name}{rank_text}"


@dataclass(frozen=True)
class SkillImplementationCoverage:
    name: str
    kind: str
    class_name: str
    category: str
    summary: str
    implementation_note: str


def class_skill(
    class_name: str,
    name: str,
    max_ranks: int,
    summary: str,
    *aliases: str,
    tags: tuple[str, ...] = (),
    choices: tuple[SkillChoiceSpec, ...] = (),
) -> SkillReference:
    return SkillReference(
        name=name,
        kind="class",
        class_name=class_name,
        max_ranks=max_ranks,
        summary=summary,
        aliases=aliases,
        tags=tags,
        choice_specs=choices,
    )


def hero_skill(
    name: str,
    summary: str,
    *aliases: str,
    class_name: str = "",
    tags: tuple[str, ...] = (),
) -> SkillReference:
    return SkillReference(
        name=name,
        kind="hero",
        class_name=class_name,
        summary=summary,
        aliases=aliases,
        tags=tags,
    )


def npc_skill(name: str, summary: str, *aliases: str, tags: tuple[str, ...] = ()) -> SkillReference:
    return SkillReference(name=name, kind="npc", summary=summary, aliases=aliases, tags=tags)


ARCANUM_CHOICE_NAMES = (
    "熔炉奥灵",
    "寒霜奥灵",
    "门径奥灵",
    "魔典奥灵",
    "橡树奥灵",
    "天空奥灵",
    "剑之奥灵",
    "高塔奥灵",
    "轮之奥灵",
)
CHIMERIST_ATTRIBUTE_CHOICES = ("洞察+意志", "力量+意志")
PORTABLE_DEVICE_CHOICE_NAMES = ("炼金装置", "注魔装置", "魔导装置")


def skill_choice(
    key: str,
    label: str,
    storage_field: str,
    *,
    timing: str = "on_acquire",
    count_mode: str = "once",
    required_for_creation: bool = True,
    option_source: str = "",
    options: tuple[str, ...] = (),
    guidance: str = "",
) -> SkillChoiceSpec:
    return SkillChoiceSpec(
        key=key,
        label=label,
        storage_field=storage_field,
        timing=timing,
        count_mode=count_mode,
        required_for_creation=required_for_creation,
        option_source=option_source,
        options=options,
        guidance=guidance,
    )


CLASS_SKILL_REFERENCES: tuple[SkillReference, ...] = (
    class_skill("奥灵使", "奥灵回响", 4, "冲突中于自己回合主动遣散非本回合召唤的奥灵，且装备魔法类武器时，可在遣散效果后顺势施法；法术总 MP 消耗不得高于 SL×5。", "阿卡纳圆环"),
    class_skill("奥灵使", "奥灵疗愈", 2, "召唤奥灵时，自身恢复 SL×5 HP。", "阿卡纳再生"),
    class_skill(
        "奥灵使",
        "契约与召唤",
        1,
        "与奥灵结契；用一次行动消耗 40 MP 召唤已结契奥灵，获得融合增益；可主动遣散并触发遣散效果。",
        "绑定和召唤",
        "绑定与召唤",
        choices=(
            skill_choice(
                "initial_arcanum",
                "起始绑定奥灵",
                "bound_arcana",
                timing="character_creation_optional",
                required_for_creation=False,
                options=ARCANUM_CHOICE_NAMES,
                guidance=(
                    "创建角色时可以选择一个已经结契的奥灵；玩家暂不选择不会阻止建卡，"
                    "但日后必须先在剧情中完成契约才能召唤。"
                ),
            ),
        ),
    ),
    class_skill("奥灵使", "险境召唤", 6, "危机状态下，召唤奥灵的 MP 消耗减少 SL×5。", "紧急奥术"),
    class_skill("奥灵使", "奥灵系仪式", 1, "可启动隶属于已结契奥灵领域的仪式，使用【意志+意志】施法检定。", "奥术仪式"),
    class_skill("拟兽使", "摄能为食", 5, "施放法术对至少一个生物造成伤害后，若装备魔法类、匕首类或链枷类武器，则恢复 SL×2 MP。", "吞噬"),
    class_skill("拟兽使", "野性之语", 1, "可与野兽、怪物和植物物种交流。", "野性人语"),
    class_skill("拟兽使", "同源之毒", 1, "嵌合法术造成伤害时，对同物种生物施加中毒。", "恶性循环"),
    class_skill(
        "拟兽使",
        "拟兽系仪式",
        1,
        "可启动拟兽学派仪式；习得时选择【洞察+意志】或【力量+意志】作为拟兽仪式检定。",
        "仪式嵌合术",
        choices=(
            skill_choice(
                "casting_attributes",
                "仪式属性组合",
                "skill_options",
                options=CHIMERIST_ATTRIBUTE_CHOICES,
                guidance="习得时固定选择拟兽系仪式以后使用的属性组合。",
            ),
        ),
    ),
    class_skill(
        "拟兽使",
        "形意咒法",
        10,
        "目睹野兽、怪物或植物施法后，可记忆为拟兽使法术；攻击性拟兽法术使用习得时选择的属性组合；记忆上限为 SL+2。",
        "咒语模仿",
        choices=(
            skill_choice(
                "casting_attributes",
                "攻击性法术属性组合",
                "skill_options",
                options=CHIMERIST_ATTRIBUTE_CHOICES,
                guidance="首次习得时固定选择攻击性拟兽使法术使用的属性组合。",
            ),
        ),
    ),
    class_skill("暗刃骑士", "痛楚", 5, "每回合限一次，当你对存在羁绊的生物造成伤害后，恢复 SL×2 HP 和 MP。", "愤怒"),
    class_skill("暗刃骑士", "身负黑血", 1, "危机状态下对暗系和毒系伤害获得抵抗。", "黑暗之血"),
    class_skill("暗刃骑士", "黑暗之心", 1, "进入危机后，可对一个未结羁绊的生物建立憎恨羁绊。"),
    class_skill("暗刃骑士", "苦痛教训", 3, "另一个生物令你失去 HP 后，可立即对该生物执行调查顺势行动，并在检定中获得 +SL。", "痛苦的教训"),
    class_skill("暗刃骑士", "暗影击", 5, "用行动掷当前 MIG 骰并失去等量 HP；若未降至 0 HP，可用装备武器顺势攻击，命中额外造成 SL+该骰值的暗系伤害且类型不能改变。", "暗影突袭"),
    class_skill("元素使", "天灾骤降", 3, "装备魔法类武器施放瞬发法术时，可把法术总 MP 消耗提高至多 SL×10；每提高 10 MP，该法术造成 5 点额外伤害。", "灾难"),
    class_skill(
        "元素使",
        "元素魔法",
        10,
        "每级学习一个元素使法术。",
        choices=(
            skill_choice(
                "granted_spell",
                "元素使法术",
                "spells",
                count_mode="per_rank",
                option_source="元素使法术",
                guidance="每次取得此技能都要选择一个元素使法术。",
            ),
        ),
    ),
    class_skill("元素使", "魔法炮击", 3, "装备魔法类武器施放攻击性法术时，施法检定获得 SL×2 修正。"),
    class_skill("元素使", "元素系仪式", 1, "可执行元素领域仪式，通常使用【洞察+意志】。", "仪式元素术"),
    class_skill("元素使", "以械引咒", 3, "针对单个目标施放攻击性法术时，若法术总 MP 消耗不高于 SL×20，可用当前装备的一件非魔法类武器命中算式作为施法检定；若算式含 DEX，检定额外 +SL。", "咒语之刃"),
    class_skill("熵术士", "灵智回流", 5, "受到伤害后，恢复 SL×2 MP。", "吸收心智"),
    class_skill(
        "熵术士",
        "熵系魔法",
        10,
        "每级学习一个熵术士法术。",
        choices=(
            skill_choice(
                "granted_spell",
                "熵术士法术",
                "spells",
                count_mode="per_rank",
                option_source="熵术士法术",
                guidance="每次取得此技能都要选择一个熵术士法术。",
            ),
        ),
    ),
    class_skill("熵术士", "幸运七", 1, "每场游戏幸运数字重置为 7；每场景限一次，检定后可用幸运数字替换一枚骰子结果，被替换的点数成为新的幸运数字。"),
    class_skill("熵术士", "熵系仪式", 1, "可执行熵系领域仪式，通常使用【洞察+意志】。", "熵系仪式术"),
    class_skill("熵术士", "窃取时间", 4, "冲突中用行动消耗至多 SL×5 MP；每 5 MP 可施加/解除迟缓、让敌人缓慢失去 10+SL×5 HP、让盟友顺势装备，或让本轮未行动盟友在你回合后立即行动；每项单次最多选一次。", "盗取时间"),
    class_skill("怒焰斗士", "肾上腺素", 5, "危机状态下，你通过攻击、法术、奥灵、物品或其他方式造成 SL×2 额外伤害。"),
    class_skill("怒焰斗士", "狂暴", 1, "使用格斗、匕首、链枷或投掷武器时，双骰同点也可能触发大成功。", "疯狂"),
    class_skill("怒焰斗士", "不屈意志", 4, "每当你消耗至少 1 点物语点时，额外恢复 SL×5 HP、恢复 SL×5 MP，或解除自身一种异常状态。", "不屈不挠的精神"),
    class_skill("怒焰斗士", "挑衅", 5, "用行动消耗 5 MP，对可见生物进行【力量+意志】对抗并获得 +SL；成功则施加激怒，并迫使目标尽量把你纳入攻击或攻击性法术目标。", "嘲讽"),
    class_skill("怒焰斗士", "死战不退", 5, "执行防御且不掩护他人时，恢复 SL×最高羁绊强度 HP，并使 MIG 或 WLP 骰级+1，持续到你的下个回合结束。", "忍耐"),
    class_skill("守护者", "保镖", 1, "执行防御并掩护其他生物时，被掩护者对所有伤害类型获得抵抗，持续至你的下个回合开始。"),
    class_skill("守护者", "防御精通", 5, "装备盾牌或职业限定防具时，你受到的所有伤害先于相性减少 SL 点。", "防守掌握"),
    class_skill("守护者", "双盾战士", 1, "可在主手装备盾牌；装备双盾时同时获得两面盾增益，并可视为【力量+力量】【高值+5】物理双手格斗武器，额外造成防御精通 SL 伤害。", "双重盾牌"),
    class_skill("守护者", "铁壁", 5, "你的最大 HP 永久提升 SL×3。", "不动要塞"),
    class_skill("守护者", "挺身守护", 1, "当另一个生物即将遭遇攻击、法术或险情时，可由你代为承受；若你本来也在目标内，则分开承受两次；冲突中使用后直到你的下回合开始前不能再用。", "保护者"),
    class_skill("博学家", "灵光洞见", 3, "调查生物、物品或地点且检定结果 13+ 时，可向 GM 询问至多 SL 个相关问题；同一对象只可触发一次。", "灵光一现"),
    class_skill("博学家", "集中心智", 5, "最大 MP 永久提升 SL×3。", "集中"),
    class_skill("博学家", "知识就是力量", 1, "当你使用【洞察+洞察】进行开放检定时，本次检定获得 +SL；仅对开放检定有效。"),
    class_skill("博学家", "快速评估", 6, "冲突开始时可消耗至多 SL×5 MP；每 5 MP 揭示一个可见生物的特质，或声明一种伤害类型并揭示一个可见生物对此类型的相性。"),
    class_skill("博学家", "记忆训练", 1, "完美回忆一周内去过的地点，可对记忆场景使用灵光洞见。", "训练有素的记忆能力"),
    class_skill("游说家", "谴责", 6, "用行动消耗 5 MP，对能理解你言语的敌人进行【洞察+意志】对抗并获得 +SL；成功则目标失去 SL×10 MP，受到眩晕或动摇，且直到你下回合开始，任何伤害来源对其额外造成 SL 点伤害。"),
    class_skill("游说家", "鼓舞", 6, "用行动消耗 5 MP，选择能听懂你的盟友；其恢复 SL×10 HP，并在你的下回合开始前使一个属性骰提升 1 阶。", "激励"),
    class_skill("游说家", "予以信任", 2, "另一个能听见你的 PC 检定后，你可消耗 1 物语点援用其特质或羁绊帮其重掷或加值；若你对其有羁绊，该角色恢复 SL×10 MP。", "我相信你"),
    class_skill("游说家", "巧舌如簧", 2, "当你以魅力、交涉、欺骗或威胁成功检定并填充/擦除命刻时，可消耗至多 SL×20 MP；每 20 MP 额外填充/擦除 1 格。", "令人信服"),
    class_skill("游说家", "意外盟友", 1, "用行动消耗 1 物语点，选择能听懂你言语的非敌对生物；只要友善、尊重且要求合理，对方便会提供帮助。", "意想不到的盟友"),
    class_skill("浪客", "阴狠手段", 5, "攻击命中唯一目标且该目标受至少一种异常状态影响时，额外造成 SL+该目标异常状态数量的伤害。", "恶意中伤"),
    class_skill("浪客", "闪避", 3, "只要未装备盾牌和职业限定防具，物防提升 SL。", "招架"),
    class_skill("浪客", "疾速身法", 3, "冲突开始时可消耗 10 MP；第一轮开始时顺势攻击，或执行妨碍/推进目标顺势行动；该检定获得 +SL。", "高速"),
    class_skill("浪客", "回见了您呐", 1, "消耗物语点从当前场景消失，并在之后合理出现在盟友身边。", "待会再见"),
    class_skill("浪客", "窃取灵魂", 5, "冲突中用行动对可见生物执行【敏捷+意志】检定，难度等级为目标魔防，检定 +SL；成功时小兵让你恢复 SL IP，精英/悍将给予价值上限为等级×30Z（反派×50Z）的灵魂宝藏；每个生物最多被成功窃取一次。", "灵魂窃取"),
    class_skill("神射手", "弹幕射击", 1, "进行远程攻击时可消耗 10 MP，使攻击获得多重(2)，或让已有多重目标数+1，最高多重(3)。", "连续射击"),
    class_skill("神射手", "干涉火力", 1, "装备远程武器时，在可见生物进行非大成功远程攻击后，消耗 5+该命中检定结算值 MP，令此次命中检定自动失败。", "交叉火力"),
    class_skill("神射手", "鹰眼", 5, "防御且不掩护时，选择本场景下一次远程攻击额外造成 SL×3 伤害，或用当前弓类/枪械类武器进行一次高值视为 0 的顺势攻击。"),
    class_skill("神射手", "远程武器精通", 4, "使用远程武器进行的所有命中检定获得 +SL。", "远程武器掌握"),
    class_skill("神射手", "威慑射击", 4, "远程攻击命中并即将造成伤害时，可选择不造成伤害，改为对所有命中目标施加动摇、施加迟缓，或使其失去 SL×10 MP。", "警告射击"),
    class_skill("御魂使", "治愈之力", 2, "对一个或多个盟友施放法术且装备魔法类武器时，每个目标额外恢复 3+(SL×你的羁绊数量) HP；此治疗与法术治疗分开。", "疗愈能力"),
    class_skill("御魂使", "御魂系仪式", 1, "可执行灵魂领域仪式，通常使用【洞察+意志】。", "仪式灵师术", "仪式御魂使术"),
    class_skill(
        "御魂使",
        "灵魂魔法",
        10,
        "每级学习一个御魂使法术。",
        "灵系魔法",
        choices=(
            skill_choice(
                "granted_spell",
                "御魂使法术",
                "spells",
                count_mode="per_rank",
                option_source="御魂使法术",
                guidance="每次取得此技能都要选择一个御魂使法术。",
            ),
        ),
    ),
    class_skill("御魂使", "法术支援", 1, "对一个或多个盟友施放法术且装备魔法类武器时，可选择法术目标中一名你有羁绊的盟友；其本场景下一次检定获得等同于该羁绊强度的修正。", "支援魔法"),
    class_skill("御魂使", "生命秘法", 1, "施法时 MP 不足可改为消耗 10+该法术总 MP 消耗的 HP；不能因此降至 0 HP；若该法术会治疗你自己，则你不恢复 HP。", "活力充沛"),
    class_skill("造物使", "应急用品", 1, "每个冲突场景限一次；若你处于危机状态，可在自己的回合内执行一次额外行动，该行动必须是消耗物资行动。", "紧急道具"),
    class_skill(
        "造物使",
        "便携装置",
        5,
        "解锁炼金装置、注魔装置或魔导装置的基础/进阶/顶级能力；再次习得时可解锁新类型或提升已有类型等级。",
        "小工具",
        choices=(
            skill_choice(
                "portable_device",
                "装置类型或升级",
                "skill_options",
                count_mode="per_rank",
                options=PORTABLE_DEVICE_CHOICE_NAMES,
                guidance=(
                    "每次取得此技能都选择一种装置；首次选择解锁基础增益，"
                    "再次选择已解锁类型则提升一级。"
                ),
            ),
        ),
    ),
    class_skill("造物使", "药剂雨", 2, "制造能为单个生物恢复 HP/MP 的药剂时，可让它额外影响至多 SL 个生物；若如此，每个目标的恢复量减半。", "药水雨"),
    class_skill("造物使", "秘密配方", 5, "制造的药剂或法球若能恢复 HP/MP，恢复量 +SL×5；元素裂片、药剂或法球若能造成伤害，伤害 +SL。"),
    class_skill("造物使", "先见之明", 5, "启动工程时自动支付至多 SL×100Z 材料费；此外每天额外产生 SL 进度；多名角色可叠加。", "高瞻远瞩"),
    class_skill(
        "旅人",
        "忠诚伙伴",
        5,
        "协作创建一名 5 级野兽、构装体、元素或植物伙伴；伙伴无独立回合，你可用自己的行动指挥其行动；其命中/施法 +SL，最大 HP 为 SL×伙伴基础 MIG 骰值+你的等级一半。",
        "忠实的伙伴",
        choices=(
            skill_choice(
                "companion_profile",
                "伙伴资料",
                "companion_profile",
                timing="before_first_use",
                required_for_creation=False,
                option_source="忠诚伙伴创建规则",
                guidance=(
                    "与玩家协作创建伙伴的物种、属性和至多两种基础攻击；"
                    "这属于独立伙伴资料，不要塞进技能附带选择字符串。"
                ),
            ),
        ),
    ),
    class_skill("旅人", "充足补给", 4, "每次旅行掷骰后恢复 SL 点物资点。", "足智多谋"),
    class_skill("旅人", "酒馆攀谈", 3, "在旅店或酒馆休息时，可就周边地区和本地居民向 GM 提问至多 SL 个问题。", "酒馆闲聊"),
    class_skill("旅人", "宝物猎人", 2, "队伍在世界地图旅行时，只要旅行掷骰结果不高于 SL+1（而非仅为 1），便触发发现。", "宝藏猎人"),
    class_skill("旅人", "见多识广", 1, "旅行掷骰所用骰子等级降低一级，最低 d6；多人拥有不叠加。", "通晓道路"),
    class_skill("武器大师", "利刃风暴", 1, "进行近战攻击时可消耗 10 MP，使攻击获得多重(2)，或让已有多重目标数+1，最高多重(3)。", "剑刃风暴"),
    class_skill("武器大师", "碎骨", 4, "近战攻击命中并即将造成伤害时，可选择不造成伤害，改为对所有命中目标施加眩晕、施加虚弱，或使其失去 SL×10 MP。", "碎骨击"),
    class_skill("武器大师", "破防打击", 3, "用行动消耗 5 MP，以当前近战武器对单个生物顺势攻击；命中不造成伤害，改为摧毁盾牌、摧毁防具，或让直到你的下回合开始前任何伤害来源对其 +SL×2 伤害。", "破甲击"),
    class_skill("武器大师", "反击", 1, "敌人对你近战攻击后，无论命中与否，若其命中检定结算值为偶数，你可在结算后对该敌人进行一次高值视为 0 的近战顺势攻击。", "招架攻击"),
    class_skill("武器大师", "近战武器精通", 4, "使用近战武器进行的所有命中检定获得 +SL。", "近战武器掌握"),
)


# 核心职业名称来自权威职业技能表，供语义智能体和规则目录共用，
# 避免“旅人”这类同时也是普通名词的职业在不同提示中各自维护。
CORE_CLASS_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        reference.class_name
        for reference in CLASS_SKILL_REFERENCES
        if reference.class_name
    )
)


CLASS_REFERENCES: tuple[ClassReference, ...] = (
    ClassReference(
        "奥灵使",
        "与奥灵缔结契约，通过召唤、融合、遣散效果与奥灵领域仪式改变战局。",
        ("召唤", "奥灵", "仪式", "魔法", "支援"),
        mp_bonus=5,
    ),
    ClassReference(
        "拟兽使",
        "从野兽、怪物与植物身上学习法术，并以交流、毒素和拟兽仪式应对未知生物。",
        ("怪物", "植物", "法术", "仪式", "探索", "毒"),
        mp_bonus=5,
    ),
    ClassReference(
        "暗刃骑士",
        "以生命、痛苦与羁绊换取暗系攻势，越接近危机越能发挥力量。",
        ("近战", "暗系", "危机", "羁绊", "生命消耗", "攻击"),
        hp_bonus=5,
        abilities=("可装备职业近战武器", "可装备职业盔甲"),
    ),
    ClassReference(
        "元素使",
        "操纵风、雷、冰、火与土，兼顾元素伤害、防护、附魔和战场机动。",
        ("元素", "法术", "伤害", "防护", "附魔", "仪式"),
        mp_bonus=5,
    ),
    ClassReference(
        "熵术士",
        "操纵命运、时间与熵，以重掷、迟缓、汲取和时序变化干涉局势。",
        ("命运", "时间", "法术", "控制", "暗系", "仪式"),
        mp_bonus=5,
    ),
    ClassReference(
        "怒焰斗士",
        "在危机中爆发伤害，以挑衅、韧性和正面压迫迫使敌人应战。",
        ("近战", "危机", "挑衅", "攻击", "坚韧", "前排"),
        hp_bonus=5,
        abilities=("可装备职业近战武器", "可装备职业盔甲"),
    ),
    ClassReference(
        "守护者",
        "依靠重甲、盾牌、掩护与挺身守护保护队友并承受伤害。",
        ("防御", "防护", "保护", "盾牌", "重甲", "前排", "掩护"),
        hp_bonus=5,
        abilities=("可装备职业盔甲", "可装备职业盾牌"),
    ),
    ClassReference(
        "博学家",
        "通过调查、知识、记忆和快速评估揭示敌人、地点与谜团的信息。",
        ("知识", "调查", "探索", "情报", "精神值", "支援"),
        mp_bonus=5,
    ),
    ClassReference(
        "游说家",
        "以鼓舞、谴责、交涉和信任影响盟友、敌人与非敌对角色。",
        ("交涉", "支援", "鼓舞", "社交", "治疗", "控制"),
        mp_bonus=5,
    ),
    ClassReference(
        "浪客",
        "擅长闪避、利用异常状态、抢占先机、脱身与窃取宝物。",
        ("敏捷", "闪避", "异常状态", "潜行", "盗取", "探索"),
        ip_bonus=2,
    ),
    ClassReference(
        "神射手",
        "使用远程武器实施精准射击、弹幕、威慑和干涉火力。",
        ("远程", "射击", "控制", "攻击", "枪械", "弓"),
        hp_bonus=5,
        abilities=("可装备职业远程武器", "可装备职业盾牌"),
    ),
    ClassReference(
        "御魂使",
        "运用灵魂魔法治疗、净化、保护和强化伙伴，也能以光系力量攻击。",
        ("治疗", "支援", "防护", "净化", "光系", "法术", "仪式"),
        mp_bonus=5,
    ),
    ClassReference(
        "造物使",
        "利用炼金、注魔和魔导装置制造物品、改造攻击并推进工程。",
        ("装置", "炼金", "制造", "工程", "物资点", "支援"),
        ip_bonus=2,
        abilities=("可发起项目",),
    ),
    ClassReference(
        "旅人",
        "通过伙伴、补给、见闻和寻宝能力支援旅行与世界地图探索。",
        ("旅行", "探索", "伙伴", "补给", "寻宝", "地图"),
        ip_bonus=2,
    ),
    ClassReference(
        "武器大师",
        "精通近战武器，以多重攻击、碎骨、破防和反击控制前线。",
        ("近战", "武器", "反击", "控制", "攻击", "前排"),
        hp_bonus=5,
        abilities=("可装备职业近战武器", "可装备职业盾牌"),
    ),
)

if tuple(reference.name for reference in CLASS_REFERENCES) != CORE_CLASS_NAMES:
    raise RuntimeError("职业目录与职业技能表的标准职业顺序不一致。")

CLASS_REFERENCES_BY_NAME: dict[str, ClassReference] = {
    reference.name: reference for reference in CLASS_REFERENCES
}


HERO_SKILL_REFERENCES: tuple[SkillReference, ...] = (
    hero_skill("灵活双持", "可用不同类型武器进行双武器战斗。", "灵巧双手"),
    hero_skill("额外生命值", "立即提高最大 HP；40 级后提升幅度更高。", "额外HP", "额外 HP"),
    hero_skill("额外精神值", "立即提高最大 MP；40 级后提升幅度更高。", "额外MP", "额外 MP"),
    hero_skill("额外物资点", "最大库存点 +4。", "额外IP", "额外 IP"),
    hero_skill("额外法术", "额外学习两个同列表法术。", "额外咒语"),
    hero_skill("绝处逢生", "暗刃骑士精通；异常状态会反过来强化检定和伤害。", "背水", class_name="暗刃骑士"),
    hero_skill("摧心重击", "暗刃骑士精通；牺牲当前 HP，对有羁绊的唯一目标造成爆发伤害。", "薄情者", class_name="暗刃骑士"),
    hero_skill("奥灵共鸣", "奥灵使精通；奥灵领域适用时，额外推进或清除命刻。", "奥术回响", class_name="奥灵使"),
    hero_skill("奥灵启示", "奥灵使精通；与自创未知奥灵结契，并可强化其遣散效果。", "启示", class_name="奥灵使"),
    hero_skill("拟兽系精通", "拟兽使精通；可从新的生物类型学习法术，并增加记忆槽。", "嵌合术精通", class_name="拟兽使"),
    hero_skill("彗星", "熵术士精通；学习究极熵系法术。", class_name="熵术士"),
    hero_skill("深藏不露", "造物使精通；使用库存点时消耗减少。", "大口袋", class_name="造物使"),
    hero_skill("技术升级", "造物使精通；休息时改造装备品质。", "升级", class_name="造物使"),
    hero_skill("缴械雄辩", "游说家精通；说服动摇或危机中的小兵和平离开冲突。", "卸甲真言", class_name="游说家"),
    hero_skill("复诵", "游说家精通；谴责或鼓舞后可再次使用同技能。", "重唱", class_name="游说家"),
    hero_skill("英勇伙伴", "旅人精通且拥有忠实伙伴；强化伙伴。", "英雄级同伴", class_name="旅人"),
    hero_skill("免于异常", "旅人精通；选择一种异常状态完全免疫。", "状态免疫", class_name="旅人"),
    hero_skill("重燃希望", "御魂使精通；学习让已投降 PC 重燃希望的究极法术。", "希望", class_name="御魂使"),
    hero_skill("数学魔法", "博学家精通；按属性骰尺寸把单体法术扩展为群体法术。", class_name="博学家"),
    hero_skill("不出所料！", "博学家精通；预测敌人行动并提高其行动成本。", "不出所料", "我算到了", class_name="博学家"),
    hero_skill("强力射击", "神射手精通；远程攻击造成额外伤害。", class_name="神射手"),
    hero_skill("完美瞄准", "神射手精通且拥有威慑射击；威慑射击可选择两个效果。", class_name="神射手"),
    hero_skill("洗劫一空", "浪客精通且拥有窃取灵魂；可对多个生物使用窃取灵魂。", "劫掠", class_name="浪客"),
    hero_skill("影逝", "浪客精通；命中后消耗物语点，让目标暂时无法看见你。", "消失", class_name="浪客"),
    hero_skill("灵猴握", "怒焰斗士精通；可单手装备部分双手武器。", "猴式握法", class_name="怒焰斗士"),
    hero_skill("猛力打击", "怒焰斗士或武器大师精通；近战攻击造成额外伤害。", "强力攻击", class_name="怒焰斗士/武器大师"),
    hero_skill("坚强壁垒", "守护者精通；冲突第一轮获得全伤害抵抗和异常抗性。", "堡垒", class_name="守护者"),
    hero_skill("坚不可摧", "守护者精通；每场景首次即将降到 0 HP 时可保留 1 HP。", "不破之人", class_name="守护者"),
    hero_skill("疾风连打", "武器大师精通；多重近战攻击只打单体时造成额外伤害。", "风暴击", class_name="武器大师"),
    hero_skill("火山", "元素使精通；学习究极元素法术。", class_name="元素使"),
    hero_skill("强效法术", "施法职业精通；法术造成额外伤害。", "强力咒语", class_name="拟兽使/元素使/熵术士/御魂使"),
)


NPC_SKILL_REFERENCES: tuple[SkillReference, ...] = (
    npc_skill("危机效果", "危机状态下改变相性、无视抗性、获得多重攻击或启用特殊能力。"),
    npc_skill("伤害吸收", "对特定伤害类型吸收，受到该类型伤害时转为恢复。"),
    npc_skill("伤害免疫", "对特定伤害类型免疫。"),
    npc_skill("伤害抵抗", "对特定伤害类型抵抗。"),
    npc_skill("最后一搏", "HP 归零时触发自爆、遗言、召唤或大招。"),
    npc_skill("飞行", "飞行时通常不能被近战攻击触及，被特定手段命中后可能落地。"),
    npc_skill("强化伤害", "提升攻击、法术或特殊能力造成的伤害。"),
    npc_skill("强化防御", "提升物防、魔防或对特定检定的抵抗。"),
    npc_skill("强化生命", "提高 HP 或危机阈值相关能力。"),
    npc_skill("强化先攻", "提高先攻值。"),
    npc_skill("反应", "受击、闪避或特定行动后触发反击、恢复、位移或状态。"),
    npc_skill("特殊攻击", "攻击附加多重、打魔防、异常状态、吞食命刻等特殊机制。"),
    npc_skill("专精", "特定检定获得 +3 修正。"),
    npc_skill("施法者", "获得 NPC 法术库或特定领域法术。"),
    npc_skill("异常状态免疫", "免疫一种或多种异常状态。"),
    npc_skill("特殊行动", "可改变姿态、召唤小怪、转换相性或推进 Boss 机制。"),
)


SKILL_REFERENCES: tuple[SkillReference, ...] = (
    *CLASS_SKILL_REFERENCES,
    *HERO_SKILL_REFERENCES,
    *NPC_SKILL_REFERENCES,
)


def _build_index(references: Iterable[SkillReference]) -> dict[str, SkillReference]:
    index: dict[str, SkillReference] = {}
    for reference in references:
        index[reference.name] = reference
        for alias in reference.aliases:
            index[alias] = reference
    return index


SKILL_REFERENCES_BY_NAME = _build_index(SKILL_REFERENCES)
SKILL_ALIASES: dict[str, str] = {
    alias: reference.name
    for reference in SKILL_REFERENCES
    for alias in reference.aliases
}


SPELL_GRANTING_SKILLS: dict[str, str] = {
    reference.name: choice.option_source
    for reference in CLASS_SKILL_REFERENCES
    for choice in reference.choice_specs
    if choice.storage_field == "spells" and choice.option_source
}


SKILL_COVERAGE_HARD_RULE = "hard_rule"
SKILL_COVERAGE_PASSIVE_HARD = "passive_hard"
SKILL_COVERAGE_GM_JUDGEMENT = "gm_judgement"
SKILL_COVERAGE_REFERENCE_ONLY = "reference_only"


_HARD_RULE_SKILLS = {
    "契约与召唤",
    "暗影击",
    "挑衅",
    "谴责",
    "鼓舞",
    "窃取时间",
    "窃取灵魂",
    "回见了您呐",
    "碎骨",
    "威慑射击",
    "破防打击",
    "挺身守护",
    "快速评估",
    "意外盟友",
    "弹幕射击",
    "利刃风暴",
    "干涉火力",
    "反击",
    "摧心重击",
    "缴械雄辩",
    "不出所料！",
    "影逝",
    "重燃希望",
    "火山",
    "彗星",
    "天灾骤降",
    "魔法炮击",
    "以械引咒",
    "双盾战士",
    "复诵",
    "完美瞄准",
    "洗劫一空",
    "疾风连打",
    "强化伤害",
    "强化防御",
    "强化生命",
    "强化先攻",
    "专精",
    "忠诚伙伴",
}

_PASSIVE_HARD_SKILLS = {
    "奥灵疗愈",
    "摄能为食",
    "野性之语",
    "形意咒法",
    "记忆训练",
    "酒馆攀谈",
    "险境召唤",
    "奥灵系仪式",
    "拟兽系仪式",
    "元素魔法",
    "元素系仪式",
    "熵系魔法",
    "熵系仪式",
    "御魂系仪式",
    "灵魂魔法",
    "生命秘法",
    "防御精通",
    "铁壁",
    "集中心智",
    "近战武器精通",
    "远程武器精通",
    "先见之明",
    "便携装置",
    "秘密配方",
    "见多识广",
    "肾上腺素",
    "身负黑血",
    "灵智回流",
    "狂暴",
    "闪避",
    "充足补给",
    "宝物猎人",
    "知识就是力量",
    "巧舌如簧",
    "奥灵共鸣",
    "深藏不露",
    "免于异常",
    "强力射击",
    "强效法术",
    "猛力打击",
    "坚不可摧",
    "额外生命值",
    "额外精神值",
    "额外物资点",
    "额外法术",
    "灵活双持",
    "灵猴握",
    "坚强壁垒",
    "绝处逢生",
    "伤害吸收",
    "伤害免疫",
    "伤害抵抗",
    "飞行",
    "异常状态免疫",
}

_GM_JUDGEMENT_SKILLS = {
    "灵光洞见",
    "意外盟友",
    "英勇伙伴",
    "奥灵启示",
    "数学魔法",
    "拟兽系精通",
    "技术升级",
    "奥灵回响",
    "同源之毒",
    "痛楚",
    "黑暗之心",
    "苦痛教训",
    "幸运七",
    "不屈意志",
    "死战不退",
    "保镖",
    "予以信任",
    "阴狠手段",
    "疾速身法",
    "鹰眼",
    "治愈之力",
    "法术支援",
    "应急用品",
    "药剂雨",
    "危机效果",
    "最后一搏",
    "反应",
    "特殊攻击",
    "施法者",
    "特殊行动",
}

_COVERAGE_NOTES = {
    SKILL_COVERAGE_HARD_RULE: "已有 ActionInterceptor 或相关组件执行数值结算。",
    SKILL_COVERAGE_PASSIVE_HARD: "已有角色创建、升级、旅行、仪式、休息、攻击或资源系统读取并结算。",
    SKILL_COVERAGE_GM_JUDGEMENT: "规则效果依赖场景提问、叙事许可或长期创作判断，应由 GM 根据上下文裁定。",
    SKILL_COVERAGE_REFERENCE_ONLY: "技能已收录并可检索；当前不自动改动数值，表达层应清楚提示需要人工/GM 判断。",
}


def normalize_skill_reference_name(raw_name: str) -> str:
    name = raw_name.split("（+")[0].split("(+")[0].strip()
    return SKILL_ALIASES.get(name, name)


def normalize_skill_map(skills: dict[str, int]) -> dict[str, int]:
    """把旧译名/别名统一折叠到当前权威译名。"""

    normalized: dict[str, int] = {}
    for raw_name, rank in skills.items():
        canonical = normalize_skill_reference_name(raw_name)
        normalized[canonical] = normalized.get(canonical, 0) + int(rank)
    return normalized


def skill_rank(skills: dict[str, int], name: str) -> int:
    """读取技能等级，同时兼容旧存档里的旧译名。"""

    canonical = normalize_skill_reference_name(name)
    if canonical in skills:
        return int(skills[canonical])
    for alias, alias_canonical in SKILL_ALIASES.items():
        if alias_canonical == canonical and alias in skills:
            return int(skills[alias])
    return 0


def has_skill_name(skill_names: Iterable[str], name: str) -> bool:
    """判断列表式技能是否包含某技能，同时兼容旧存档里的旧译名。"""

    canonical = normalize_skill_reference_name(name)
    return any(normalize_skill_reference_name(skill_name) == canonical for skill_name in skill_names)


def normalize_skill_name_list(skill_names: Iterable[str]) -> list[str]:
    """把列表式技能统一折叠到当前权威译名，并保持原顺序去重。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in skill_names:
        canonical = normalize_skill_reference_name(raw_name)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def required_spell_slots(skills: dict[str, int]) -> dict[str, int]:
    """返回职业技能带来的法术选择需求。"""

    requirements: dict[str, int] = {}
    for skill_name, label in SPELL_GRANTING_SKILLS.items():
        rank = skill_rank(skills, skill_name)
        if rank > 0:
            requirements[label] = requirements.get(label, 0) + rank
    return requirements


def skill_choice_specs(name: str) -> tuple[SkillChoiceSpec, ...]:
    reference = get_skill_reference(name)
    return reference.choice_specs if reference is not None else ()


def skill_choice_requirements(
    skills: dict[str, int],
    *,
    include_optional: bool = True,
) -> list[dict[str, object]]:
    """Resolve acquisition-time choices opened by the selected skill ranks."""

    rows: list[dict[str, object]] = []
    for reference in CLASS_SKILL_REFERENCES:
        rank = skill_rank(skills or {}, reference.name)
        if rank <= 0:
            continue
        for choice in reference.choice_specs:
            if not include_optional and not choice.required_for_creation:
                continue
            required_count = rank if choice.count_mode == "per_rank" else 1
            rows.append(
                {
                    "skill_name": reference.name,
                    "class_name": reference.class_name,
                    "skill_rank": rank,
                    "choice_key": choice.key,
                    "choice_label": choice.label,
                    "storage_field": choice.storage_field,
                    "timing": choice.timing,
                    "count_mode": choice.count_mode,
                    "required_for_creation": choice.required_for_creation,
                    "required_count": required_count,
                    "option_source": choice.option_source,
                    "allowed_values": list(choice.options),
                    "guidance": choice.guidance,
                }
            )
    return rows


def compact_skill_choice_requirement(
    requirement: dict[str, object],
    *,
    inline_value_limit: int = 5,
) -> dict[str, object]:
    """Create a model-facing choice record without embedding long catalogs.

    The authoritative validator keeps its complete ``allowed_values``.  Only
    spell catalogs are replaced here because they have a dedicated read-only
    lookup tool; short acquisition choices remain inline and immediately
    usable.
    """

    result = dict(requirement)
    values = [
        str(value).strip()
        for value in list(result.get("allowed_values") or [])
        if str(value).strip()
    ]
    result["allowed_value_count"] = len(values)
    if (
        str(result.get("storage_field") or "") == "spells"
        and str(result.get("option_source") or "")
        and len(values) > max(0, int(inline_value_limit))
    ):
        result.pop("allowed_values", None)
        result["catalog_query"] = {
            "kind": "spell",
            "school": str(result["option_source"]),
            "view": "shortlist",
            "limit": 3,
        }
    else:
        result["allowed_values"] = values
    return result


def compact_skill_choice_requirements(
    requirements: Iterable[dict[str, object]],
    *,
    inline_value_limit: int = 5,
) -> list[dict[str, object]]:
    return [
        compact_skill_choice_requirement(
            requirement,
            inline_value_limit=inline_value_limit,
        )
        for requirement in requirements
    ]


def skill_implementation_coverage(name: str) -> SkillImplementationCoverage | None:
    canonical = normalize_skill_reference_name(name)
    reference = get_skill_reference(canonical)
    if reference is None:
        return None
    if canonical in _HARD_RULE_SKILLS:
        category = SKILL_COVERAGE_HARD_RULE
    elif canonical in _PASSIVE_HARD_SKILLS:
        category = SKILL_COVERAGE_PASSIVE_HARD
    elif canonical in _GM_JUDGEMENT_SKILLS:
        category = SKILL_COVERAGE_GM_JUDGEMENT
    else:
        category = SKILL_COVERAGE_REFERENCE_ONLY
    return SkillImplementationCoverage(
        name=reference.name,
        kind=reference.kind,
        class_name=reference.class_name,
        category=category,
        summary=reference.summary,
        implementation_note=_COVERAGE_NOTES[category],
    )


def skill_implementation_table(*, kind: str = "", class_name: str = "") -> list[SkillImplementationCoverage]:
    rows: list[SkillImplementationCoverage] = []
    for reference in SKILL_REFERENCES:
        if kind and reference.kind != kind:
            continue
        if class_name and reference.class_name != class_name:
            continue
        coverage = skill_implementation_coverage(reference.name)
        if coverage is not None:
            rows.append(coverage)
    return rows


def get_skill_reference(name: str) -> SkillReference | None:
    return SKILL_REFERENCES_BY_NAME.get(name.strip())


def get_class_reference(name: str) -> ClassReference | None:
    return CLASS_REFERENCES_BY_NAME.get(str(name or "").strip())


def search_class_references(
    *,
    text: str = "",
    tags: Iterable[str] = (),
    limit: int = 15,
) -> list[ClassReference]:
    """Search core classes without exposing every class skill to the caller."""

    query = str(text or "").strip().lower()
    tag_terms = [str(tag or "").strip().lower() for tag in tags if str(tag or "").strip()]
    terms = [term for term in (query, *tag_terms) if term]
    ranked: list[tuple[int, int, ClassReference]] = []
    for index, reference in enumerate(CLASS_REFERENCES):
        haystack = " ".join(
            [reference.name, reference.summary, *reference.tags, *reference.abilities]
        ).lower()
        if terms:
            score = sum(1 for term in terms if term in haystack)
            if query == reference.name.lower():
                score += 100
            if score <= 0:
                continue
        else:
            score = 0
        ranked.append((score, -index, reference))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [reference for _score, _index, reference in ranked[: max(1, int(limit))]]


def search_skill_references(
    *,
    kind: str = "",
    class_name: str = "",
    text: str = "",
    limit: int = 20,
) -> list[SkillReference]:
    query = text.strip().lower()
    results: list[SkillReference] = []
    for reference in SKILL_REFERENCES:
        if kind and reference.kind != kind:
            continue
        if class_name and reference.class_name != class_name:
            continue
        if query:
            haystack = " ".join(
                [reference.name, reference.kind, reference.class_name, reference.summary, *reference.aliases, *reference.tags]
            ).lower()
            if query not in haystack:
                continue
        results.append(reference)
        if len(results) >= limit:
            break
    return results

