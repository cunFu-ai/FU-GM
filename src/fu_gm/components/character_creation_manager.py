from __future__ import annotations

import ast
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from copy import deepcopy

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.portable_device_rules import validate_portable_device_choices
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Bond,
    CampaignCreationBundle,
    Character,
    CharacterCreationResult,
    HeroDraft,
    HeroDraftValidationResult,
    HeroCreationProfile,
    PartyMemberEntry,
    PartySheet,
    WorldSheet,
)
from fu_gm.skill_library import (
    CLASS_SKILL_REFERENCES,
    SKILL_ALIASES,
    normalize_skill_map,
    normalize_skill_reference_name,
    required_spell_slots,
    skill_choice_requirements,
    skill_rank,
)
from fu_gm.spellbook import normalize_spell_name, spell_names_for_school, spell_school_for


VALID_ATTRIBUTE_DICE = {6, 8, 10, 12}
REQUIRED_ATTRIBUTES = ("DEX", "INS", "MIG", "WLP")
ATTRIBUTE_ALIASES = {
    "DEX": "DEX",
    "敏捷": "DEX",
    "AGI": "DEX",
    "INS": "INS",
    "洞察": "INS",
    "MIG": "MIG",
    "力量": "MIG",
    "STR": "MIG",
    "WLP": "WLP",
    "意志": "WLP",
    "WIL": "WLP",
}
STARTING_ATTRIBUTE_TOTAL = 32
RECOMMENDED_STARTING_ATTRIBUTE_PATTERNS = (
    ("多面手", (8, 8, 8, 8)),
    ("均衡", (10, 8, 8, 6)),
    ("专精", (10, 10, 6, 6)),
)
STARTING_EQUIPMENT_BUDGET = 500

CLASS_ALIASES = {
    "arcanist": "奥灵使",
    "奥术师": "奥灵使",
    "奥灵使": "奥灵使",
    "chimerist": "拟兽使",
    "嵌合师": "拟兽使",
    "拟兽使": "拟兽使",
    "darkblade": "暗刃骑士",
    "dark blade": "暗刃骑士",
    "暗黑之刃": "暗刃骑士",
    "暗刃骑士": "暗刃骑士",
    "elementalist": "元素使",
    "元素师": "元素使",
    "元素使": "元素使",
    "entropist": "熵术士",
    "熵师": "熵术士",
    "熵术士": "熵术士",
    "fury": "怒焰斗士",
    "狂怒斗士": "怒焰斗士",
    "怒焰斗士": "怒焰斗士",
    "guardian": "守护者",
    "守护者": "守护者",
    "loremaster": "博学家",
    "博学士": "博学家",
    "博学家": "博学家",
    "orator": "游说家",
    "吟唱者": "游说家",
    "游说家": "游说家",
    "rogue": "浪客",
    "浪客": "浪客",
    "sharpshooter": "神射手",
    "神射手": "神射手",
    "spiritist": "御魂使",
    "灵师": "御魂使",
    "御魂使": "御魂使",
    "tinkerer": "造物使",
    "修补匠": "造物使",
    "造物使": "造物使",
    "wayfarer": "旅人",
    "旅人": "旅人",
    "weaponmaster": "武器大师",
    "weapon master": "武器大师",
    "武器大师": "武器大师",
}

HP_BONUS_CLASSES = {"暗刃骑士", "怒焰斗士", "守护者", "神射手", "武器大师"}
MP_BONUS_CLASSES = {"奥灵使", "拟兽使", "元素使", "熵术士", "博学家", "游说家", "御魂使"}
IP_BONUS_CLASSES = {"浪客", "造物使", "旅人"}
MARTIAL_MELEE_CLASSES = {"暗刃骑士", "怒焰斗士", "武器大师"}
MARTIAL_ARMOR_CLASSES = {"暗刃骑士", "怒焰斗士", "守护者"}
MARTIAL_SHIELD_CLASSES = {"守护者", "神射手", "武器大师"}
MARTIAL_RANGED_CLASSES = {"神射手"}


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    class_name: str
    max_ranks: int = 1


@dataclass(frozen=True)
class ArmorDefinition:
    name: str
    price: int
    physical_base: str | int
    physical_bonus: int
    magic_base: str | int
    magic_bonus: int
    initiative_modifier: int
    required_ability: str = ""


@dataclass(frozen=True)
class ShieldDefinition:
    name: str
    price: int
    physical_bonus: int
    magic_bonus: int = 0
    required_ability: str = ""


@dataclass(frozen=True)
class WeaponDefinition:
    name: str
    price: int
    accuracy_attributes: tuple[str, str]
    accuracy_modifier: int
    damage_bonus: int
    hands: int
    range_type: str
    category: str
    required_ability: str = ""


@dataclass
class EquipmentRequest:
    display_name: str
    template_name: str


@dataclass
class EquipmentPlan:
    names: list[str]
    cost: int
    armor: ArmorDefinition
    armor_display: str
    shield: ShieldDefinition | None
    shield_display: str
    weapons: list[WeaponDefinition]
    weapon_displays: list[str]
    defenses: dict[str, int]
    initiative_modifier: int
    main_hand: str
    off_hand: str
    weapon: WeaponDefinition
    templates: dict[str, str]


SKILL_CATALOG: dict[str, SkillDefinition] = {}


def _register_skill(class_name: str, name: str, max_ranks: int = 1) -> None:
    SKILL_CATALOG[name] = SkillDefinition(name=name, class_name=class_name, max_ranks=max_ranks)


for _skill_reference in CLASS_SKILL_REFERENCES:
    _register_skill(_skill_reference.class_name, _skill_reference.name, _skill_reference.max_ranks)


ARMOR_TABLE = {
    "无防具": ArmorDefinition("无防具", 0, "DEX", 0, "INS", 0, 0),
    "丝质衬衫": ArmorDefinition("丝质衬衫", 100, "DEX", 1, "INS", 2, -1),
    "旅行装束": ArmorDefinition("旅行装束", 100, "DEX", 1, "INS", 1, -1),
    "武道服": ArmorDefinition("武道服", 150, "DEX", 1, "INS", 1, 0),
    "贤者之袍": ArmorDefinition("贤者之袍", 200, "DEX", 1, "INS", 2, -2),
    "板甲衣": ArmorDefinition("板甲衣", 150, 10, 0, "INS", 0, -2, "可装备职业盔甲"),
    "青铜板甲": ArmorDefinition("青铜板甲", 200, 11, 0, "INS", 0, -3, "可装备职业盔甲"),
    "符文板甲": ArmorDefinition("符文板甲", 250, 11, 0, "INS", 1, -3, "可装备职业盔甲"),
    "钢制板甲": ArmorDefinition("钢制板甲", 300, 12, 0, "INS", 0, -4, "可装备职业盔甲"),
}


SHIELD_TABLE = {
    "青铜盾": ShieldDefinition("青铜盾", 100, 2),
    "符文盾": ShieldDefinition("符文盾", 150, 2, 2, "可装备职业盾牌"),
}


