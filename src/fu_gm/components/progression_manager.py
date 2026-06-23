from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.components.character_creation_manager import (
    CLASS_ALIASES,
    HP_BONUS_CLASSES,
    IP_BONUS_CLASSES,
    MARTIAL_ARMOR_CLASSES,
    MARTIAL_MELEE_CLASSES,
    MARTIAL_RANGED_CLASSES,
    MARTIAL_SHIELD_CLASSES,
    MP_BONUS_CLASSES,
    SKILL_CATALOG,
)
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Character,
    ExperienceGain,
    LevelUpResult,
    SessionExperienceReport,
    StatusEffect,
)
from fu_gm.skill_library import SKILL_ALIASES, normalize_skill_reference_name


MAX_CHARACTER_LEVEL = 50
MAX_CLASS_LEVEL = 10
XP_PER_LEVEL = 10
ATTRIBUTE_STEP_LEVELS = {20, 40}
ATTRIBUTE_STEPS = [6, 8, 10, 12]


@dataclass(frozen=True)
class HeroSkillDefinition:
    name: str
    required_mastered_classes: set[str] = field(default_factory=set)
    required_any_mastered_class: bool = False
    repeatable: bool = False


HERO_SKILLS: dict[str, HeroSkillDefinition] = {}


def _register_hero_skill(
    name: str,
    *,
    required_mastered_classes: set[str] | None = None,
    required_any_mastered_class: bool = False,
    repeatable: bool = False,
) -> None:
    HERO_SKILLS[name] = HeroSkillDefinition(
        name=name,
        required_mastered_classes=set(required_mastered_classes or set()),
        required_any_mastered_class=required_any_mastered_class,
        repeatable=repeatable,
    )


for _name in ("灵巧双手", "额外HP", "额外MP", "额外IP", "额外咒语"):
    _register_hero_skill(_name)

_register_hero_skill("大口袋", required_mastered_classes={"造物使"})
_register_hero_skill("状态免疫", required_mastered_classes={"旅人"})
_register_hero_skill("强力射击", required_mastered_classes={"神射手"})
_register_hero_skill("强力咒语", required_mastered_classes={"拟兽使", "元素使", "熵术士", "御魂使"})
_register_hero_skill("强力攻击", required_mastered_classes={"怒焰斗士", "武器大师"})
_register_hero_skill("不破之人", required_mastered_classes={"守护者"})
_register_hero_skill("灵猴握", required_mastered_classes={"怒焰斗士"})
_register_hero_skill("堡垒", required_mastered_classes={"守护者"})
_register_hero_skill("数学魔法", required_mastered_classes={"博学家"})
_register_hero_skill("启示", required_mastered_classes={"奥灵使"})
_register_hero_skill("嵌合术精通", required_mastered_classes={"拟兽使"}, repeatable=True)
_register_hero_skill("背水", required_mastered_classes={"暗刃骑士"})
_register_hero_skill("薄情者", required_mastered_classes={"暗刃骑士"})
_register_hero_skill("希望", required_mastered_classes={"御魂使"})
_register_hero_skill("火山", required_mastered_classes={"元素使"})
_register_hero_skill("彗星", required_mastered_classes={"熵术士"})
_register_hero_skill("英雄级同伴", required_mastered_classes={"旅人"})
_register_hero_skill("完美瞄准", required_mastered_classes={"神射手"})
_register_hero_skill("劫掠", required_mastered_classes={"浪客"})
_register_hero_skill("卸甲真言", required_mastered_classes={"游说家"})
_register_hero_skill("重唱", required_mastered_classes={"游说家"})
_register_hero_skill("我算到了", required_mastered_classes={"博学家"})
_register_hero_skill("升级", required_mastered_classes={"造物使"})
_register_hero_skill("风暴击", required_mastered_classes={"武器大师"})
_register_hero_skill("消失", required_mastered_classes={"浪客"})
_register_hero_skill("奥术回响", required_mastered_classes={"奥灵使"})


