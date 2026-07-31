from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from fu_gm.models import EquipmentItemType


@dataclass(frozen=True)
class EquipmentExample:
    """规则书示例装备条目，供宝箱、商店、奖励与 AI GM 检索参考。"""

    name: str
    item_type: EquipmentItemType
    price: int | None = None
    category: str = ""
    accuracy_attributes: tuple[str, str] | tuple[()] = ()
    accuracy_modifier: int = 0
    damage_bonus: int = 0
    damage_type: str = ""
    hands: int = 0
    range_type: str = ""
    physical_defense: str = ""
    magic_defense: str = ""
    initiative_modifier: int = 0
    required_ability: str = ""
    effects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        parts = [self.name]
        if self.price is not None:
            parts.append(f"{self.price}Z")
        if self.item_type == EquipmentItemType.WEAPON:
            formula = "+".join(self.accuracy_attributes)
            if self.accuracy_modifier:
                formula += f"+{self.accuracy_modifier}"
            parts.append(f"{self.category}，命中 {formula}，伤害 HR+{self.damage_bonus} {self.damage_type}")
        elif self.item_type == EquipmentItemType.ARMOR:
            parts.append(f"防具，物防 {self.physical_defense}，魔防 {self.magic_defense}，先攻 {self.initiative_modifier:+d}")
        elif self.item_type == EquipmentItemType.SHIELD:
            parts.append(f"盾牌，物防 {self.physical_defense}，魔防 {self.magic_defense}")
        elif self.item_type == EquipmentItemType.ACCESSORY:
            parts.append("饰品")
        else:
            parts.append("神器")
        if self.effects:
            parts.append("；".join(self.effects))
        return "；".join(parts)