WEAPON_TABLE = {
    "徒手攻击": WeaponDefinition("徒手攻击", 0, ("DEX", "MIG"), 0, 0, 1, "melee", "格斗"),
    "法杖": WeaponDefinition("法杖", 100, ("WLP", "WLP"), 0, 6, 2, "melee", "魔法"),
    "魔典": WeaponDefinition("魔典", 100, ("INS", "INS"), 0, 6, 2, "melee", "魔法"),
    "十字弩": WeaponDefinition("十字弩", 150, ("DEX", "INS"), 0, 8, 2, "ranged", "弓"),
    "短弓": WeaponDefinition("短弓", 200, ("DEX", "DEX"), 0, 8, 2, "ranged", "弓"),
    "临时武器(近战)": WeaponDefinition("临时武器(近战)", 0, ("DEX", "MIG"), 0, 2, 1, "melee", "格斗"),
    "铁指虎": WeaponDefinition("铁指虎", 150, ("DEX", "MIG"), 0, 6, 1, "melee", "格斗"),
    "钢匕首": WeaponDefinition("钢匕首", 150, ("DEX", "INS"), 1, 4, 1, "melee", "匕首"),
    "手枪": WeaponDefinition("手枪", 250, ("DEX", "INS"), 0, 8, 1, "ranged", "枪械", "可装备职业远程武器"),
    "链鞭": WeaponDefinition("链鞭", 150, ("DEX", "DEX"), 0, 8, 2, "melee", "链枷"),
    "铁锤": WeaponDefinition("铁锤", 200, ("MIG", "MIG"), 0, 6, 1, "melee", "重型"),
    "阔斧": WeaponDefinition("阔斧", 250, ("MIG", "MIG"), 0, 10, 1, "melee", "重型", "可装备职业近战武器"),
    "战斧": WeaponDefinition("战斧", 250, ("MIG", "MIG"), 0, 14, 2, "melee", "重型", "可装备职业近战武器"),
    "轻矛": WeaponDefinition("轻矛", 200, ("DEX", "MIG"), 0, 8, 1, "melee", "矛", "可装备职业近战武器"),
    "重矛": WeaponDefinition("重矛", 200, ("DEX", "MIG"), 0, 12, 2, "melee", "矛", "可装备职业近战武器"),
    "青铜剑": WeaponDefinition("青铜剑", 200, ("DEX", "MIG"), 1, 6, 1, "melee", "剑", "可装备职业近战武器"),
    "巨剑": WeaponDefinition("巨剑", 200, ("DEX", "MIG"), 1, 10, 2, "melee", "剑", "可装备职业近战武器"),
    "武士刀": WeaponDefinition("武士刀", 200, ("DEX", "INS"), 1, 10, 2, "melee", "剑", "可装备职业近战武器"),
    "细剑": WeaponDefinition("细剑", 200, ("DEX", "INS"), 1, 6, 1, "melee", "剑", "可装备职业近战武器"),
    "临时武器(远程)": WeaponDefinition("临时武器(远程)", 0, ("DEX", "MIG"), 0, 2, 1, "ranged", "投掷"),
    "手里剑": WeaponDefinition("手里剑", 150, ("DEX", "INS"), 0, 4, 1, "ranged", "投掷"),
}


EQUIPMENT_ALIASES = {
    "临时武器（近战）": "临时武器(近战)",
    "临时近战武器": "临时武器(近战)",
    "临时武器近战": "临时武器(近战)",
    "临时武器（远程）": "临时武器(远程)",
    "临时远程武器": "临时武器(远程)",
    "临时武器远程": "临时武器(远程)",
    "十字弓": "十字弩",
    "短弓炮": "短弓",
    "铁拳套": "铁指虎",
    "大刀阔斧": "阔斧",
    "武术服装": "武道服",
    "智者之袍": "贤者之袍",
    "没有防护装备": "无防具",
    "无护甲": "无防具",
}


def normalize_equipment_template_name(raw_name: str) -> str:
    clean = str(raw_name or "").replace("(+)", "").replace("（+）", "").strip()
    clean = re.sub(r"\s+", "", clean)
    return EQUIPMENT_ALIASES.get(clean, clean)


def is_known_equipment_template(name: str) -> bool:
    return name in ARMOR_TABLE or name in SHIELD_TABLE or name in WEAPON_TABLE


def extract_equipment_template_name(text: str) -> str:
    template_text = (
        str(text or "")
        .replace("数值模板", "")
        .replace("模板", "")
        .replace("按", "")
        .replace("以", "")
        .replace("使用", "")
        .replace("作为", "")
        .replace("结算", "")
        .replace("判定", "")
        .replace("：", "")
        .replace(":", "")
        .strip()
    )
    template_name = normalize_equipment_template_name(template_text)
    return template_name if is_known_equipment_template(template_name) else ""


def split_equipment_template_pair(clean: str) -> tuple[str, str] | None:
    bracket_match = re.fullmatch(r"(.+?)[（(](.+?)[）)]", clean)
    if bracket_match:
        display_name = bracket_match.group(1).strip()
        template_text = bracket_match.group(2).strip()
        template_name = extract_equipment_template_name(template_text)
        if display_name and template_name:
            return display_name, template_name

    for separator in ("=>", "->", "=", "＝"):
        if separator in clean:
            display_name, template_text = [part.strip() for part in clean.split(separator, 1)]
            template_name = extract_equipment_template_name(template_text)
            if display_name and template_name:
                return display_name, template_name
    return None


def resolve_equipment_request_text(raw_name: str) -> EquipmentRequest:
    raw_clean = str(raw_name or "").replace("(+)", "").replace("（+）", "").strip()
    if not raw_clean:
        raise ValueError("装备名称不能为空。")
    canonical = normalize_equipment_template_name(raw_clean)
    if is_known_equipment_template(canonical):
        display_name = raw_clean if raw_clean != canonical else canonical
        return EquipmentRequest(display_name=display_name, template_name=canonical)

    pair = split_equipment_template_pair(raw_clean)
    if pair is not None:
        display_name, template_name = pair
        return EquipmentRequest(display_name=display_name, template_name=template_name)

    raise ValueError(f"未知或不可作为初始购买的装备：{raw_name}；自定义外观需要写明数值模板，例如“外观名（手里剑模板）”。")