class ProgressionManager:
    """处理阶段经验、升级、职业等级与英雄技能。"""

    def __init__(self, character_manager: CharacterManager, world_state: WorldState | None = None) -> None:
        self.character_manager = character_manager
        self.world_state = world_state
        self._leveled_this_session: set[str] = set()

    def award_session_experience(
        self,
        *,
        participating_pcs: list[str] | None = None,
        ultima_spent: int = 0,
        fabula_spent: int = 0,
        base_xp: int = 5,
    ) -> SessionExperienceReport:
        self._leveled_this_session.clear()
        pc_names = participating_pcs or [character.name for character in self.character_manager.all() if "pc" in character.traits]
        if not pc_names:
            raise ValueError("没有参与阶段的 PC，无法结算经验。")
        fabula_xp = fabula_spent // len(pc_names)
        total_xp = base_xp + max(0, ultima_spent) + fabula_xp
        gains: list[ExperienceGain] = []
        for name in pc_names:
            character = self.character_manager.get(name)
            before = character.experience_points
            character.experience_points += total_xp
            gains.append(
                ExperienceGain(
                    character_name=name,
                    before=before,
                    after=character.experience_points,
                    amount=total_xp,
                    can_level_up=self.can_level_up(name),
                )
            )
        summary = (
            f"阶段经验：基础 {base_xp} + 终结点 {max(0, ultima_spent)} + 物语点均分 {fabula_xp} = {total_xp} XP。"
        )
        if self.world_state is not None:
            self.world_state.add_memory(summary)
        return SessionExperienceReport(
            participating_pcs=list(pc_names),
            base_xp=base_xp,
            ultima_spent=max(0, ultima_spent),
            fabula_spent=max(0, fabula_spent),
            fabula_xp=fabula_xp,
            total_xp=total_xp,
            gains=gains,
            summary=summary,
        )

    def can_level_up(self, character_name: str) -> bool:
        character = self.character_manager.get(character_name)
        return (
            character.level < MAX_CHARACTER_LEVEL
            and character.experience_points >= XP_PER_LEVEL
            and character_name not in self._leveled_this_session
        )

    def level_up(
        self,
        character_name: str,
        *,
        class_name: str,
        skill_name: str,
        attribute_increase: str = "",
        hero_skill: str = "",
        status_immunity: StatusEffect | str | None = None,
        extra_spells: list[str] | None = None,
        new_identity: str = "",
        new_theme: str = "",
    ) -> LevelUpResult:
        character = self.character_manager.get(character_name)
        if not self.can_level_up(character_name):
            raise ValueError(f"{character_name} 当前 XP 不足，不能升级。")
        if character.level >= MAX_CHARACTER_LEVEL:
            raise ValueError(f"{character_name} 已达到等级上限。")

        normalized_class = self.normalize_class_name(class_name)
        normalized_skill = self.normalize_skill_name(skill_name)
        self._validate_class_choice(character, normalized_class)
        self._validate_skill_choice(character, normalized_class, normalized_skill)
        next_level = character.level + 1
        class_level_before = character.classes.get(normalized_class, 0)
        will_master_class = normalized_class if class_level_before + 1 == MAX_CLASS_LEVEL else ""
        if next_level in ATTRIBUTE_STEP_LEVELS:
            self._validate_attribute_increase(character, attribute_increase)
        elif attribute_increase:
            raise ValueError("只有升到 20 或 40 级时才能提升属性骰。")
        if will_master_class:
            if not hero_skill:
                raise ValueError(f"{character.name} 精通 {normalized_class}，必须选择一个英雄技能。")
            self._validate_hero_skill_choice(
                character,
                self.normalize_hero_skill_name(hero_skill),
                mastered_class=will_master_class,
                status_immunity=status_immunity,
            )
        elif hero_skill:
            raise ValueError("只有职业达到 10 级并精通时才能获得英雄技能。")

        level_before = character.level
        xp_before = character.experience_points
        hp_before = character.max_hp
        mp_before = character.max_mp
        ip_before = character.max_inventory_points

        character.experience_points -= XP_PER_LEVEL
        character.level += 1
        character.max_hp += 1
        character.max_mp += 1
        character.crisis_threshold = character.max_hp // 2
        if new_identity:
            character.identity = new_identity
        if new_theme:
            character.theme = new_theme

        notes = [f"等级 {level_before} -> {character.level}，消耗 10 XP。"]
        if character.level in ATTRIBUTE_STEP_LEVELS:
            attribute = attribute_increase.upper()
            self._increase_attribute(character, attribute)
            notes.append(f"{attribute} 属性骰提升到 d{character.attributes[attribute]}。")

        is_new_class = class_level_before == 0
        character.classes[normalized_class] = class_level_before + 1
        if is_new_class:
            notes.extend(self._apply_new_class_benefits(character, normalized_class))

        character.skills[normalized_skill] = character.skills.get(normalized_skill, 0) + 1
        notes.append(f"{normalized_class} 提升到 {character.classes[normalized_class]} 级，获得技能【{normalized_skill}】。")

        mastered_class = ""
        if class_level_before + 1 == MAX_CLASS_LEVEL:
            mastered_class = normalized_class
            normalized_hero_skill = self.normalize_hero_skill_name(hero_skill)
            self._apply_hero_skill(
                character,
                normalized_hero_skill,
                status_immunity=status_immunity,
                extra_spells=extra_spells,
            )
            notes.append(f"精通 {normalized_class}，获得英雄技能【{normalized_hero_skill}】。")
            hero_skill = normalized_hero_skill

        character.crisis_threshold = character.max_hp // 2
        result = LevelUpResult(
            character_name=character.name,
            level_before=level_before,
            level_after=character.level,
            xp_before=xp_before,
            xp_after=character.experience_points,
            class_name=normalized_class,
            class_level_before=class_level_before,
            class_level_after=character.classes[normalized_class],
            skill_name=normalized_skill,
            skill_rank_after=character.skills[normalized_skill],
            attribute_increase=attribute_increase.upper(),
            hero_skill=hero_skill,
            mastered_class=mastered_class,
            max_hp_before=hp_before,
            max_hp_after=character.max_hp,
            max_mp_before=mp_before,
            max_mp_after=character.max_mp,
            max_ip_before=ip_before,
            max_ip_after=character.max_inventory_points,
            notes=notes,
        )
        if self.world_state is not None:
            self.world_state.add_memory(f"升级：{character.name} 升至 {character.level} 级。{'；'.join(notes)}")
        self._leveled_this_session.add(character.name)
        return result

    def normalize_class_name(self, raw_name: str) -> str:
        key = raw_name.strip().lower()
        canonical = CLASS_ALIASES.get(key) or CLASS_ALIASES.get(raw_name.strip())
        if canonical is None:
            raise ValueError(f"未知职业：{raw_name}")
        return canonical

    def normalize_skill_name(self, raw_name: str) -> str:
        return normalize_skill_reference_name(raw_name)

    def normalize_hero_skill_name(self, raw_name: str) -> str:
        name = SKILL_ALIASES.get(raw_name.strip(), raw_name.strip())
        if name not in HERO_SKILLS:
            raise ValueError(f"未知英雄技能：{raw_name}")
        return name

    def _validate_class_choice(self, character: Character, class_name: str) -> None:
        current_level = character.classes.get(class_name, 0)
        if current_level >= MAX_CLASS_LEVEL:
            raise ValueError(f"{class_name} 已经达到 10 级上限。")
        if current_level == 0:
            non_mastered = sum(1 for level in character.classes.values() if level < MAX_CLASS_LEVEL)
            if non_mastered + 1 > 3:
                raise ValueError("角色不能拥有超过 3 个尚未精通的职业。")

    def _validate_skill_choice(self, character: Character, class_name: str, skill_name: str) -> None:
        definition = SKILL_CATALOG.get(skill_name)
        if definition is None:
            raise ValueError(f"未知技能：{skill_name}")
        if definition.class_name != class_name:
            raise ValueError(f"技能【{skill_name}】属于{definition.class_name}，不能用于提升{class_name}。")
        if character.skills.get(skill_name, 0) >= definition.max_ranks:
            raise ValueError(f"技能【{skill_name}】最多只能获取 {definition.max_ranks} 次。")

    def _increase_attribute(self, character: Character, attribute: str) -> None:
        self._validate_attribute_increase(character, attribute)
        attribute = attribute.upper()
        before = character.attributes[attribute]
        index = ATTRIBUTE_STEPS.index(before)
        after = ATTRIBUTE_STEPS[index + 1]
        character.attributes[attribute] = after
        if attribute == "MIG":
            character.max_hp += (after - before) * 5
        if attribute == "WLP":
            character.max_mp += (after - before) * 5

    def _validate_attribute_increase(self, character: Character, attribute: str) -> None:
        if not attribute:
            raise ValueError(f"{character.name} 升到关键等级时必须选择一个属性骰提升。")
        attribute = attribute.upper()
        if attribute not in character.attributes:
            raise ValueError(f"未知属性：{attribute}")
        before = character.attributes[attribute]
        if before >= 12:
            raise ValueError(f"{attribute} 已经是 d12，不能继续提升。")

    def _apply_new_class_benefits(self, character: Character, class_name: str) -> list[str]:
        notes: list[str] = []
        if class_name in HP_BONUS_CLASSES:
            character.max_hp += 5
            notes.append(f"新职业免费增益：最大 HP +5。")
        if class_name in MP_BONUS_CLASSES:
            character.max_mp += 5
            notes.append(f"新职业免费增益：最大 MP +5。")
        if class_name in IP_BONUS_CLASSES:
            character.max_inventory_points += 2
            character.inventory_points += 2
            notes.append(f"新职业免费增益：库存点上限 +2。")
        for ability in self._class_abilities(class_name):
            if ability not in character.abilities:
                character.abilities.append(ability)
                notes.append(f"获得能力：{ability}。")
        return notes

    def _class_abilities(self, class_name: str) -> list[str]:
        abilities: list[str] = []
        if class_name in MARTIAL_MELEE_CLASSES:
            abilities.append("可装备职业近战武器")
        if class_name in MARTIAL_RANGED_CLASSES:
            abilities.append("可装备职业远程武器")
        if class_name in MARTIAL_ARMOR_CLASSES:
            abilities.append("可装备职业盔甲")
        if class_name in MARTIAL_SHIELD_CLASSES:
            abilities.append("可装备职业盾牌")
        if class_name == "造物使":
            abilities.append("可发起项目")
        return abilities

    def _apply_hero_skill(
        self,
        character: Character,
        hero_skill: str,
        *,
        status_immunity: StatusEffect | str | None = None,
        extra_spells: list[str] | None = None,
    ) -> None:
        self._validate_hero_skill_choice(character, hero_skill, status_immunity=status_immunity)
        if hero_skill == "状态免疫":
            status = status_immunity if isinstance(status_immunity, StatusEffect) else StatusEffect(status_immunity)
        else:
            status = None
        character.hero_skills.append(hero_skill)
        if hero_skill == "额外HP":
            character.max_hp += 20 if character.level >= 40 else 10
        elif hero_skill == "额外MP":
            character.max_mp += 20 if character.level >= 40 else 10
        elif hero_skill == "额外IP":
            character.max_inventory_points += 4
            character.inventory_points += 4
        elif hero_skill == "状态免疫" and status is not None:
            character.permanent_status_immunities.add(status)
            if status in character.statuses:
                character.statuses.remove(status)
        elif hero_skill == "额外咒语":
            for spell in extra_spells or []:
                if spell not in character.spells:
                    character.spells.append(spell)
        elif hero_skill in {"灵巧双手", "大口袋", "灵猴握", "堡垒"}:
            if hero_skill not in character.abilities:
                character.abilities.append(hero_skill)

    def _validate_hero_skill_choice(
        self,
        character: Character,
        hero_skill: str,
        *,
        mastered_class: str = "",
        status_immunity: StatusEffect | str | None = None,
    ) -> None:
        definition = HERO_SKILLS[hero_skill]
        if hero_skill in character.hero_skills and not definition.repeatable:
            raise ValueError(f"英雄技能【{hero_skill}】不能重复获得。")
        if definition.required_mastered_classes:
            mastered_classes = {class_name for class_name, level in character.classes.items() if level >= MAX_CLASS_LEVEL}
            if mastered_class:
                mastered_classes.add(mastered_class)
            has_requirement = bool(mastered_classes & definition.required_mastered_classes)
            if not has_requirement:
                requirements = "、".join(sorted(definition.required_mastered_classes))
                raise ValueError(f"英雄技能【{hero_skill}】需要精通以下职业之一：{requirements}。")
        if definition.required_any_mastered_class:
            has_any_mastery = any(level >= MAX_CLASS_LEVEL for level in character.classes.values()) or bool(mastered_class)
            if not has_any_mastery:
                raise ValueError(f"英雄技能【{hero_skill}】需要至少精通一个职业。")
        if hero_skill == "状态免疫":
            if status_immunity is None:
                raise ValueError("选择【状态免疫】时必须指定一个异常状态。")
            if not isinstance(status_immunity, StatusEffect):
                StatusEffect(status_immunity)

