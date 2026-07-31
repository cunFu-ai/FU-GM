from __future__ import annotations

import json
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from fu_gm.models import (
    Bond,
    CampaignCreationBundle,
    Character,
    PartyMemberEntry,
    PartySheet,
    SheetExportBundle,
    WorldSheet,
)
from fu_gm.skill_library import normalize_skill_name_list


ATTRIBUTE_LABELS = {
    "DEX": "敏捷",
    "INS": "洞察",
    "MIG": "力量",
    "WLP": "意志",
}

DAMAGE_TYPE_LABELS = {
    "physical": "物理",
    "bolt": "雷",
    "lightning": "雷",
    "air": "风",
    "wind": "风",
    "ice": "冰",
    "fire": "火",
    "earth": "土",
    "poison": "毒",
    "light": "光",
    "dark": "暗",
    "arcane": "奥灵",
    "none": "无属性",
}

RANGE_LABELS = {
    "melee": "近战",
    "ranged": "远程",
}


class SheetExporter:
    """把世界表、小队表和角色表导出为玩家可读 Markdown 与机器可读 JSON。"""

    def export_campaign(self, bundle: CampaignCreationBundle) -> SheetExportBundle:
        member_by_hero = {member.hero_name: member for member in bundle.party_sheet.members}
        character_markdowns = {
            character.name: self.export_character_markdown(character, member_by_hero.get(character.name))
            for character in bundle.characters
        }
        return SheetExportBundle(
            world_markdown=self.export_world_markdown(bundle.world_sheet),
            party_markdown=self.export_party_markdown(bundle.party_sheet),
            character_markdowns=character_markdowns,
            json_payload=self.export_campaign_json(bundle),
        )

    def write_campaign_exports(self, bundle: CampaignCreationBundle, directory: str | Path) -> SheetExportBundle:
        export = self.export_campaign(bundle)
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)

        written_files: dict[str, str] = {}
        world_path = target / "world_sheet.md"
        party_path = target / "party_sheet.md"
        json_path = target / "campaign_bundle.json"

        world_path.write_text(export.world_markdown, encoding="utf-8")
        party_path.write_text(export.party_markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(export.json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        written_files["world_markdown"] = str(world_path.resolve())
        written_files["party_markdown"] = str(party_path.resolve())
        written_files["json"] = str(json_path.resolve())

        for hero_name, markdown in export.character_markdowns.items():
            character_path = target / f"character_{self.safe_filename(hero_name)}.md"
            character_path.write_text(markdown, encoding="utf-8")
            written_files[f"character:{hero_name}"] = str(character_path.resolve())

        export.written_files = written_files
        return export

    def export_world_markdown(self, sheet: WorldSheet) -> str:
        title = sheet.campaign_title or "未命名战役"
        lines = [
            f"# 世界表：{title}",
            "",
            "## 基本信息",
            f"- 世界风格：{sheet.world_style or '未记录'}",
            f"- 起始地区：{sheet.starting_region or '未记录'}",
            f"- 核心主题：{self.join_values(sheet.core_themes)}",
            "",
            "## 八大支柱",
            self.dict_table(sheet.pillars, "支柱", "当前设定"),
            "",
            "## 重要地点",
            self.dict_table(sheet.major_locations, "地点", "说明"),
            "",
            "## 阵营与势力",
            self.dict_table(sheet.factions, "阵营", "说明"),
            "",
            "## 反派种子",
            self.bullet_list(sheet.villain_seeds),
            "",
            "## 反派映照原则",
            self.bullet_list(sheet.villain_mirrors),
            "",
            "## 未解谜团",
            self.bullet_list(sheet.mysteries),
            "",
            "## 第一幕开局",
            self.bullet_list([sheet.selected_first_act] if sheet.selected_first_act else []),
            "",
            "## 可选初始羁绊",
            self.bullet_list(sheet.starting_bond_suggestions),
            "",
            "## 仪式与世界变化",
            self.bullet_list(sheet.persistent_changes),
            "",
            "## 发明与资产",
            self.bullet_list(sheet.created_assets),
            "",
            "## 地点设施",
            self.dict_list_table(sheet.location_facilities, "地点", "设施/变化"),
            "",
            "## 界限与帷幕",
            f"- 界限：{self.join_values(sheet.safety_lines)}",
            f"- 帷幕：{self.join_values(sheet.safety_veils)}",
        ]
        return "\n".join(lines).strip() + "\n"

    def export_party_markdown(self, sheet: PartySheet) -> str:
        lines = [
            "# 小队表",
            "",
            "## 小队概念",
            f"- 小队原型：{sheet.group_concept or '未记录'}",
            f"- 共同目标：{sheet.shared_goal or '未记录'}",
            f"- 起始地区：{sheet.starting_region or '未记录'}",
            "",
            "## 成员",
            self.member_table(sheet.members),
            "",
            "## 小队备注",
            self.bullet_list(sheet.party_notes),
            "",
            "## 待确认问题",
            self.bullet_list(sheet.open_questions),
        ]
        return "\n".join(lines).strip() + "\n"

    def export_character_markdown(
        self,
        character: Character,
        member: PartyMemberEntry | None = None,
    ) -> str:
        max_ip = character.max_inventory_points or character.inventory_points
        player_name = member.player_name if member is not None else ""
        lines = [
            f"# 角色表：{character.name}",
            "",
            "## 基本信息",
            f"- 玩家：{player_name or '未记录'}",
            f"- 身份：{character.identity or '未记录'}",
            f"- 主题：{character.theme or '未记录'}",
            f"- 起源：{character.origin or '未记录'}",
            f"- 等级：{character.level}",
            f"- 经验值：{character.experience_points}/10",
            "",
            "## 属性",
            self.attribute_table(character),
            "",
            "## 资源",
            f"- HP：{character.hp}/{character.max_hp}",
            f"- 危机值：{character.crisis_threshold or character.max_hp // 2}",
            f"- MP：{character.mp}/{character.max_mp}",
            f"- IP：{character.inventory_points}/{max_ip}",
            f"- 物语点：{character.fabula_points}",
            f"- 泽尼特：{character.zenit}Z",
            "",
            "## 战斗数值",
            f"- 物防 DEF：{character.defenses.get('physical', 0)}",
            f"- 魔防 MDEF：{character.defenses.get('magic', 0)}",
            f"- 先攻修正：{character.initiative:+d}",
            f"- 当前武器：{character.equipped_main_hand}",
            f"- 命中检定：{self.format_accuracy(character)}",
            f"- 伤害公式：【HR+{character.weapon_damage}】{self.damage_label(character.weapon_type)}",
            f"- 攻击范围：{RANGE_LABELS.get(character.weapon_range, character.weapon_range)}",
            "",
            "## 职业与技能",
            f"- 职业：{self.format_rank_map(character.classes)}",
            f"- 技能：{self.format_rank_map(character.skills)}",
            f"- 英雄技能：{self.join_values(normalize_skill_name_list(character.hero_skills))}",
            f"- 法术：{self.join_values(character.spells)}",
            f"- 能力：{self.join_values(character.abilities)}",
            "",
            "## 装备",
            f"- 已购买/携带：{self.join_values(character.equipment)}",
            f"- 数值模板：{self.format_equipment_templates(character.equipment_templates)}",
            f"- 防具：{character.equipped_armor or '无防具'}",
            f"- 盾牌：{character.equipped_shield or '未装备'}",
            f"- 主手：{character.equipped_main_hand or '徒手攻击'}",
            f"- 副手：{character.equipped_off_hand or '空置'}",
            "",
            "## 羁绊",
            self.bond_list(character.bonds),
            "",
            "## 状态与相性",
            f"- 当前状态：{self.join_values([status.value for status in character.statuses])}",
            f"- 伤害相性：{self.format_affinities(character)}",
        ]
        return "\n".join(lines).strip() + "\n"

    def format_equipment_templates(self, templates: dict[str, str]) -> str:
        flavored = [f"{display}=>{template}" for display, template in templates.items() if display != template]
        return "、".join(flavored) if flavored else "外观与规则表一致"

    def export_campaign_json(self, bundle: CampaignCreationBundle) -> dict[str, Any]:
        return {
            "world_sheet": self.to_jsonable(bundle.world_sheet),
            "party_sheet": self.to_jsonable(bundle.party_sheet),
            "characters": [self.to_jsonable(character) for character in bundle.characters],
        }

    def to_jsonable(self, value: Any) -> Any:
        if is_dataclass(value):
            return {field.name: self.to_jsonable(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {str(self.to_jsonable(key)): self.to_jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.to_jsonable(item) for item in value]
        return value

    def safe_filename(self, value: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value.strip())
        cleaned = re.sub(r"\s+", "_", cleaned)
        return cleaned or "未命名角色"

    def attribute_table(self, character: Character) -> str:
        lines = ["| 属性 | 骰子 |", "|---|---|"]
        for key in ("DEX", "INS", "MIG", "WLP"):
            lines.append(f"| {ATTRIBUTE_LABELS[key]}（{key}） | d{character.attributes.get(key, 6)} |")
        return "\n".join(lines)

    def member_table(self, members: list[PartyMemberEntry]) -> str:
        if not members:
            return "- 尚未创建 PC。"
        lines = [
            "| 玩家 | 英雄 | 身份 | 主题 | 起源 | 职业 | 技能 | 装备 | 泽尼特 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for member in members:
            lines.append(
                "| "
                + " | ".join(
                    [
                        self.cell(member.player_name),
                        self.cell(member.hero_name),
                        self.cell(member.identity),
                        self.cell(member.theme),
                        self.cell(member.origin),
                        self.cell(self.format_rank_map(member.classes)),
                        self.cell(self.format_rank_map(member.skills)),
                        self.cell(self.join_values(member.equipment)),
                        self.cell(f"{member.zenit}Z"),
                    ]
                )
                + " |"
            )
        return "\n".join(lines)

    def dict_table(self, values: dict[str, str], key_label: str, value_label: str) -> str:
        if not values:
            return "- 尚未记录。"
        lines = [f"| {key_label} | {value_label} |", "|---|---|"]
        for key, value in values.items():
            lines.append(f"| {self.cell(key)} | {self.cell(value)} |")
        return "\n".join(lines)

    def dict_list_table(self, values: dict[str, list[str]], key_label: str, value_label: str) -> str:
        if not values:
            return "- 尚未记录。"
        lines = [f"| {key_label} | {value_label} |", "|---|---|"]
        for key, items in values.items():
            lines.append(f"| {self.cell(key)} | {self.cell(self.join_values(items))} |")
        return "\n".join(lines)

    def bullet_list(self, values: list[str]) -> str:
        clean_values = [value for value in values if value]
        if not clean_values:
            return "- 尚未记录。"
        return "\n".join(f"- {value}" for value in clean_values)

    def bond_list(self, bonds: list[Bond]) -> str:
        if not bonds:
            return "- 尚未建立羁绊。"
        return "\n".join(
            f"- {bond.target}：强度 {bond.strength}（{self.join_values(bond.emotions, '未定义情感')}）"
            for bond in bonds
        )

    def format_rank_map(self, values: dict[str, int]) -> str:
        if not values:
            return "未记录"
        return "、".join(f"{name} {rank}" for name, rank in values.items())

    def format_accuracy(self, character: Character) -> str:
        attributes = "+".join(
            ATTRIBUTE_LABELS.get(attribute, attribute)
            for attribute in (character.weapon_accuracy_attributes or ["DEX", "MIG"])
        )
        modifier = character.weapon_accuracy_modifier
        return f"【{attributes}】{modifier:+d}" if modifier else f"【{attributes}】"

    def format_affinities(self, character: Character) -> str:
        if not character.affinities:
            return "默认普通"
        return "、".join(f"{damage_type}:{affinity.value}" for damage_type, affinity in character.affinities.items())

    def damage_label(self, damage_type: str) -> str:
        return DAMAGE_TYPE_LABELS.get(damage_type, damage_type)

    def join_values(self, values: list[str], empty: str = "未记录") -> str:
        clean_values = [str(value) for value in values if str(value)]
        return "、".join(clean_values) if clean_values else empty

    def cell(self, value: Any) -> str:
        text = str(value) if value not in (None, "") else "未记录"
        return text.replace("|", "/").replace("\n", " ")