class CharacterCreationManager:
    """把 Session 0 的创作结果推进到正式 PC、小队表与世界表。"""

    def __init__(
        self,
        character_manager: CharacterManager,
        world_state: WorldState,
        rules_engine: RulesEngine | None = None,
    ) -> None:
        self.character_manager = character_manager
        self.world_state = world_state
        self.rules_engine = rules_engine or RulesEngine()
        self.hero_profiles: dict[str, HeroCreationProfile] = {}

    def validate_hero_draft(self, draft_key: str) -> HeroDraftValidationResult:
        draft = self.get_hero_draft(draft_key)
        profile = self.hero_draft_to_profile(draft_key, draft)
        missing = self.missing_fields_for_draft(draft)
        unresolved_skill_choices = self.unresolved_skill_choice_requirements(
            draft.skills,
            skill_options=draft.skill_options,
            spells=draft.spells,
            bound_arcana=draft.bound_arcana,
            include_optional=False,
        )
        errors: list[str] = []
        warnings: list[str] = []

        try:
            classes = self.normalize_classes(profile.classes)
            self.validate_starting_classes(classes)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            profile.classes = classes

        attributes_valid = False
        try:
            attributes = self.validate_attributes(profile.attributes)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            profile.attributes = attributes
            attributes_valid = True

        skills_valid = False
        try:
            skills = self.validate_skills(profile.classes, profile.skills)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            profile.skills = skills
            skills_valid = True

        if skills_valid:
            try:
                profile.skill_options = self.validate_skill_options(
                    profile.skills,
                    profile.skill_options,
                    require_complete=False,
                )
            except ValueError as exc:
                errors.append(str(exc))
            try:
                profile.spells = self.validate_granted_spells(profile.skills, profile.spells, require_complete=False)
            except ValueError as exc:
                errors.append(str(exc))

        if attributes_valid:
            try:
                benefits = self.class_benefits(profile.classes)
                self.build_equipment_plan(
                    profile.equipment,
                    list(profile.abilities) + benefits["abilities"],
                    profile.attributes,
                    profile.equipment_slots,
                    profile.skills,
                )
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))

        if profile.hero_name and self.character_manager.exists(profile.hero_name):
            errors.append(f"角色【{profile.hero_name}】已经存在，不能重复创建。")
        if not profile.bonds:
            warnings.append("建议至少建立一个起始羁绊；这不是硬性阻止项。")
        if not profile.notes:
            warnings.append("建议留一个私人问题、誓言或未解决的背景钩子，方便第一幕使用。")
        advisory_choices = self.unresolved_skill_choice_requirements(
            profile.skills,
            skill_options=profile.skill_options,
            spells=profile.spells,
            bound_arcana=profile.bound_arcana,
            include_optional=True,
        )
        for requirement in advisory_choices:
            if requirement.get("required_for_creation"):
                continue
            if requirement.get("choice_key") == "initial_arcanum":
                warnings.append("奥灵使可以在创建角色时选择一个起始绑定的奥灵。")
            elif requirement.get("choice_key") == "companion_profile":
                warnings.append("【忠诚伙伴】的伙伴资料需要在首次使用前协作建立。")
        if len(profile.classes) == 4:
            warnings.append("四职业属于本桌 GM 通融特例；总等级 5 与每级 1 个职业技能仍照常结算。")

        ready = not missing and not errors
        return HeroDraftValidationResult(
            draft_key=draft_key,
            ready=ready,
            missing_fields=missing,
            errors=errors,
            warnings=warnings,
            unresolved_skill_choices=unresolved_skill_choices,
            profile=profile,
        )

    def validate_hero_draft_for_session_zero(
        self,
        draft_key: str,
    ) -> HeroDraftValidationResult:
        """Validate a draft that may already have materialized into its PC."""

        result = self.validate_hero_draft(draft_key)
        draft = self.get_hero_draft(draft_key)
        duplicate_error = f"角色【{draft.hero_name}】已经存在，不能重复创建。"
        if draft.confirmed and duplicate_error in result.errors:
            result.errors = [
                item for item in result.errors if item != duplicate_error
            ]
            result.ready = not result.missing_fields and not result.errors
        return result

    def create_player_character_from_draft(
        self,
        draft_key: str,
        *,
        require_confirmed: bool = True,
    ) -> CharacterCreationResult:
        draft = self.get_hero_draft(draft_key)
        validation = self.validate_hero_draft(draft_key)
        if require_confirmed and not draft.confirmed:
            raise ValueError(f"{draft_key} 的角色草稿尚未确认；玩家说“确认创建”后才能正式建卡。")
        if not validation.ready or validation.profile is None:
            details = validation.missing_fields + validation.errors
            raise ValueError("角色草稿尚未满足建卡要求：" + "；".join(details))
        return self.create_player_character(validation.profile)

    def confirm_hero_draft(self, draft_key: str) -> HeroDraftValidationResult:
        draft = self.get_hero_draft(draft_key)
        draft.confirmed = True
        return self.validate_hero_draft(draft_key)

    def get_hero_draft(self, draft_key: str) -> HeroDraft:
        drafts = self.world_state.world_profile.hero_drafts
        if draft_key in drafts:
            return drafts[draft_key]
        for draft in drafts.values():
            if draft.hero_name == draft_key:
                return draft
        raise ValueError(f"找不到角色草稿：{draft_key}")

    def hero_draft_to_profile(self, draft_key: str, draft: HeroDraft) -> HeroCreationProfile:
        return HeroCreationProfile(
            player_name=draft.player_name or draft_key,
            hero_name=draft.hero_name,
            identity=draft.identity,
            theme=draft.theme,
            origin=draft.origin,
            classes=dict(draft.classes),
            attributes=dict(draft.attributes),
            bonds=[self.parse_bond_text(text) for text in draft.bonds if text.strip()],
            skills=dict(draft.skills),
            skill_options={name: list(values) for name, values in draft.skill_options.items()},
            spells=list(draft.spells),
            bound_arcana=list(draft.bound_arcana),
            equipment=list(draft.equipment),
            equipment_slots=dict(draft.equipment_slots),
            notes=list(draft.notes),
        )

    def missing_fields_for_draft(self, draft: HeroDraft) -> list[str]:
        missing: list[str] = []
        if not draft.hero_name:
            missing.append("角色名")
        if not draft.identity:
            missing.append("身份")
        if not draft.theme:
            missing.append("主题")
        if not draft.origin:
            missing.append("故乡")
        if not draft.classes:
            missing.append("职业分配")
        if not draft.attributes:
            missing.append("四项属性骰")
        else:
            draft_attributes = set(self.normalize_attribute_keys(draft.attributes))
            missing_attributes = [attribute for attribute in REQUIRED_ATTRIBUTES if attribute not in draft_attributes]
            if missing_attributes:
                missing.append("属性骰：" + "、".join(missing_attributes))
        if not draft.skills:
            missing.append("职业技能")
        missing.extend(self.missing_skill_option_choices(draft.skills, draft.skill_options))
        missing.extend(self.missing_spell_choices(draft.skills, draft.spells))
        if not draft.equipment:
            missing.append("起始装备")
        return missing

    def unresolved_skill_choice_requirements(
        self,
        skills: dict[str, int],
        *,
        skill_options: dict[str, list[str]] | None = None,
        spells=None,
        bound_arcana: list[str] | None = None,
        include_optional: bool = False,
    ) -> list[dict[str, object]]:
        """Return the still-open choices created by learned skills.

        This is the shared authority used by draft validation, Session Zero
        guidance and rule-reference tools.  Player-facing wording remains the
        language model's job.
        """

        try:
            requirements = skill_choice_requirements(
                normalize_skill_map(
                    {
                        name: int(rank)
                        for name, rank in (skills or {}).items()
                        if int(rank) > 0
                    }
                ),
                include_optional=include_optional,
            )
        except (TypeError, ValueError):
            return []

        normalized_spells = self._normalized_spell_values(spells)
        normalized_options: dict[str, list[str]] = {}
        for raw_name, raw_values in (skill_options or {}).items():
            canonical = normalize_skill_reference_name(str(raw_name))
            if isinstance(raw_values, (list, tuple)):
                normalized_options[canonical] = [
                    str(value).strip()
                    for value in raw_values
                    if str(value).strip()
                ]

        unresolved: list[dict[str, object]] = []
        for requirement in requirements:
            storage_field = str(requirement["storage_field"])
            if storage_field == "skill_options":
                selected_count = len(
                    normalized_options.get(str(requirement["skill_name"]), [])
                )
            elif storage_field == "spells":
                source = str(requirement.get("option_source") or "")
                selected_count = sum(
                    1
                    for spell in normalized_spells
                    if spell_school_for(spell) == source
                )
            elif storage_field == "bound_arcana":
                selected_count = len(
                    [value for value in (bound_arcana or []) if str(value).strip()]
                )
            else:
                selected_count = 0

            required_count = int(requirement.get("required_count") or 0)
            missing_count = max(0, required_count - selected_count)
            if missing_count <= 0:
                continue
            row = dict(requirement)
            row["selected_count"] = selected_count
            row["missing_count"] = missing_count
            if storage_field == "spells" and requirement.get("option_source"):
                row["allowed_values"] = list(
                    spell_names_for_school(str(requirement["option_source"]))
                )
            unresolved.append(row)
        return unresolved

    def missing_skill_option_choices(
        self,
        skills: dict[str, int],
        skill_options: dict[str, list[str]],
    ) -> list[str]:
        rows = self.unresolved_skill_choice_requirements(
            skills,
            skill_options=skill_options,
            include_optional=False,
        )
        return [
            self._missing_choice_label(row)
            for row in rows
            if row.get("storage_field") == "skill_options"
        ]

    def missing_spell_choices(self, skills: dict[str, int], spells) -> list[str]:
        rows = self.unresolved_skill_choice_requirements(
            skills,
            spells=spells,
            include_optional=False,
        )
        return [
            self._missing_choice_label(row)
            for row in rows
            if row.get("storage_field") == "spells"
        ]

    @staticmethod
    def _missing_choice_label(requirement: dict[str, object]) -> str:
        skill_name = str(requirement.get("skill_name") or "技能")
        choice_key = str(requirement.get("choice_key") or "")
        choice_label = str(requirement.get("choice_label") or "附带选择")
        option_source = str(requirement.get("option_source") or "")
        missing_count = int(requirement.get("missing_count") or 1)
        if choice_key == "portable_device":
            return f"{skill_name}（还需 {missing_count} 次装置选择）"
        if requirement.get("storage_field") == "spells":
            return f"{option_source or choice_label}（还需 {missing_count} 个）"
        if choice_key == "casting_attributes":
            return f"{skill_name}（需选择洞察+意志或力量+意志）"
        return f"{skill_name}（还需 {missing_count} 项{choice_label}）"

    def parse_bond_text(self, text: str) -> Bond:
        clean = str(text or "").strip()
        if clean.startswith("{") and clean.endswith("}"):
            try:
                legacy = ast.literal_eval(clean)
            except (SyntaxError, ValueError):
                legacy = None
            if isinstance(legacy, dict):
                target = str(legacy.get("target") or "").strip()
                raw_emotions = legacy.get("emotions") or []
                if isinstance(raw_emotions, str):
                    raw_emotions = [raw_emotions]
                emotions = self.extract_bond_emotions(
                    "、".join(str(item or "") for item in raw_emotions)
                )
                if target:
                    return Bond(target=target, emotions=emotions)
        target = clean
        emotions_text = ""
        for separator in ("：", ":", " - ", "，", ","):
            if separator in clean:
                target, emotions_text = clean.split(separator, 1)
                break
        emotions = self.extract_bond_emotions(emotions_text or clean)
        if not emotions_text:
            for emotion in emotions:
                target = target.replace(emotion, "")
            target = target.strip("：:，,、 　")
        return Bond(target=target.strip() or clean, emotions=emotions)

    def reconcile_legacy_bonds(self) -> list[str]:
        """Repair old confirmed drafts that stored bond objects as strings."""

        repaired: list[str] = []
        drafts = self.world_state.world_profile.hero_drafts
        for character in self.character_manager.all():
            if "pc" not in character.traits or not any(
                str(bond.target or "").lstrip().startswith("{")
                for bond in character.bonds
            ):
                continue
            draft = next(
                (
                    candidate
                    for candidate in drafts.values()
                    if candidate.hero_name == character.name
                ),
                None,
            )
            if draft is None or not draft.bonds:
                continue
            parsed = [
                self.parse_bond_text(text)
                for text in draft.bonds
                if str(text or "").strip()
            ]
            if not parsed or any(
                not bond.target or bond.target.lstrip().startswith("{")
                for bond in parsed
            ):
                continue
            character.bonds = parsed
            repaired.append(f"{character.name} 的旧格式羁绊已规范化。")
        return repaired

    def extract_bond_emotions(self, text: str) -> list[str]:
        emotions: list[str] = []
        if any(token in text for token in ["赞赏", "钦佩"]):
            emotions.append("钦佩")
        elif "自卑" in text:
            emotions.append("自卑")

        if any(token in text for token in ["不信任", "猜忌"]):
            emotions.append("猜忌")
        elif any(token in text for token in ["忠诚", "信赖", "信任"]):
            emotions.append("信赖")

        if any(token in text for token in ["憎恨", "仇恨", "恨"]):
            emotions.append("憎恨")
        elif any(token in text for token in ["喜爱", "爱"]):
            emotions.append("喜爱")
        return emotions[:3]

    def create_player_character(
        self,
        profile: HeroCreationProfile,
        *,
        fate_roll: tuple[int, int] | list[int] | None = None,
        register: bool = True,
    ) -> CharacterCreationResult:
        classes = self.normalize_classes(profile.classes)
        self.validate_starting_classes(classes)
        skills = self.validate_skills(classes, profile.skills)
        skill_options = self.validate_skill_options(
            skills,
            profile.skill_options,
            require_complete=True,
        )
        spells = self.validate_granted_spells(skills, profile.spells)
        attributes = self.validate_attributes(profile.attributes)
        benefits = self.class_benefits(classes)
        abilities = list(profile.abilities) + benefits["abilities"]
        equipment_plan = self.build_equipment_plan(
            profile.equipment,
            abilities,
            attributes,
            profile.equipment_slots,
            skills,
        )
        if fate_roll is None:
            resolved_fate_roll = (
                self.rules_engine.roll_die(6),
                self.rules_engine.roll_die(6),
            )
        else:
            if len(fate_roll) != 2:
                raise ValueError("起始命运骰必须正好包含两枚 d6 的结果。")
            resolved_fate_roll = tuple(int(value) for value in fate_roll)
            if any(value < 1 or value > 6 for value in resolved_fate_roll):
                raise ValueError("起始命运骰的每个结果都必须位于 1 到 6 之间。")
        starting_zenit = (
            STARTING_EQUIPMENT_BUDGET
            - equipment_plan.cost
            + sum(resolved_fate_roll) * 10
        )

        level = 5
        permanent_skill_ranks = {
            "铁壁": skills.get("铁壁", 0),
            "集中心智": skills.get("集中心智", 0),
        }
        max_hp = (
            level
            + attributes["MIG"] * 5
            + benefits["hp"]
            + permanent_skill_ranks["铁壁"] * 3
        )
        max_mp = (
            level
            + attributes["WLP"] * 5
            + benefits["mp"]
            + permanent_skill_ranks["集中心智"] * 3
        )
        inventory_points = 6 + benefits["ip"]
        character = Character(
            name=profile.hero_name,
            attributes=attributes,
            max_hp=max_hp,
            hp=max_hp,
            max_mp=max_mp,
            mp=max_mp,
            level=level,
            crisis_threshold=max_hp // 2,
            inventory_points=inventory_points,
            max_inventory_points=inventory_points,
            fabula_points=3,
            zenit=starting_zenit,
            identity=profile.identity,
            theme=profile.theme,
            origin=profile.origin,
            card_id=str(uuid.uuid4()),
            player_name=profile.player_name,
            creation_fate_roll=list(resolved_fate_roll),
            creation_equipment_cost=equipment_plan.cost,
            notes=list(profile.notes),
            bonds=list(profile.bonds),
            defenses=equipment_plan.defenses,
            traits=["pc"],
            abilities=abilities,
            spells=spells,
            bound_arcana=list(profile.bound_arcana),
            classes=classes,
            skills=skills,
            permanent_skill_ranks_applied={
                name: rank
                for name, rank in permanent_skill_ranks.items()
                if rank > 0
            },
            skill_options=skill_options,
            equipment=equipment_plan.names,
            equipment_templates=equipment_plan.templates,
            equipped_armor=equipment_plan.armor_display,
            equipped_shield=equipment_plan.shield_display if equipment_plan.shield else "",
            equipped_main_hand=equipment_plan.main_hand,
            equipped_off_hand=equipment_plan.off_hand,
            weapon_damage=equipment_plan.weapon.damage_bonus,
            weapon_type="physical",
            weapon_accuracy_attributes=list(equipment_plan.weapon.accuracy_attributes),
            weapon_accuracy_modifier=equipment_plan.weapon.accuracy_modifier,
            weapon_range=equipment_plan.weapon.range_type,
            initiative=equipment_plan.initiative_modifier,
        )
        if register:
            self.character_manager.add(character)
        stored_profile = deepcopy(profile)
        stored_profile.classes = classes
        stored_profile.attributes = attributes
        stored_profile.skills = skills
        stored_profile.skill_options = skill_options
        stored_profile.spells = spells
        stored_profile.equipment = equipment_plan.names
        stored_profile.equipment_slots = {
            "main_hand": equipment_plan.main_hand,
            "off_hand": (
                ""
                if equipment_plan.off_hand in {"", "双手占用", equipment_plan.shield_display}
                else equipment_plan.off_hand
            ),
            "armor": equipment_plan.armor_display,
            "shield": equipment_plan.shield_display,
        }
        if any(display != template for display, template in equipment_plan.templates.items()):
            stored_profile.notes = list(stored_profile.notes) + [
                "装备外观与数值模板："
                + "；".join(
                    f"{display} 按【{template}】结算"
                    for display, template in equipment_plan.templates.items()
                    if display != template
                )
            ]
        if register:
            self.hero_profiles[profile.hero_name] = stored_profile
            self.world_state.add_memory(
                f"角色创建：{profile.hero_name}，{profile.identity}，主题【{profile.theme}】，故乡【{profile.origin}】。"
            )
        return CharacterCreationResult(
            character=character,
            applied_benefits=benefits["descriptions"],
            warnings=self.creation_warnings(profile),
            next_questions=self.next_questions_for(profile),
            equipment_cost=equipment_plan.cost,
            fate_roll=resolved_fate_roll,
            starting_zenit=starting_zenit,
        )

    def normalize_classes(self, classes: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_name, level in classes.items():
            key = raw_name.strip().lower()
            canonical = CLASS_ALIASES.get(key) or CLASS_ALIASES.get(raw_name.strip())
            if canonical is None:
                raise ValueError(f"未知职业：{raw_name}")
            normalized[canonical] = normalized.get(canonical, 0) + int(level)
        return normalized

    def validate_starting_classes(self, classes: dict[str, int]) -> None:
        if len(classes) < 2 or len(classes) > 3:
            raise ValueError("起始角色必须选择 2 到 3 个职业。")
        if sum(classes.values()) != 5:
            raise ValueError("起始角色总职业等级必须等于 5。")
        for class_name, level in classes.items():
            if level < 1:
                raise ValueError(f"{class_name} 的职业等级必须至少为 1。")
            if level > 5:
                raise ValueError(f"{class_name} 的起始职业等级不能高于 5。")

    def validate_attributes(self, attributes: dict[str, int]) -> dict[str, int]:
        normalized = self.normalize_attribute_keys(attributes)
        missing = [attribute for attribute in REQUIRED_ATTRIBUTES if attribute not in normalized]
        if missing:
            raise ValueError(f"缺少属性骰：{', '.join(missing)}")
        for attribute in REQUIRED_ATTRIBUTES:
            if normalized[attribute] not in VALID_ATTRIBUTE_DICE:
                raise ValueError(f"{attribute} 必须是 d6、d8、d10 或 d12。")
        chosen_pattern = tuple(sorted(normalized[attribute] for attribute in REQUIRED_ATTRIBUTES))
        allowed_patterns = {tuple(sorted(values)) for _, values in RECOMMENDED_STARTING_ATTRIBUTE_PATTERNS}
        if chosen_pattern not in allowed_patterns:
            recommended = "、".join(
                f"{name} d{values[0]}/d{values[1]}/d{values[2]}/d{values[3]}"
                for name, values in RECOMMENDED_STARTING_ATTRIBUTE_PATTERNS
            )
            raise ValueError(f"起始属性必须采用规则书组合：{recommended}。")
        return {attribute: normalized[attribute] for attribute in REQUIRED_ATTRIBUTES}

    def normalize_attribute_keys(self, attributes: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_key, raw_value in (attributes or {}).items():
            key_text = str(raw_key).strip()
            key = ATTRIBUTE_ALIASES.get(key_text) or ATTRIBUTE_ALIASES.get(key_text.upper())
            if key is None:
                key = key_text.upper()
            normalized[key] = self._attribute_die_value(raw_value)
        return normalized

    def _attribute_die_value(self, raw_value) -> int:
        text = str(raw_value).strip().lower()
        if text.startswith("d"):
            text = text[1:]
        return int(text)

    def validate_skills(self, classes: dict[str, int], skills: dict[str, int]) -> dict[str, int]:
        normalized = self.validate_partial_skills(classes, skills)
        ranks_by_class = {class_name: 0 for class_name in classes}
        for skill_name, rank in normalized.items():
            definition = SKILL_CATALOG[skill_name]
            ranks_by_class[definition.class_name] += rank
        for class_name, class_level in classes.items():
            if ranks_by_class[class_name] != class_level:
                raise ValueError(f"{class_name} {class_level} 级必须选择 {class_level} 个对应职业技能。")
        return normalized

    def validate_partial_skills(
        self,
        classes: dict[str, int],
        skills: dict[str, int],
    ) -> dict[str, int]:
        """Validate skill identity and rank without requiring all five picks."""

        normalized = normalize_skill_map({name: int(rank) for name, rank in skills.items() if int(rank) > 0})
        ranks_by_class = {class_name: 0 for class_name in classes}
        for skill_name, rank in normalized.items():
            definition = SKILL_CATALOG.get(skill_name)
            if definition is None:
                raise ValueError(f"未知技能：{skill_name}")
            if definition.class_name not in classes:
                raise ValueError(f"技能【{skill_name}】属于{definition.class_name}，但该角色没有这个职业。")
            if rank > definition.max_ranks:
                raise ValueError(f"技能【{skill_name}】最多只能获取 {definition.max_ranks} 次。")
            ranks_by_class[definition.class_name] += rank
        for class_name, class_level in classes.items():
            if ranks_by_class[class_name] > class_level:
                raise ValueError(
                    f"{class_name}只有 {class_level} 级，不能选择 "
                    f"{ranks_by_class[class_name]} 个对应职业技能等级。"
                )
        return normalized

    def validate_skill_options(
        self,
        skills: dict[str, int],
        skill_options: dict[str, list[str]] | None,
        *,
        require_complete: bool,
    ) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for raw_skill_name, raw_choices in (skill_options or {}).items():
            skill_name = normalize_skill_reference_name(str(raw_skill_name))
            if not isinstance(raw_choices, (list, tuple)):
                raise ValueError(f"技能【{skill_name}】的附带选择必须是列表。")
            choices = [str(choice).strip() for choice in raw_choices if str(choice).strip()]
            if choices:
                normalized[skill_name] = choices

        option_requirements = [
            row
            for row in skill_choice_requirements(
                skills or {},
                include_optional=False,
            )
            if row.get("storage_field") == "skill_options"
        ]
        portable_requirement = next(
            (
                row
                for row in option_requirements
                if row.get("choice_key") == "portable_device"
            ),
            None,
        )
        portable_rank = int(
            (portable_requirement or {}).get("required_count") or 0
        )
        portable_skill_name = str(
            (portable_requirement or {}).get("skill_name") or "便携装置"
        )
        portable_choices = validate_portable_device_choices(
            portable_rank,
            normalized.get(portable_skill_name, []),
            require_complete=require_complete,
        )
        if portable_choices:
            normalized[portable_skill_name] = portable_choices
        elif portable_skill_name in normalized:
            normalized.pop(portable_skill_name, None)
        active_option_skills = {
            str(requirement["skill_name"])
            for requirement in option_requirements
        }
        catalog_option_skills = {
            reference.name
            for reference in CLASS_SKILL_REFERENCES
            for choice in reference.choice_specs
            if choice.storage_field == "skill_options"
        }
        for skill_name in catalog_option_skills - active_option_skills:
            if skill_name != portable_skill_name:
                normalized.pop(skill_name, None)
        canonical_chimerist_attributes = {
            "INS+WLP": "洞察+意志",
            "MIG+WLP": "力量+意志",
        }
        for requirement in option_requirements:
            if requirement.get("choice_key") != "casting_attributes":
                continue
            skill_name = str(requirement["skill_name"])
            rank = int(requirement.get("skill_rank") or 0)
            allowed_values = [
                str(value)
                for value in list(requirement.get("allowed_values") or [])
            ]
            canonical_attributes = {
                **{value: value for value in allowed_values},
                **canonical_chimerist_attributes,
            }
            choices = normalized.get(skill_name, [])
            if rank <= 0:
                normalized.pop(skill_name, None)
                continue
            if require_complete and len(choices) != 1:
                raise ValueError(
                    f"技能【{skill_name}】习得时必须选择一次"
                    "【洞察+意志】或【力量+意志】。"
                )
            if len(choices) > 1:
                raise ValueError(
                    f"技能【{skill_name}】只能记录一个固定属性组合。"
                )
            if choices:
                compact = choices[0].replace("【", "").replace("】", "").replace(" ", "")
                if compact not in canonical_attributes:
                    raise ValueError(
                        f"技能【{skill_name}】只能选择【洞察+意志】或【力量+意志】。"
                    )
                normalized[skill_name] = [
                    canonical_attributes[compact]
                ]
        return normalized

    def validate_granted_spells(
        self,
        skills: dict[str, int],
        spells,
        *,
        require_complete: bool = True,
    ) -> list[str]:
        requirements = required_spell_slots(skills or {})
        normalized_spells = self._normalized_spell_values(spells)
        errors: list[str] = []
        allowed_schools = set(requirements)

        for raw, canonical in zip(self._spell_values(spells), normalized_spells):
            school = spell_school_for(canonical)
            if not school:
                errors.append(f"未知或未接入标准法术：{raw}")
                continue
            if allowed_schools and school not in allowed_schools:
                allowed_text = "、".join(allowed_schools)
                errors.append(f"法术【{canonical}】属于{school}，不能满足当前授法技能需要的{allowed_text}。")

        for school, required_count in requirements.items():
            known_count = sum(1 for spell in normalized_spells if spell_school_for(spell) == school)
            missing_count = max(0, required_count - known_count)
            if missing_count and require_complete:
                options = "、".join(spell_names_for_school(school))
                errors.append(f"{school}需要选择 {required_count} 个标准法术，目前还缺 {missing_count} 个；可选：{options}。")

        if errors:
            raise ValueError("；".join(errors))
        return normalized_spells

    def _spell_values(self, spells) -> list[str]:
        if isinstance(spells, str):
            values = [spells]
        elif isinstance(spells, list):
            values = spells
        else:
            values = []
        return [str(spell).strip() for spell in values if str(spell).strip()]

    def _normalized_spell_values(self, spells) -> list[str]:
        normalized: list[str] = []
        for spell in self._spell_values(spells):
            canonical = normalize_spell_name(spell)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    def class_benefits(self, classes: dict[str, int]) -> dict:
        class_names = set(classes)
        hp_bonus = 5 * len(class_names & HP_BONUS_CLASSES)
        mp_bonus = 5 * len(class_names & MP_BONUS_CLASSES)
        ip_bonus = 2 * len(class_names & IP_BONUS_CLASSES)
        abilities: list[str] = []
        descriptions: list[str] = []
        if hp_bonus:
            descriptions.append(f"职业免费增益：最大 HP +{hp_bonus}")
        if mp_bonus:
            descriptions.append(f"职业免费增益：最大 MP +{mp_bonus}")
        if ip_bonus:
            descriptions.append(f"职业免费增益：库存点 +{ip_bonus}")
        if class_names & MARTIAL_MELEE_CLASSES:
            abilities.append("可装备职业近战武器")
        if class_names & MARTIAL_RANGED_CLASSES:
            abilities.append("可装备职业远程武器")
        if class_names & MARTIAL_ARMOR_CLASSES:
            abilities.append("可装备职业盔甲")
        if class_names & MARTIAL_SHIELD_CLASSES:
            abilities.append("可装备职业盾牌")
        if "造物使" in class_names:
            abilities.append("可发起项目")
        descriptions.extend(abilities)
        return {
            "hp": hp_bonus,
            "mp": mp_bonus,
            "ip": ip_bonus,
            "abilities": abilities,
            "descriptions": descriptions,
        }

    def build_equipment_plan(
        self,
        requested_equipment: list[str],
        abilities: list[str],
        attributes: dict[str, int],
        equipment_slots: dict[str, str] | None = None,
        skills: dict[str, int] | None = None,
    ) -> EquipmentPlan:
        armor_candidates: list[tuple[str, ArmorDefinition]] = []
        shield_candidates: list[tuple[str, ShieldDefinition]] = []
        weapons: list[WeaponDefinition] = []
        weapon_displays: list[str] = []
        purchased_names: list[str] = []
        templates: dict[str, str] = {}
        cost = 0

        for raw_name in requested_equipment:
            request = self.resolve_equipment_request(raw_name)
            name = request.template_name
            display_name = request.display_name
            if name in ARMOR_TABLE:
                item = ARMOR_TABLE[name]
                if item.name != "无防具":
                    self.ensure_ability(item.required_ability, abilities, item.name)
                    armor_candidates.append((display_name, item))
                    cost += item.price
                    purchased_names.append(display_name)
                    if display_name != item.name:
                        templates[display_name] = item.name
                continue
            if name in SHIELD_TABLE:
                item = SHIELD_TABLE[name]
                self.ensure_ability(item.required_ability, abilities, item.name)
                shield_candidates.append((display_name, item))
                cost += item.price
                purchased_names.append(display_name)
                if display_name != item.name:
                    templates[display_name] = item.name
                continue
            if name in WEAPON_TABLE:
                item = WEAPON_TABLE[name]
                if item.name != "徒手攻击":
                    self.ensure_ability(item.required_ability, abilities, item.name)
                    weapons.append(item)
                    weapon_displays.append(display_name)
                    cost += item.price
                    purchased_names.append(display_name)
                    if display_name != item.name:
                        templates[display_name] = item.name
                continue
            raise ValueError(f"未知或不可作为初始购买的装备：{raw_name}")

        if cost > STARTING_EQUIPMENT_BUDGET:
            raise ValueError(f"起始装备总价 {cost}Z 超过 500Z 预算。")

        slot_aliases = {
            "main_hand": "main_hand",
            "主手": "main_hand",
            "off_hand": "off_hand",
            "副手": "off_hand",
            "armor": "armor",
            "防具": "armor",
            "shield": "shield",
            "盾牌": "shield",
        }
        slots: dict[str, str] = {}
        for raw_slot, raw_value in dict(equipment_slots or {}).items():
            slot = slot_aliases.get(str(raw_slot).strip())
            if slot is None:
                raise ValueError(f"未知装备栏位：{raw_slot}")
            value = str(raw_value or "").strip()
            if slot == "main_hand" and not value:
                continue
            slots[slot] = value

        owned_records: list[tuple[str, str, object]] = [
            *((display, "weapon", item) for display, item in zip(weapon_displays, weapons)),
            *((display, "armor", item) for display, item in armor_candidates),
            *((display, "shield", item) for display, item in shield_candidates),
        ]

        owned_quantities: dict[tuple[str, str], int] = {}
        selected_quantities: dict[tuple[str, str], int] = {}
        for display, item_type, _item in owned_records:
            key = (display, item_type)
            owned_quantities[key] = owned_quantities.get(key, 0) + 1

        def select_owned(raw_value: str, allowed_types: set[str]) -> tuple[str, object] | None:
            value = str(raw_value or "").strip()
            if not value or value in {"空", "无", "卸下", "徒手攻击", "无防具"}:
                return None
            resolved_value: EquipmentRequest | None = None
            depleted_match: tuple[str, tuple[str, str]] | None = None
            try:
                resolved_value = self.resolve_equipment_request(value)
            except ValueError:
                # A slot may already contain the normalized display name, which
                # is intentionally not required to repeat its template suffix.
                pass
            for display, item_type, item in owned_records:
                key = (display, item_type)
                matches_resolved_value = bool(
                    resolved_value is not None
                    and display == resolved_value.display_name
                    and getattr(item, "name", "") == resolved_value.template_name
                )
                if (
                    item_type in allowed_types
                    and (
                        display == value
                        or getattr(item, "name", "") == value
                        or matches_resolved_value
                    )
                ):
                    if selected_quantities.get(key, 0) >= owned_quantities.get(key, 0):
                        depleted_match = (display, key)
                        continue
                    selected_quantities[key] = selected_quantities.get(key, 0) + 1
                    return display, item
            if depleted_match is not None:
                display, key = depleted_match
                required_count = selected_quantities.get(key, 0) + 1
                purchased_count = owned_quantities.get(key, 0)
                raise ValueError(
                    f"装备栏数量不足：需要 {required_count} 件【{display}】，"
                    f"但只购买了 {purchased_count} 件。"
                )
            raise ValueError(
                f"起始装备栏指定了未购买、数量不足或类型不符的装备：【{value}】。"
            )

        dual_shield_training = skill_rank(skills or {}, "双盾战士") > 0

        selected_armor = (
            select_owned(slots.get("armor", ""), {"armor"})
            if "armor" in slots
            else (armor_candidates[0] if armor_candidates else None)
        )
        armor_display, armor = (
            selected_armor
            if selected_armor is not None
            else ("无防具", ARMOR_TABLE["无防具"])
        )

        main_allowed_types = {"weapon", "shield"} if dual_shield_training else {"weapon"}
        if "main_hand" in slots:
            selected_main = select_owned(slots.get("main_hand", ""), main_allowed_types)
        elif weapons:
            selected_main = select_owned(weapon_displays[0], {"weapon"})
        elif dual_shield_training and len(shield_candidates) >= 2:
            selected_main = select_owned(shield_candidates[0][0], {"shield"})
        else:
            selected_main = None

        main_shield: ShieldDefinition | None = None
        main_shield_display = ""
        if selected_main is not None and isinstance(selected_main[1], ShieldDefinition):
            main_shield_display, main_shield = selected_main
            main_hand = main_shield_display
            equipped_weapon = WEAPON_TABLE["徒手攻击"]
        else:
            main_hand, equipped_weapon = (
                selected_main
                if selected_main is not None
                else ("徒手攻击", WEAPON_TABLE["徒手攻击"])
            )

        explicit_off = (
            select_owned(slots.get("off_hand", ""), {"weapon", "shield"})
            if "off_hand" in slots
            else None
        )
        if (
            "shield" in slots
            and explicit_off is not None
            and isinstance(explicit_off[1], ShieldDefinition)
            and slots.get("shield", "") == slots.get("off_hand", "")
        ):
            # off_hand and shield are two names for the same physical slot.
            explicit_shield = explicit_off
        else:
            explicit_shield = (
                select_owned(slots.get("shield", ""), {"shield"})
                if "shield" in slots
                else None
            )
        shield: ShieldDefinition | None = None
        shield_display = ""
        off_hand = ""
        off_weapon: WeaponDefinition | None = None

        if explicit_off is not None:
            off_display, off_item = explicit_off
            if isinstance(off_item, ShieldDefinition):
                shield_display = off_display
                shield = off_item
            else:
                off_hand = off_display
                off_weapon = off_item
        if explicit_shield is not None:
            explicit_shield_display, explicit_shield_item = explicit_shield
            if shield is not None and shield_display != explicit_shield_display:
                raise ValueError("副手与盾牌栏不能指定两面不同盾牌。")
            shield_display = explicit_shield_display
            shield = explicit_shield_item

        if "off_hand" not in slots and "shield" not in slots:
            if main_shield is None and equipped_weapon.hands == 1 and len(weapons) >= 2:
                selected_second_weapon = select_owned(weapon_displays[1], {"weapon"})
                if selected_second_weapon is not None:
                    second_display, second_weapon = selected_second_weapon
                    if second_weapon.hands == 1:
                        off_hand = second_display
                        off_weapon = second_weapon
            if not off_hand and (main_shield is not None or equipped_weapon.hands == 1):
                for candidate_display, _candidate_shield in shield_candidates:
                    try:
                        selected_default_shield = select_owned(candidate_display, {"shield"})
                    except ValueError:
                        continue
                    if selected_default_shield is not None:
                        shield_display, shield = selected_default_shield
                        break
        elif "off_hand" not in slots and shield is not None:
            off_hand = ""

        dual_shields_equipped = main_shield is not None and shield is not None
        if dual_shields_equipped:
            equipped_weapon = WeaponDefinition(
                "双盾",
                0,
                ("MIG", "MIG"),
                0,
                5 + skill_rank(skills or {}, "防御精通"),
                2,
                "melee",
                "格斗",
            )
        elif main_shield is not None and off_weapon is not None:
            equipped_weapon = off_weapon

        purchased_counts = Counter(purchased_names)
        equipped_counts: Counter[str] = Counter()
        if armor.name != "无防具":
            equipped_counts[armor_display] += 1
        if main_shield is not None:
            equipped_counts[main_shield_display] += 1
        elif equipped_weapon.name != "徒手攻击":
            equipped_counts[main_hand] += 1
        if off_weapon is not None:
            equipped_counts[off_hand] += 1
        if shield is not None:
            equipped_counts[shield_display] += 1
        for display_name, required_count in equipped_counts.items():
            if purchased_counts[display_name] < required_count:
                raise ValueError(
                    f"装备栏需要 {required_count} 件【{display_name}】，"
                    f"但只购买了 {purchased_counts[display_name]} 件。"
                )

        hands_used = 1 if main_shield is not None else equipped_weapon.hands
        if off_weapon is not None:
            if off_weapon.hands != 1:
                raise ValueError("副手只能装备单手武器。")
            hands_used += 1
        if shield is not None:
            hands_used += 1
        if hands_used > 2:
            raise ValueError("当前初始装备栏占用超过两只手；备用装备可以保留在库存中。")
        if dual_shields_equipped or (main_shield is None and equipped_weapon.hands == 2):
            # A two-handed main weapon makes the off-hand unavailable; it does
            # not put a synthetic item into that equipment slot.
            off_hand = ""
        elif shield is not None:
            off_hand = shield_display

        if not purchased_names:
            purchased_names = ["无防具", "徒手攻击"]
        else:
            if armor.name == "无防具":
                purchased_names.insert(0, "无防具")
            if not weapons:
                purchased_names.append("徒手攻击")

        defenses = {
            "physical": self.resolve_defense_value(armor.physical_base, attributes) + armor.physical_bonus,
            "magic": self.resolve_defense_value(armor.magic_base, attributes) + armor.magic_bonus,
        }
        if shield is not None:
            defenses["physical"] += shield.physical_bonus
            defenses["magic"] += shield.magic_bonus
        if main_shield is not None:
            defenses["physical"] += main_shield.physical_bonus
            defenses["magic"] += main_shield.magic_bonus

        return EquipmentPlan(
            names=purchased_names,
            cost=cost,
            armor=armor,
            armor_display=armor_display,
            shield=shield,
            shield_display=shield_display,
            weapons=weapons,
            weapon_displays=weapon_displays,
            defenses=defenses,
            initiative_modifier=armor.initiative_modifier,
            main_hand=main_hand,
            off_hand=off_hand,
            weapon=equipped_weapon,
            templates=templates,
        )

    def normalize_equipment_name(self, raw_name: str) -> str:
        return normalize_equipment_template_name(raw_name)

    def resolve_equipment_request(self, raw_name: str) -> EquipmentRequest:
        """把“叙事外观”解析成“数值模板”。

        例：和服（丝质衬衫模板）、投掷卡牌（按手里剑结算）、赌徒纸牌=手里剑。
        规则表只决定价格、槽位、权限和数值；外观名称由玩家概念决定。
        """

        return resolve_equipment_request_text(raw_name)

    def _split_equipment_template_pair(self, clean: str) -> tuple[str, str] | None:
        return split_equipment_template_pair(clean)

    def _extract_equipment_template_name(self, text: str) -> str:
        return extract_equipment_template_name(text)

    def is_known_equipment(self, name: str) -> bool:
        return is_known_equipment_template(name)

    def normalize_skill_name(self, raw_name: str) -> str:
        return normalize_skill_reference_name(raw_name)

    def ensure_ability(self, required_ability: str, abilities: list[str], item_name: str) -> None:
        if not required_ability:
            return
        if required_ability not in abilities:
            if required_ability == "可装备职业近战武器":
                raise ValueError(
                    f"【{item_name}】是职业限定近战武器；目前职业还没有对应训练，可以换一把基础武器或选择暗刃骑士、怒焰斗士、武器大师。"
                )
            if required_ability == "可装备职业远程武器":
                raise ValueError(
                    f"【{item_name}】是职业限定远程武器；目前职业还没有对应训练，可以换弓/投掷武器或选择神射手。"
                )
            if required_ability == "可装备职业盔甲":
                raise ValueError(f"【{item_name}】需要职业盔甲权限；可以选择守护者、暗刃骑士或怒焰斗士。")
            if required_ability == "可装备职业盾牌":
                raise ValueError(f"【{item_name}】需要职业盾牌权限；可以选择守护者、神射手或武器大师。")
            raise ValueError(f"【{item_name}】缺少装备权限：{required_ability}")

    def resolve_defense_value(self, base: str | int, attributes: dict[str, int]) -> int:
        if isinstance(base, int):
            return base
        return attributes[base]

    def creation_warnings(self, profile: HeroCreationProfile) -> list[str]:
        warnings: list[str] = []
        if len(profile.classes) == 4:
            warnings.append("四职业属于本桌 GM 通融特例；总等级 5 与每级 1 个职业技能仍照常结算。")
        if not profile.bonds:
            warnings.append("建议至少和一名 PC、NPC、组织或地点建立起始羁绊。")
        if not profile.notes:
            warnings.append("建议记录一个未解决的私人问题，方便 GM 在第一幕使用。")
        missing_spells = self.missing_spell_choices(profile.skills, profile.spells)
        if missing_spells:
            warnings.append("授法技能还需要补法术选择：" + "、".join(missing_spells))
        advisory_choices = self.unresolved_skill_choice_requirements(
            profile.skills,
            skill_options=profile.skill_options,
            spells=profile.spells,
            bound_arcana=profile.bound_arcana,
            include_optional=True,
        )
        for requirement in advisory_choices:
            if requirement.get("required_for_creation"):
                continue
            if requirement.get("choice_key") == "initial_arcanum":
                warnings.append("可以选择一个起始绑定的奥灵；暂不选择不会阻止建卡。")
            elif requirement.get("choice_key") == "companion_profile":
                warnings.append("【忠诚伙伴】的伙伴资料尚未协作建立，首次使用前需要完成。")
        return warnings

    def next_questions_for(self, profile: HeroCreationProfile) -> list[str]:
        questions: list[str] = []
        missing_spells = self.missing_spell_choices(profile.skills, profile.spells)
        if missing_spells:
            questions.append(f"{profile.hero_name} 还差法术选择；玩家询问可选项时再展示对应标准法术列表。")
        if not profile.bonds:
            questions.append(f"{profile.hero_name} 最信任谁，或者最不愿再见到谁？")
        if not profile.notes:
            questions.append(f"{profile.hero_name} 的主题【{profile.theme}】第一次伤害他们，是在什么时候？")
        advisory_choices = self.unresolved_skill_choice_requirements(
            profile.skills,
            skill_options=profile.skill_options,
            spells=profile.spells,
            bound_arcana=profile.bound_arcana,
            include_optional=True,
        )
        for requirement in advisory_choices:
            if requirement.get("required_for_creation"):
                continue
            if requirement.get("choice_key") == "initial_arcanum":
                options = "、".join(requirement.get("allowed_values") or [])
                questions.append(
                    f"{profile.hero_name} 是否要在创建时选择一个已结契奥灵？可选：{options}。"
                )
            elif requirement.get("choice_key") == "companion_profile":
                questions.append(
                    f"{profile.hero_name} 的忠诚伙伴是什么生物，它最擅长怎样协助队伍？"
                )
        return questions[:2]

    def suggest_hero_angles(self) -> list[str]:
        world = self.world_state.world_profile
        suggestions: list[str] = []
        if world.group_concept:
            suggestions.append(f"围绕小队原型【{world.group_concept}】，每名英雄给出一个加入理由和一个隐秘顾虑。")
        if world.world_style == "科技奇幻":
            suggestions.extend(
                [
                    "可以创建一名被财阀实验改造后逃出的英雄。",
                    "可以创建一名曾为压迫者工作、如今寻求赎罪的前雇佣兵。",
                ]
            )
        elif world.world_style == "自然奇幻":
            suggestions.extend(
                [
                    "可以创建一名想证明自己的村长之女或学徒。",
                    "可以创建一名听得见自然精灵警告的年轻贤者。",
                ]
            )
        elif world.world_style == "高度奇幻":
            suggestions.extend(
                [
                    "可以创建一名失去王国的公主或誓言骑士。",
                    "可以创建一名寻找传奇咒语的老魔导师或天空海盗。",
                ]
            )
        if world.villain_seeds:
            suggestions.append("至少一名英雄最好和第一个反派种子有直接或主题上的联系。")
        return suggestions

    def build_party_sheet(self, shared_goal: str = "", party_notes: list[str] | None = None) -> PartySheet:
        world = self.world_state.world_profile
        members: list[PartyMemberEntry] = []
        for character in self.character_manager.all():
            if "pc" not in character.traits:
                continue
            profile = self.hero_profiles.get(character.name)
            members.append(
                PartyMemberEntry(
                    player_name=profile.player_name if profile is not None else "",
                    hero_name=character.name,
                    identity=character.identity,
                    theme=character.theme,
                    origin=character.origin,
                    classes=dict(character.classes),
                    skills=dict(character.skills),
                    skill_options={name: list(values) for name, values in character.skill_options.items()},
                    equipment=list(character.equipment),
                    zenit=character.zenit,
                    bonds=[self.format_bond(bond) for bond in character.bonds],
                )
            )
        sheet = PartySheet(
            group_concept=world.group_concept,
            shared_goal=shared_goal,
            starting_region=world.starting_region,
            members=members,
            party_notes=list(party_notes or []),
            open_questions=list(world.open_questions),
        )
        self.world_state.apply_party_sheet(sheet)
        return sheet

    def build_world_sheet(self) -> WorldSheet:
        world = self.world_state.world_profile
        sheet = WorldSheet(
            campaign_title=world.campaign_title,
            continent_name=world.continent_name,
            world_style=world.world_style,
            pillars=dict(world.pillars),
            core_themes=list(world.core_themes),
            starting_region=world.starting_region,
            major_locations=dict(world.major_locations),
            factions=dict(world.factions),
            villain_seeds=list(world.villain_seeds),
            villain_mirrors=list(world.villain_mirrors),
            mysteries=list(world.mysteries),
            selected_first_act=world.selected_first_act_summary,
            starting_bond_suggestions=list(world.starting_bond_suggestions),
            safety_lines=list(world.safety_lines),
            safety_veils=list(world.safety_veils),
        )
        self.world_state.apply_world_sheet(sheet)
        return sheet

    def finalize_campaign_creation(
        self,
        *,
        shared_goal: str = "",
        party_notes: list[str] | None = None,
    ) -> CampaignCreationBundle:
        world_sheet = self.build_world_sheet()
        party_sheet = self.build_party_sheet(shared_goal=shared_goal, party_notes=party_notes)
        characters = [character for character in self.character_manager.all() if "pc" in character.traits]
        self.world_state.add_memory("Session 0 闭环完成：世界表、小队表与起始 PC 已生成。")
        return CampaignCreationBundle(
            world_sheet=world_sheet,
            party_sheet=party_sheet,
            characters=characters,
        )

    def format_bond(self, bond: Bond) -> str:
        emotions = "、".join(bond.emotions) if bond.emotions else "未定义情感"
        return f"{bond.target}（强度 {bond.strength}：{emotions}）"