def weapon(
    name: str,
    price: int,
    category: str,
    acc: tuple[str, str],
    damage_bonus: int,
    damage_type: str,
    hands: int,
    range_type: str,
    *effects: str,
    accuracy_modifier: int = 0,
    required_ability: str = "",
    aliases: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> EquipmentExample:
    return EquipmentExample(
        name=name,
        item_type=EquipmentItemType.WEAPON,
        price=price,
        category=category,
        accuracy_attributes=acc,
        accuracy_modifier=accuracy_modifier,
        damage_bonus=damage_bonus,
        damage_type=damage_type,
        hands=hands,
        range_type=range_type,
        required_ability=required_ability,
        effects=effects,
        aliases=aliases,
        tags=tags,
    )


def armor(
    name: str,
    price: int,
    physical_defense: str,
    magic_defense: str,
    initiative_modifier: int,
    *effects: str,
    required_ability: str = "",
    aliases: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> EquipmentExample:
    return EquipmentExample(
        name=name,
        item_type=EquipmentItemType.ARMOR,
        price=price,
        physical_defense=physical_defense,
        magic_defense=magic_defense,
        initiative_modifier=initiative_modifier,
        required_ability=required_ability,
        effects=effects,
        aliases=aliases,
        tags=tags,
    )


def shield(
    name: str,
    price: int,
    physical_defense: str,
    magic_defense: str,
    *effects: str,
    required_ability: str = "",
    aliases: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> EquipmentExample:
    return EquipmentExample(
        name=name,
        item_type=EquipmentItemType.SHIELD,
        price=price,
        physical_defense=physical_defense,
        magic_defense=magic_defense,
        required_ability=required_ability,
        effects=effects,
        aliases=aliases,
        tags=tags,
    )


def accessory(name: str, price: int, *effects: str, aliases: tuple[str, ...] = (), tags: tuple[str, ...] = ()) -> EquipmentExample:
    return EquipmentExample(
        name=name,
        item_type=EquipmentItemType.ACCESSORY,
        price=price,
        effects=effects,
        aliases=aliases,
        tags=tags,
    )


def artifact(name: str, *effects: str, aliases: tuple[str, ...] = (), tags: tuple[str, ...] = ()) -> EquipmentExample:
    return EquipmentExample(
        name=name,
        item_type=EquipmentItemType.ARTIFACT,
        price=None,
        effects=effects,
        aliases=aliases,
        tags=tags,
    )


BASIC_WEAPON_EXAMPLES: tuple[EquipmentExample, ...] = (
    weapon("法杖", 100, "魔法", ("WLP", "WLP"), 6, "physical", 2, "melee", "无特殊效果", tags=("basic",)),
    weapon("魔典", 100, "魔法", ("INS", "INS"), 6, "physical", 2, "melee", "无特殊效果", tags=("basic",)),
    weapon("十字弩", 150, "弓", ("DEX", "INS"), 8, "physical", 2, "ranged", "无特殊效果", tags=("basic",)),
    weapon("短弓", 200, "弓", ("DEX", "DEX"), 8, "physical", 2, "ranged", "无特殊效果", tags=("basic",)),
    weapon("徒手攻击", 0, "格斗", ("DEX", "MIG"), 0, "physical", 1, "melee", "自动装备至空闲手部", tags=("basic",)),
    weapon("临时武器（近战）", 0, "格斗", ("DEX", "MIG"), 2, "physical", 1, "melee", "攻击后损毁", aliases=("临时武器(近战)",), tags=("basic", "improvised")),
    weapon("铁指虎", 150, "格斗", ("DEX", "MIG"), 6, "physical", 1, "melee", "无特殊效果", tags=("basic",)),
    weapon("钢匕首", 150, "匕首", ("DEX", "INS"), 4, "physical", 1, "melee", "无特殊效果", accuracy_modifier=1, tags=("basic",)),
    weapon("手枪", 250, "枪械", ("DEX", "INS"), 8, "physical", 1, "ranged", "无特殊效果", required_ability="可装备职业远程武器", tags=("basic",)),
    weapon("链鞭", 150, "链枷", ("DEX", "DEX"), 8, "physical", 2, "melee", "无特殊效果", tags=("basic",)),
    weapon("铁锤", 200, "重型", ("MIG", "MIG"), 6, "physical", 1, "melee", "无特殊效果", tags=("basic",)),
    weapon("阔斧", 250, "重型", ("MIG", "MIG"), 10, "physical", 1, "melee", "无特殊效果", required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("战斧", 250, "重型", ("MIG", "MIG"), 14, "physical", 2, "melee", "无特殊效果", required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("轻矛", 200, "矛", ("DEX", "MIG"), 8, "physical", 1, "melee", "无特殊效果", required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("重矛", 200, "矛", ("DEX", "MIG"), 12, "physical", 2, "melee", "无特殊效果", required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("青铜剑", 200, "剑", ("DEX", "MIG"), 6, "physical", 1, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("巨剑", 200, "剑", ("DEX", "MIG"), 10, "physical", 2, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("武士刀", 200, "剑", ("DEX", "INS"), 10, "physical", 2, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("细剑", 200, "剑", ("DEX", "INS"), 6, "physical", 1, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器", tags=("basic",)),
    weapon("临时武器（远程）", 0, "投掷", ("DEX", "MIG"), 2, "physical", 1, "ranged", "攻击后损毁", aliases=("临时武器(远程)",), tags=("basic", "improvised")),
    weapon("手里剑", 150, "投掷", ("DEX", "INS"), 4, "physical", 1, "ranged", "无特殊效果", tags=("basic",)),
)


BASIC_ARMOR_EXAMPLES: tuple[EquipmentExample, ...] = (
    armor("无防具", 0, "DEX", "INS", 0, "无特殊效果", tags=("basic",)),
    armor("丝质衬衫", 100, "DEX+1", "INS+2", -1, "无特殊效果", tags=("basic",)),
    armor("旅行装束", 100, "DEX+1", "INS+1", -1, "无特殊效果", tags=("basic",)),
    armor("武道服", 150, "DEX+1", "INS+1", 0, "无特殊效果", tags=("basic",)),
    armor("贤者之袍", 200, "DEX+1", "INS+2", -2, "无特殊效果", tags=("basic",)),
    armor("板甲衣", 150, "10", "INS", -2, "无特殊效果", required_ability="可装备职业盔甲", tags=("basic",)),
    armor("青铜板甲", 200, "11", "INS", -3, "无特殊效果", required_ability="可装备职业盔甲", tags=("basic",)),
    armor("符文板甲", 250, "11", "INS+1", -3, "无特殊效果", required_ability="可装备职业盔甲", tags=("basic",)),
    armor("钢制板甲", 300, "12", "INS", -4, "无特殊效果", required_ability="可装备职业盔甲", tags=("basic",)),
)


BASIC_SHIELD_EXAMPLES: tuple[EquipmentExample, ...] = (
    shield("青铜盾", 100, "+2", "0", "无特殊效果", tags=("basic",)),
    shield("符文盾", 150, "+2", "+2", "无特殊效果", required_ability="可装备职业盾牌", tags=("basic",)),
)


BASIC_EQUIPMENT_EXAMPLES: tuple[EquipmentExample, ...] = (
    *BASIC_WEAPON_EXAMPLES,
    *BASIC_ARMOR_EXAMPLES,
    *BASIC_SHIELD_EXAMPLES,
)


RARE_WEAPON_EXAMPLES: tuple[EquipmentExample, ...] = (
    weapon("祝福权杖", 200, "魔法", ("WLP", "WLP"), 2, "light", 1, "melee", "无特殊效果"),
    weapon("百科全书", 600, "魔法", ("INS", "INS"), 6, "physical", 2, "melee", "免疫眩晕"),
    weapon("镇魔之书", 800, "魔法", ("INS", "INS"), 6, "light", 2, "melee", "对恶魔进行施法检定和对抗检定获得 +2", aliases=("案镇魔之书",)),
    weapon("教主之杖", 1050, "魔法", ("INS", "WLP"), 2, "physical", 1, "melee", "施法检定获得 +1"),
    weapon("暴君权杖", 1200, "魔法", ("WLP", "WLP"), 6, "dark", 2, "melee", "命中一个或多个生物时，每个目标损失 10 MP"),
    weapon("尸鬼教典仪", 1400, "魔法", ("INS", "INS"), 6, "wind", 1, "melee", "命中一个或多个生物时，恢复 5 HP"),
    weapon("神使节杖", 1600, "魔法", ("WLP", "WLP"), 6, "physical", 2, "melee", "施放恢复 HP 的法术时，额外恢复 5 HP"),
    weapon("死灵之书", 1800, "魔法", ("INS", "WLP"), 6, "dark", 2, "melee", "施放攻击性法术命中时，对每个目标施加动摇", accuracy_modifier=1),
    weapon("黄衣之书", 2100, "魔法", ("INS", "INS"), 6, "physical", 2, "melee", "你施放的法术造成 5 点额外伤害"),
    weapon("王花之杖", 2200, "魔法", ("WLP", "WLP"), 6, "poison", 2, "melee", "施放攻击性法术命中时，对每个目标施加中毒"),
    weapon("手弩", 150, "弓", ("DEX", "INS"), 4, "physical", 1, "ranged", "无特殊效果"),
    weapon("复合弓", 250, "弓", ("DEX", "MIG"), 8, "physical", 2, "ranged", "无特殊效果", accuracy_modifier=1),
    weapon("破城弩", 750, "弓", ("DEX", "INS"), 12, "physical", 2, "ranged", "造成的伤害无视抵抗相性", required_ability="可装备职业远程武器"),
    weapon("与一之弓", 900, "弓", ("DEX", "DEX"), 8, "wind", 2, "ranged", "免疫动摇", accuracy_modifier=1),
    weapon("雷霆之弓", 1000, "弓", ("DEX", "DEX"), 8, "lightning", 2, "ranged", "对雷系伤害获得抵抗相性"),
    weapon("劫掠弓", 1250, "弓", ("DEX", "INS"), 8, "fire", 2, "ranged", "用此武器将生物 HP 降为 0 时，恢复 2 IP"),
    weapon("加特林弩", 1350, "弓", ("DEX", "INS"), 12, "physical", 2, "ranged", "攻击具有多重攻击（2）特性", required_ability="可装备职业远程武器"),
    weapon("缚龙弓", 1500, "弓", ("DEX", "DEX"), 12, "earth", 2, "ranged", "命中飞行生物时，可迫使目标立刻落地", required_ability="可装备职业远程武器"),
    weapon("冰冻之妒", 1500, "弓", ("DEX", "DEX"), 12, "ice", 2, "ranged", "若拥有至少一段包含自卑的羁绊，命中后恢复 5 MP", required_ability="可装备职业远程武器"),
    weapon("戈尔贡之眼", 2000, "弓", ("DEX", "DEX"), 12, "poison", 2, "ranged", "对每个命中的目标施加迟缓", required_ability="可装备职业远程武器"),
    weapon("阿尔忒弥斯", 2100, "弓", ("DEX", "DEX"), 12, "light", 2, "ranged", "免疫暗系伤害", accuracy_modifier=1, required_ability="可装备职业远程武器"),
    weapon("猫爪拳", 250, "格斗", ("DEX", "MIG"), 6, "physical", 1, "melee", "无特殊效果", accuracy_modifier=1),
    weapon("地狱指虎", 350, "格斗", ("DEX", "MIG"), 6, "dark", 1, "melee", "攻击针对目标魔防"),
    weapon("寒冰之攫", 750, "格斗", ("DEX", "MIG"), 6, "ice", 1, "melee", "免疫激怒"),
    weapon("熊掌", 850, "格斗", ("DEX", "MIG"), 10, "physical", 1, "melee", "免疫虚弱", required_ability="可装备职业近战武器"),
    weapon("燃焰指虎", 950, "格斗", ("DEX", "MIG"), 6, "fire", 1, "melee", "对火系伤害获得抵抗相性"),
    weapon("银爪", 1100, "格斗", ("DEX", "DEX"), 6, "light", 1, "melee", "魔防获得 +1"),
    weapon("破旧绷带", 1250, "格斗", ("DEX", "MIG"), 6, "physical", 1, "melee", "对暗系和毒系伤害获得抵抗相性", accuracy_modifier=1),
    weapon("风暴之拳", 1300, "格斗", ("MIG", "MIG"), 6, "lightning", 1, "melee", "攻击具有多重攻击（2）特性"),
    weapon("龙虾钳", 1950, "格斗", ("DEX", "MIG"), 10, "physical", 1, "melee", "对每个命中的目标施加迟缓", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("震荡拳套", 2000, "格斗", ("MIG", "MIG"), 10, "earth", 1, "melee", "对每个命中的目标施加眩晕", required_ability="可装备职业近战武器"),
    weapon("毒爪", 2250, "格斗", ("DEX", "MIG"), 6, "physical", 1, "melee", "对每个命中的目标施加中毒", accuracy_modifier=1),
    weapon("神之手", 2550, "格斗", ("DEX", "MIG"), 10, "light", 1, "melee", "造成的伤害无视所有相性", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("黑寡妇", 250, "匕首", ("DEX", "INS"), 4, "poison", 1, "melee", "无特殊效果", accuracy_modifier=1),
    weapon("剜心刃", 550, "匕首", ("DEX", "WLP"), 4, "light", 1, "melee", "对恶魔造成 5 点额外伤害"),
    weapon("原子切割者", 600, "匕首", ("DEX", "DEX"), 4, "physical", 1, "melee", "造成的伤害无视抵抗相性", accuracy_modifier=1),
    weapon("寂静之刃", 700, "匕首", ("DEX", "DEX"), 4, "wind", 1, "melee", "免疫迟缓"),
    weapon("破法之刃", 850, "匕首", ("DEX", "INS"), 4, "dark", 1, "melee", "若攻击唯一目标，命中后可移除目标一个持续到场景结束的效果", accuracy_modifier=1),
    weapon("刺客匕首", 1000, "匕首", ("DEX", "INS"), 4, "physical", 1, "melee", "对处于危机状态的目标造成 5 点额外伤害", accuracy_modifier=1),
    weapon("飨宴菜刀", 1350, "匕首", ("DEX", "INS"), 8, "physical", 1, "melee", "命中一个或多个生物时，恢复 5 HP", accuracy_modifier=1),
    weapon("锯齿刃", 1650, "匕首", ("DEX", "INS"), 4, "physical", 1, "melee", "对每个命中的目标施加动摇", accuracy_modifier=1),
    weapon("寒霜之指", 1950, "匕首", ("DEX", "INS"), 8, "ice", 1, "melee", "对每个命中的目标施加虚弱", accuracy_modifier=1),
    weapon("大黄蜂", 2200, "匕首", ("DEX", "DEX"), 4, "physical", 1, "melee", "攻击具有多重攻击（3）特性", accuracy_modifier=1),
    weapon("狂乱之钉", 2450, "匕首", ("INS", "INS"), 8, "fire", 1, "melee", "对每个命中的目标施加激怒", accuracy_modifier=1),
    weapon("左轮手枪", 300, "枪械", ("DEX", "DEX"), 8, "physical", 1, "ranged", "无特殊效果", required_ability="可装备职业远程武器"),
    weapon("燧发枪", 350, "枪械", ("DEX", "INS"), 12, "physical", 2, "ranged", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业远程武器"),
    weapon("咒射铳", 400, "枪械", ("INS", "INS"), 8, "physical", 1, "ranged", "攻击针对目标魔防", required_ability="可装备职业远程武器"),
    weapon("钻石手枪", 650, "枪械", ("DEX", "INS"), 8, "physical", 1, "ranged", "对构装体造成 5 点额外伤害", accuracy_modifier=1, required_ability="可装备职业远程武器"),
    weapon("猎头铳", 800, "枪械", ("DEX", "INS"), 8, "physical", 1, "ranged", "攻击你带有憎恨羁绊的目标时造成 5 点额外伤害", required_ability="可装备职业远程武器"),
    weapon("彗星枪", 950, "枪械", ("DEX", "INS"), 8, "dark", 1, "ranged", "免疫眩晕", accuracy_modifier=1, required_ability="可装备职业远程武器"),
    weapon("地堡加农炮", 1050, "枪械", ("DEX", "INS"), 12, "physical", 2, "ranged", "物防获得 +1", required_ability="可装备职业远程武器"),
    weapon("炼金火枪", 1300, "枪械", ("DEX", "INS"), 8, "poison", 1, "ranged", "消耗 IP 制作的药剂造成 5 点额外伤害，或额外恢复 5 HP", required_ability="可装备职业远程武器"),
    weapon("灾厄", 1550, "枪械", ("DEX", "INS"), 16, "fire", 2, "ranged", "攻击具有多重攻击（2）特性", required_ability="可装备职业远程武器"),
    weapon("暴风雪", 1850, "枪械", ("DEX", "INS"), 8, "ice", 1, "ranged", "对每个命中的目标施加迟缓", required_ability="可装备职业远程武器"),
    weapon("终结者", 2600, "枪械", ("DEX", "INS"), 12, "wind", 2, "ranged", "造成额外伤害，数值等同当前 IP 与最大 IP 的差值", accuracy_modifier=1, required_ability="可装备职业远程武器"),
    weapon("旧鞭", 650, "链枷", ("DEX", "DEX"), 8, "physical", 2, "melee", "对野兽和怪物造成 5 点额外伤害"),
    weapon("暮星", 750, "链枷", ("DEX", "DEX"), 4, "dark", 1, "melee", "免疫动摇"),
    weapon("女巫克星", 800, "链枷", ("DEX", "DEX"), 8, "physical", 2, "melee", "造成的伤害扣除 MP 而非 HP；MP 降为 0 后，多余伤害照常扣 HP", accuracy_modifier=1, aliases=("焉女巫克星",)),
    weapon("战火蜥蜴", 1000, "链枷", ("DEX", "MIG"), 8, "physical", 1, "melee", "对火系伤害具有抵抗相性"),
    weapon("双节棍", 1100, "链枷", ("DEX", "MIG"), 8, "physical", 1, "melee", "物防获得 +1"),
    weapon("支配者", 1200, "链枷", ("DEX", "WLP"), 8, "fire", 1, "melee", "对处于激怒状态的目标进行命中和施法检定获得 +2"),
    weapon("刃鞭", 1400, "链枷", ("DEX", "MIG"), 12, "physical", 2, "melee", "攻击具有多重攻击（2）特性", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("丝线", 1450, "链枷", ("DEX", "DEX"), 12, "physical", 2, "melee", "对物理伤害具有抵抗相性", required_ability="可装备职业近战武器"),
    weapon("锁镰", 1650, "链枷", ("DEX", "DEX"), 8, "physical", 2, "melee", "对每个命中的目标施加迟缓"),
    weapon("耶梦加得", 2400, "链枷", ("DEX", "MIG"), 12, "dark", 2, "melee", "攻击具有多重攻击（3）特性", required_ability="可装备职业近战武器"),
    weapon("锦鲤须", 2800, "链枷", ("DEX", "WLP"), 12, "physical", 2, "melee", "免疫暗系和光系伤害", required_ability="可装备职业近战武器"),
    weapon("月刃斧", 350, "重型", ("MIG", "MIG"), 14, "physical", 2, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("神匠巨槌", 450, "重型", ("INS", "MIG"), 6, "physical", 1, "melee", "对构装体造成 5 点额外伤害", required_ability="可装备职业近战武器"),
    weapon("贝奥武夫", 550, "重型", ("MIG", "MIG"), 10, "physical", 1, "melee", "对怪物造成 5 点额外伤害", required_ability="可装备职业近战武器"),
    weapon("野兽之腹", 650, "重型", ("MIG", "MIG"), 14, "poison", 2, "melee", "对人型生物造成 5 点额外伤害", required_ability="可装备职业近战武器"),
    weapon("巡林短柄斧", 750, "重型", ("MIG", "MIG"), 10, "physical", 1, "melee", "对野兽和植物造成 5 点额外伤害", required_ability="可装备职业近战武器"),
    weapon("精金锤", 1050, "重型", ("MIG", "MIG"), 14, "physical", 2, "melee", "物防获得 +1", required_ability="可装备职业近战武器"),
    weapon("圣光之锤", 1350, "重型", ("MIG", "MIG"), 14, "light", 2, "melee", "攻击具有多重攻击（2）特性", required_ability="可装备职业近战武器"),
    weapon("重力钉头槌", 1850, "重型", ("MIG", "MIG"), 14, "earth", 2, "melee", "对每个命中的目标施加迟缓", required_ability="可装备职业近战武器"),
    weapon("雷神之锤", 1850, "重型", ("MIG", "MIG"), 10, "lightning", 1, "melee", "对每个命中的目标施加眩晕", required_ability="可装备职业近战武器"),
    weapon("妖龙之翼", 2050, "重型", ("MIG", "MIG"), 18, "fire", 2, "melee", "免疫火系伤害", required_ability="可装备职业近战武器"),
    weapon("掠夺之魂", 2550, "重型", ("MIG", "MIG"), 18, "dark", 2, "melee", "对每个命中的目标施加激怒", required_ability="可装备职业近战武器"),
    weapon("凛冬巨像", 2550, "重型", ("MIG", "MIG"), 18, "ice", 2, "melee", "物防和魔防获得 +1", required_ability="可装备职业近战武器"),
    weapon("龙舌", 500, "矛", ("DEX", "MIG"), 12, "fire", 2, "melee", "攻击针对目标魔防", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("驽驿难得", 500, "矛", ("DEX", "MIG"), 8, "physical", 1, "melee", "每有一个异常状态，造成 1 点额外伤害", aliases=("OCR疑似：驽驿难得",)),
    weapon("蛇矛", 800, "矛", ("DEX", "MIG"), 16, "physical", 2, "melee", "造成的伤害无视抵抗相性", required_ability="可装备职业近战武器"),
    weapon("长戟", 1000, "矛", ("DEX", "MIG"), 12, "physical", 2, "melee", "物防获得 +1", required_ability="可装备职业近战武器"),
    weapon("仙鲸角", 1200, "矛", ("DEX", "MIG"), 12, "ice", 1, "melee", "对冰系伤害具有抵抗相性"),
    weapon("勇气月戟", 1300, "矛", ("MIG", "WLP"), 12, "earth", 2, "melee", "若拥有至少三段包含忠诚或喜爱的羁绊，物防和魔防获得 +1", required_ability="可装备职业近战武器"),
    weapon("莫瑞甘", 1400, "矛", ("DEX", "MIG"), 12, "dark", 2, "melee", "命中一个或多个生物时，恢复 10 MP", accuracy_modifier=1),
    weapon("迦耶伯格", 1800, "矛", ("DEX", "MIG"), 12, "physical", 2, "melee", "用此武器掷出大成功时，可将机会用于造成 10 点额外伤害", accuracy_modifier=1),
    weapon("朗基努斯", 2000, "矛", ("DEX", "MIG"), 16, "physical", 2, "melee", "对每个命中的目标施加虚弱", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("九齿钉耙", 2500, "矛", ("DEX", "MIG"), 16, "poison", 2, "melee", "吸收毒系伤害", required_ability="可装备职业近战武器"),
    weapon("骏冈格尼尔", 3000, "矛", ("DEX", "MIG"), 16, "light", 2, "melee", "免疫火系和冰系伤害", required_ability="可装备职业近战武器"),
    weapon("双手剑", 400, "剑", ("DEX", "MIG"), 14, "physical", 2, "melee", "无特殊效果", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("落雨", 450, "剑", ("DEX", "DEX"), 10, "ice", 2, "melee", "攻击针对目标魔防", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("焰形剑", 500, "剑", ("DEX", "MIG"), 10, "fire", 1, "melee", "无特殊效果", accuracy_modifier=1),
    weapon("优雅之刃", 700, "剑", ("DEX", "INS"), 6, "physical", 1, "melee", "免疫激怒", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("咎瓦尤斯", 900, "剑", ("MIG", "WLP"), 10, "physical", 1, "melee", "免疫动摇", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("死亡之刃", 1000, "剑", ("DEX", "MIG"), 6, "dark", 1, "melee", "处于危机状态时，造成 5 点额外伤害", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("枪剑", 1000, "剑", ("DEX", "MIG"), 10, "physical", 2, "melee", "可以攻击飞行生物", accuracy_modifier=1),
    weapon("格挡短剑", 1000, "剑", ("DEX", "MIG"), 6, "physical", 1, "melee", "物防获得 +1", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("藏锋剑", 1200, "剑", ("DEX", "INS"), 10, "physical", 2, "melee", "每精通一个职业，造成 2 点额外伤害", accuracy_modifier=1),
    weapon("吞食者", 1300, "剑", ("MIG", "MIG"), 10, "poison", 1, "melee", "对处于虚弱状态的目标造成 5 点额外伤害", required_ability="可装备职业近战武器"),
    weapon("草薙", 1500, "剑", ("DEX", "MIG"), 14, "wind", 2, "melee", "攻击具有多重攻击（2）特性", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("断钢剑", 2300, "剑", ("MIG", "WLP"), 10, "light", 2, "melee", "免疫所有异常状态", accuracy_modifier=1, required_ability="可装备职业近战武器"),
    weapon("新月之刃", 350, "投掷", ("DEX", "INS"), 4, "light", 1, "ranged", "攻击针对目标魔防"),
    weapon("陨星", 350, "投掷", ("DEX", "INS"), 4, "fire", 1, "ranged", "无特殊效果", accuracy_modifier=1),
    weapon("飞斧", 350, "投掷", ("DEX", "MIG"), 8, "physical", 1, "ranged", "无特殊效果"),
    weapon("回旋镖", 750, "投掷", ("DEX", "MIG"), 4, "physical", 1, "ranged", "对野兽和怪物造成 5 点额外伤害", accuracy_modifier=1),
    weapon("风舞", 850, "投掷", ("DEX", "WLP"), 8, "wind", 1, "ranged", "造成的伤害无视抵抗相性"),
    weapon("梨花针", 950, "投掷", ("DEX", "INS"), 8, "physical", 1, "ranged", "免疫中毒", accuracy_modifier=1),
    weapon("蓝色风车", 950, "投掷", ("DEX", "INS"), 4, "ice", 1, "ranged", "对冰系伤害具有抵抗相性"),
    weapon("巫婆缝衣针", 1050, "投掷", ("DEX", "INS"), 4, "earth", 1, "ranged", "对暗系伤害具有抵抗相性", accuracy_modifier=1),
    weapon("轮刃", 1250, "投掷", ("DEX", "MIG"), 4, "physical", 1, "ranged", "攻击具有多重攻击（2）特性", accuracy_modifier=1),
    weapon("金刚杵", 2050, "投掷", ("DEX", "WLP"), 8, "lightning", 1, "ranged", "对每个命中的目标施加动摇", accuracy_modifier=1),
    weapon("暗之轨迹", 2250, "投掷", ("DEX", "INS"), 4, "dark", 1, "ranged", "物防和魔防获得 +1"),
    weapon("毒蜂镖", 2300, "投掷", ("DEX", "DEX"), 4, "poison", 1, "ranged", "对每个命中的目标施加中毒"),
)


RARE_ARMOR_EXAMPLES: tuple[EquipmentExample, ...] = (
    armor("史莱姆夹克", 600, "DEX+1", "INS+1", -1, "免疫中毒"),
    armor("灵狐装束", 650, "DEX+1", "INS+1", 0, "免疫迟缓"),
    armor("暗影罩袍", 650, "DEX+1", "INS+1", 4, "先攻获得 +4 修正值（已计入）"),
    armor("狂徒外套", 750, "DEX+1", "INS+1", -1, "使用神射手技能弹幕射击时，MP 消耗减半"),
    armor("管家制服", 800, "DEX+1", "INS+2", -2, "制造恢复 HP 的药剂或法球时，HP 恢复量 +5"),
    armor("女仆制服", 800, "DEX+1", "INS+2", -2, "制造恢复 MP 的药剂或法球时，MP 恢复量 +5"),
    armor("强盗外衣", 900, "DEX+1", "INS+1", -1, "使用匕首类武器攻击时，命中检定 +1"),
    armor("水晶板甲", 900, "11", "INS", -3, "对暗系伤害具有抵抗相性", required_ability="可装备职业盔甲"),
    armor("女武神之翼", 900, "11", "INS+1", -3, "施放元素使法术飞天打击时，MP 消耗减半", required_ability="可装备职业盔甲"),
    armor("英雄之铠", 1000, "12", "INS", -4, "敌人攻击或攻击性法术选择你为目标并掷出大成功时，不获得机会效果", required_ability="可装备职业盔甲"),
    armor("黑带", 1000, "DEX", "INS", 0, "使用格斗类武器攻击造成 5 点额外伤害"),
    armor("冥想之袍", 1000, "DEX+1", "INS+2", -2, "当你恢复 MP 时，额外恢复 5 MP"),
    armor("大法师之袍", 1200, "DEX+1", "INS+2", -2, "施法检定获得 +1"),
    armor("机甲外装", 1250, "11", "INS+1", -3, "免疫土系和毒系伤害，但对雷系伤害处于弱点状态", required_ability="可装备职业盔甲"),
    armor("精金胸甲", 1300, "12", "INS", -4, "对物理伤害具有抵抗相性", required_ability="可装备职业盔甲"),
    armor("热情之铠", 1300, "12", "INS", -4, "命中检定获得 +1", required_ability="可装备职业盔甲"),
    armor("恶魔的狞笑", 1500, "12", "INS", -4, "一个生物的近战攻击命中你后，对该生物造成 5 点火系伤害", required_ability="可装备职业盔甲"),
    armor("生化板甲", 1700, "11", "INS", -3, "免疫毒系伤害", required_ability="可装备职业盔甲"),
    armor("白色罩袍", 1700, "DEX+1", "INS+2", -2, "施放恢复 HP 的法术时，额外恢复 5 HP"),
    armor("奶奶的背心", 2000, "DEX", "INS+2", -1, "装备时意志骰尺寸提升一级，最高 d12"),
    armor("黑色罩袍", 2200, "DEX+1", "INS+2", -2, "你施放的法术造成 5 点额外伤害"),
    armor("红色罩袍", 2500, "DEX", "INS+2", -1, "装备时，你能满足“如果你装备了魔法类武器”的技能触发条件"),
)


RARE_SHIELD_EXAMPLES: tuple[EquipmentExample, ...] = (
    shield("闪电圣盾", 800, "+2", "0", "对雷系伤害具有抵抗相性"),
    shield("寒冰圣盾", 800, "+2", "0", "对冰系伤害具有抵抗相性"),
    shield("灵蛇圣盾", 800, "+2", "0", "对毒系伤害具有抵抗相性"),
    shield("炎魔圣盾", 800, "+2", "0", "对火系伤害具有抵抗相性"),
    shield("光耀圣盾", 800, "+2", "0", "对光系伤害具有抵抗相性"),
    shield("大地圣盾", 800, "+2", "0", "对土系伤害具有抵抗相性"),
    shield("幽影圣盾", 800, "+2", "0", "对暗系伤害具有抵抗相性"),
    shield("罡风圣盾", 800, "+2", "0", "对风系伤害具有抵抗相性"),
    shield("恶魔之盾", 950, "+2", "+2", "受到伤害后，若你处于危机状态，可对伤害来源施加动摇", required_ability="可装备职业盾牌"),
    shield("回春之盾", 1150, "+2", "+2", "当你恢复 HP 时，额外恢复 5 HP", required_ability="可装备职业盾牌"),
    shield("炽天使之盾", 2050, "+2", "+2", "若处于危机状态，免疫所有异常状态"),
    shield("精金塔盾", 2500, "+3", "+3", "物防和魔防获得 +1 修正值（已计入）", required_ability="可装备职业盾牌"),
)


RARE_ACCESSORY_EXAMPLES: tuple[EquipmentExample, ...] = (
    accessory("探险家腰带", 500, "先攻获得 +4 修正值"),
    accessory("优雅手套", 500, "免疫眩晕"),
    accessory("粗糙手套", 500, "免疫虚弱"),
    accessory("丝质手套", 500, "免疫迟缓"),
    accessory("暖心手套", 500, "免疫动摇"),
    accessory("新手靴", 600, "掷出大失败时，若经验值低于 10 点，获得 1 XP"),
    accessory("般若面具", 700, "对处于动摇状态的生物造成的伤害无视目标抵抗相性"),
    accessory("琥珀吊坠", 700, "对土系伤害具有抵抗相性"),
    accessory("紫水晶吊坠", 700, "对暗系伤害具有抵抗相性"),
    accessory("钻石吊坠", 700, "对光系伤害具有抵抗相性"),
    accessory("翡翠吊坠", 700, "对毒系伤害具有抵抗相性"),
    accessory("猫眼石吊坠", 700, "对风系伤害具有抵抗相性"),
    accessory("红宝石吊坠", 700, "对火系伤害具有抵抗相性"),
    accessory("蓝宝石吊坠", 700, "对冰系伤害具有抵抗相性"),
    accessory("黄宝石吊坠", 700, "对雷系伤害具有抵抗相性"),
    accessory("巫术戒指", 800, "魔防获得 +1 修正值"),
    accessory("游荡者之靴", 900, "队伍旅行获得发现时，你获得 1 点物语点"),
    accessory("缨盔", 1000, "命中检定获得 +1 修正值"),
    accessory("绯红手套", 1000, "进行带有多重攻击特性的攻击时，命中检定获得 +2 修正值"),
    accessory("黄色尖帽", 1000, "施法检定获得 +1 修正值"),
    accessory("雄狮戒指", 1500, "使用 WLP 进行对抗检定时获得 +2 修正值"),
    accessory("猫头鹰戒指", 1500, "使用 INS 进行对抗检定时获得 +2 修正值"),
    accessory("学徒戒指", 1500, "若拥有至少两段包含钦佩的羁绊，物防和魔防获得 +1 修正值"),
    accessory("物语之戒", 1500, "掷出大成功时，可以将此次机会用于获得 1 点物语点"),
    accessory("蜕生手套", 2000, "免疫所有异常状态"),
    accessory("洋葱之戒", 2000, "每拥有一个职业，最大 HP 和最大 MP 提升 2 点"),
    accessory("寒霜戒指", 2500, "吸收冰系伤害，但对火系伤害处于弱点状态"),
    accessory("熔岩戒指", 2500, "吸收火系伤害，但对冰系伤害处于弱点状态"),
    accessory("重生之戒", 3000, "当 HP 降为 0 时，可改为降至 1 HP；每次休息前只能触发一次"),
)


ARTIFACT_EXAMPLES: tuple[EquipmentExample, ...] = (
    artifact("星象仪", "操纵 1 个旅行日范围内的昼夜变换，并可更改同范围天气；类似熵系/元素仪式，但不消耗 MP 且不要求学派。"),
    artifact("黑血", "饮下者被杀死后灵魂不会回归灵魂之河，而会保留意识徘徊在生者世界。"),
    artifact("亡者金币", "持有者据传能够指挥幽灵海盗军团，代价是献出自己的灵魂。"),
    artifact("最终之羽", "碾成细粉可复活一名刚死去不久的生物，但会永久摧毁这片羽毛。"),
    artifact("思绪之盔", "佩戴者可感应附近生物表层想法和情绪；消耗动作读取一个指定存活生物的想法。"),
    artifact("浮土之杖", "插入山铜环阵中央时，可将整片土地抬起，使其浮空飞行。"),
    artifact("裂魂剑", "通过正确仪式可从灵魂之河中剥离意识，可能造成极端悲剧性的灵魂后果。"),
    artifact("星门魔典", "满月光下会打开通向宇宙的门扉，并在破晓时关闭。"),
    artifact("天音晶片", "持有者能听到神奇声音，获得权力、财富与成功的智慧；只有持有者能听见。"),
    artifact("荒野魔杖", "可将自愿生物变为小型野兽，也可消耗动作解除一个生物的变形。"),
    artifact("风之鳞", "接触皮肤时，持有者获得无限制自由飞行和浮空能力；长期接触可能改变生理结构。"),
)


RARE_EQUIPMENT_EXAMPLES: tuple[EquipmentExample, ...] = (
    *RARE_WEAPON_EXAMPLES,
    *RARE_ARMOR_EXAMPLES,
    *RARE_SHIELD_EXAMPLES,
    *RARE_ACCESSORY_EXAMPLES,
)
EQUIPMENT_EXAMPLES: tuple[EquipmentExample, ...] = (*BASIC_EQUIPMENT_EXAMPLES, *RARE_EQUIPMENT_EXAMPLES, *ARTIFACT_EXAMPLES)


def _build_index(examples: Iterable[EquipmentExample]) -> dict[str, EquipmentExample]:
    index: dict[str, EquipmentExample] = {}
    for example in examples:
        index[example.name] = example
        for alias in example.aliases:
            index[alias] = example
    return index


EQUIPMENT_EXAMPLES_BY_NAME = _build_index(EQUIPMENT_EXAMPLES)


def get_equipment_example(name: str) -> EquipmentExample | None:
    return EQUIPMENT_EXAMPLES_BY_NAME.get(name.strip())


def search_equipment_examples(
    *,
    item_type: EquipmentItemType | str | None = None,
    category: str = "",
    max_price: int | None = None,
    damage_type: str = "",
    text: str = "",
    include_artifacts: bool = False,
    limit: int = 20,
) -> list[EquipmentExample]:
    normalized_item_type = EquipmentItemType(item_type) if item_type is not None else None
    query = text.strip().lower()
    results: list[EquipmentExample] = []
    for example in EQUIPMENT_EXAMPLES:
        if not include_artifacts and example.item_type == EquipmentItemType.ARTIFACT:
            continue
        if normalized_item_type is not None and example.item_type != normalized_item_type:
            continue
        if category and example.category != category:
            continue
        if max_price is not None and (example.price is None or example.price > max_price):
            continue
        if damage_type and example.damage_type != damage_type:
            continue
        if query:
            haystack = " ".join(
                [example.name, example.category, example.damage_type, *example.effects, *example.tags, *example.aliases]
            ).lower()
            if query not in haystack:
                continue
        results.append(example)
        if len(results) >= limit:
            break
    return results

