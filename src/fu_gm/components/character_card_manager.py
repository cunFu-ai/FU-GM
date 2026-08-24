from __future__ import annotations

import re
import uuid
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Any

from fu_gm.components.character_creation_manager import (
    ARMOR_TABLE,
    RECOMMENDED_STARTING_ATTRIBUTE_PATTERNS,
    REQUIRED_ATTRIBUTES,
    SHIELD_TABLE,
    STARTING_EQUIPMENT_BUDGET,
    WEAPON_TABLE,
    CharacterCreationManager,
)
from fu_gm.components.portable_device_rules import PORTABLE_DEVICE_TYPES
from fu_gm.models import (
    Affinity,
    Bond,
    Character,
    HeroCreationProfile,
    PartyMemberEntry,
    PartySheet,
    StatusEffect,
)
from fu_gm.skill_library import CLASS_SKILL_REFERENCES, CORE_CLASS_NAMES
from fu_gm.spellbook import (
    SPELLS,
    SPELL_ALIASES,
    canonical_spell_names,
    spell_school_for,
)


CHARACTER_CARD_SCHEMA = "fabula-ultima.character-card"
CHARACTER_CARD_SCHEMA_VERSION = 1
CHARACTER_CARD_RULESET = "fabula-ultima-scn-1.03"
CHARACTER_CARD_CATALOG_REVISION = "fabula-ultima-scn-1.03-r1"
LEGACY_CHARACTER_CARD_SCHEMAS = {"fu-gm.character-card"}

_CARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_TEXT = 4000
_MAX_COLLECTION = 128
_BOND_EMOTIONS = ("钦佩", "自卑", "信赖", "猜忌", "喜爱", "憎恨")
_ARCANA = (
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


class CharacterCardError(ValueError):
    pass


class CharacterCardManager:
    """Versioned, player-safe character-card import/export boundary."""

    def __init__(self, creation: CharacterCreationManager) -> None:
        self.creation = creation

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def catalog(self) -> dict[str, Any]:
        skills_by_class: dict[str, list[dict[str, Any]]] = {
            name: [] for name in CORE_CLASS_NAMES
        }
        for reference in CLASS_SKILL_REFERENCES:
            skills_by_class.setdefault(reference.class_name, []).append(
                {
                    "name": reference.name,
                    "max_ranks": reference.max_ranks,
                    "summary": reference.summary,
                    "tags": list(reference.tags),
                }
            )
        benefits: dict[str, dict[str, Any]] = {}
        for class_name in CORE_CLASS_NAMES:
            class_benefit = self.creation.class_benefits({class_name: 1})
            benefits[class_name] = {
                "hp": class_benefit["hp"],
                "mp": class_benefit["mp"],
                "ip": class_benefit["ip"],
                "abilities": list(class_benefit["abilities"]),
            }
        spells = []
        for spell_name in canonical_spell_names():
            spell = SPELLS[spell_name]
            spells.append(
                {
                    "name": spell_name,
                    "school": spell_school_for(spell_name),
                    "mp_cost": spell.mp_cost,
                    "target": getattr(spell.target, "value", str(spell.target)),
                    "description": spell.description,
                }
            )
        return {
            "ok": True,
            "schema": CHARACTER_CARD_SCHEMA,
            "schema_version": CHARACTER_CARD_SCHEMA_VERSION,
            "ruleset": CHARACTER_CARD_RULESET,
            "catalog_revision": CHARACTER_CARD_CATALOG_REVISION,
            "classes": [
                {
                    "name": name,
                    "benefit": benefits[name],
                    "skills": skills_by_class.get(name, []),
                }
                for name in CORE_CLASS_NAMES
            ],
            "attribute_patterns": [
                {"name": name, "dice": list(dice)}
                for name, dice in RECOMMENDED_STARTING_ATTRIBUTE_PATTERNS
            ],
            "equipment_budget": STARTING_EQUIPMENT_BUDGET,
            "equipment": {
                "armor": [self._equipment_row(item) for item in ARMOR_TABLE.values()],
                "shields": [self._equipment_row(item) for item in SHIELD_TABLE.values()],
                "weapons": [self._equipment_row(item) for item in WEAPON_TABLE.values()],
            },
            "spells": spells,
            "spell_granting_skills": {
                "元素魔法": "元素使法术",
                "熵系魔法": "熵术士法术",
                "灵魂魔法": "御魂使法术",
            },
            "spell_aliases": {
                alias: canonical
                for alias, canonical in SPELL_ALIASES.items()
                if alias != canonical and canonical in SPELLS
            },
            "bond_emotions": list(_BOND_EMOTIONS),
            "portable_device_types": list(PORTABLE_DEVICE_TYPES),
            "arcana": list(_ARCANA),
        }

    @staticmethod
    def _equipment_row(item: object) -> dict[str, Any]:
        row = asdict(item)
        if "accuracy_attributes" in row:
            row["accuracy_attributes"] = list(row["accuracy_attributes"])
        return row

    def preview_build(
        self,
        payload: dict[str, Any],
        *,
        use_pending_fate: bool = True,
    ) -> dict[str, Any]:
        build = self._extract_build(payload)
        profile = self.profile_from_build(build)
        raw_fate = payload.get("fate_roll") or build.get("fate_roll") or []
        fate_roll = self._fate_roll(raw_fate, allow_empty=use_pending_fate)
        result = self.creation.create_player_character(
            profile,
            fate_roll=fate_roll or (1, 1),
            register=False,
        )
        presentation = self._mapping(payload.get("presentation"), "presentation")
        appearance = self._mapping(
            presentation.get("appearance", payload.get("appearance", {})),
            "appearance",
        )
        portrait = self._mapping(
            presentation.get("portrait", payload.get("portrait", {})),
            "portrait",
        )
        extensions = self._mapping(payload.get("extensions"), "extensions")
        self._validate_tree(appearance, "appearance")
        self._validate_tree(portrait, "portrait")
        self._validate_tree(extensions, "extensions")
        result.character.appearance = deepcopy(appearance)
        result.character.portrait = deepcopy(portrait)
        result.character.extensions = deepcopy(extensions)
        derived = self.derived_payload(result.character)
        if not fate_roll:
            derived["starting_zenit"] = None
            derived["zenit_before_fate"] = STARTING_EQUIPMENT_BUDGET - result.equipment_cost
        return {
            "ok": True,
            "valid": True,
            "warnings": list(result.warnings),
            "applied_benefits": list(result.applied_benefits),
            "next_questions": list(result.next_questions),
            "equipment_cost": result.equipment_cost,
            "fate_roll": list(fate_roll),
            "fate_pending": not bool(fate_roll),
            "derived": derived,
            "normalized_build": self.build_from_profile(profile),
        }

    def card_from_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        build = self._extract_build(payload)
        profile = self.profile_from_build(build)
        fate_roll = self._fate_roll(
            payload.get("fate_roll") or build.get("fate_roll") or [],
            allow_empty=False,
        )
        result = self.creation.create_player_character(
            profile,
            fate_roll=fate_roll,
            register=False,
        )
        presentation = self._mapping(payload.get("presentation"), "presentation")
        appearance = self._mapping(
            presentation.get("appearance", payload.get("appearance", {})),
            "appearance",
        )
        portrait = self._mapping(
            presentation.get("portrait", payload.get("portrait", {})),
            "portrait",
        )
        extensions = self._mapping(payload.get("extensions"), "extensions")
        card_id = self._card_id(payload.get("card_id") or "")
        revision = self._positive_int(payload.get("revision", 1), "revision", maximum=1_000_000)
        character = result.character
        character.card_id = card_id
        character.card_revision = revision
        character.appearance = deepcopy(appearance)
        character.portrait = deepcopy(portrait)
        character.extensions = deepcopy(extensions)
        return self.export_character(
            character,
            profile=profile,
            exported_at=self.now(),
        )

    def export_character(
        self,
        character: Character,
        member: PartyMemberEntry | None = None,
        *,
        profile: HeroCreationProfile | None = None,
        exported_at: str = "",
    ) -> dict[str, Any]:
        player_name = (
            profile.player_name
            if profile is not None
            else (member.player_name if member is not None else character.player_name)
        )
        build = (
            self.build_from_profile(profile)
            if profile is not None
            else self.build_from_character(character, player_name=player_name)
        )
        build["fate_roll"] = list(character.creation_fate_roll)
        card_id = self._card_id(character.card_id or "")
        character.card_id = card_id
        character.card_revision = max(1, int(character.card_revision or 1))
        return {
            "$schema": CHARACTER_CARD_SCHEMA,
            "schema_version": CHARACTER_CARD_SCHEMA_VERSION,
            "ruleset": {
                "id": CHARACTER_CARD_RULESET,
                "catalog_revision": CHARACTER_CARD_CATALOG_REVISION,
            },
            "card": {
                "id": card_id,
                "revision": character.card_revision,
                "exported_at": exported_at or self.now(),
            },
            "build": build,
            "state": self.state_payload(character),
            "derived": self.derived_payload(character),
            "presentation": {
                "appearance": deepcopy(character.appearance),
                "portrait": deepcopy(character.portrait),
            },
            "extensions": deepcopy(character.extensions),
            "character_snapshot": self._character_snapshot(character),
        }

    def export_character_text(self, card: dict[str, Any]) -> str:
        warnings: list[str] = []
        normalized = self._normalize_card(card, warnings=warnings)
        character, profile = self._candidate_from_card(normalized, warnings=warnings)

        lines = ["《最终物语》角色卡", "=" * 24, ""]

        def section(title: str, values: list[str]) -> None:
            lines.append(f"【{title}】")
            lines.extend(values or ["- 无"])
            lines.append("")

        lines.extend(
            [
                f"角色名：{character.name}",
                f"玩家：{profile.player_name or '未记录'}",
                f"等级：{character.level}",
                f"身份：{character.identity}",
                f"主题：{character.theme}",
                f"故乡：{character.origin}",
                "",
            ]
        )
        section(
            "职业",
            [f"- {name} Lv.{level}" for name, level in character.classes.items()],
        )
        attribute_labels = {
            "DEX": "敏捷",
            "INS": "洞察",
            "MIG": "力量",
            "WLP": "意志",
        }
        section(
            "属性",
            [
                f"- {attribute_labels.get(name, name)}（{name}）：d{die}"
                for name, die in character.attributes.items()
            ],
        )
        section(
            "资源与防御",
            [
                f"- HP：{character.hp}/{character.max_hp}（危机值 {character.crisis_threshold or character.max_hp // 2}）",
                f"- MP：{character.mp}/{character.max_mp}",
                f"- IP：{character.inventory_points}/{character.max_inventory_points or character.inventory_points}",
                f"- 物防：{character.defenses.get('physical', 0)}",
                f"- 魔防：{character.defenses.get('magic', 0)}",
                f"- 先攻：{character.initiative:+d}",
            ],
        )
        skill_lines: list[str] = []
        for name, rank in character.skills.items():
            suffix = f" SL {rank}" if rank > 1 else ""
            options = character.skill_options.get(name, [])
            option_text = f"（选择：{'、'.join(options)}）" if options else ""
            skill_lines.append(f"- {name}{suffix}{option_text}")
        section("技能", skill_lines)
        section("法术", [f"- {name}" for name in character.spells])
        section("绑定奥灵", [f"- {name}" for name in character.bound_arcana])
        section("能力", [f"- {name}" for name in character.abilities])

        equipment_lines = []
        for name, quantity in Counter(character.equipment).items():
            equipment_lines.append(f"- {name}{f' ×{quantity}' if quantity > 1 else ''}")
        slot_values = [
            ("主手", character.equipped_main_hand or "徒手攻击"),
            ("副手", character.equipped_off_hand or "空"),
            ("防具", character.equipped_armor or "无防具"),
            ("盾牌", character.equipped_shield or "无"),
        ]
        equipment_lines.append("- 装备栏：" + "；".join(f"{label} {value}" for label, value in slot_values))
        section("装备", equipment_lines)
        section(
            "羁绊",
            [
                f"- {bond.target}：{'、'.join(bond.emotions)}（强度 {len(bond.emotions)}）"
                for bond in character.bonds
            ],
        )
        fate_roll = list(character.creation_fate_roll)
        section(
            "物语与成长",
            [
                f"- 物语点：{character.fabula_points}",
                f"- 经验值：{character.experience_points}",
                f"- 金币：{character.zenit} Z",
                f"- 起始命运骰：{' + '.join(str(value) for value in fate_roll) if fate_roll else '未记录'}",
                f"- 异常状态：{'、'.join(status.value for status in character.statuses) or '无'}",
            ],
        )
        section("笔记", [f"- {note}" for note in character.notes])
        return "\n".join(lines).rstrip() + "\n"

    def validate_card(self, card: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            normalized = self._normalize_card(card, warnings=warnings)
            candidate, profile = self._candidate_from_card(normalized, warnings=warnings)
        except (CharacterCardError, TypeError, ValueError, KeyError) as exc:
            errors.append(str(exc))
            return {
                "ok": True,
                "valid": False,
                "errors": errors,
                "warnings": warnings,
            }
        return {
            "ok": True,
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "card": normalized,
            "character": self.summary_payload(candidate),
            "normalized_build": self.build_from_profile(profile),
        }

    def import_card(
        self,
        card: dict[str, Any],
        *,
        conflict: str = "reject",
    ) -> tuple[Character, HeroCreationProfile, list[str]]:
        warnings: list[str] = []
        normalized = self._normalize_card(card, warnings=warnings)
        candidate, profile = self._candidate_from_card(normalized, warnings=warnings)
        existing_same_id = next(
            (
                item
                for item in self.creation.character_manager.all()
                if item.card_id and item.card_id == candidate.card_id
            ),
            None,
        )
        existing_same_name = (
            self.creation.character_manager.get(candidate.name)
            if self.creation.character_manager.exists(candidate.name)
            else None
        )
        mode = str(conflict or "reject").strip().lower()
        if existing_same_id is not None:
            if mode not in {"replace", "replace_id"}:
                raise CharacterCardError(
                    f"角色卡 {candidate.card_id} 已导入为【{existing_same_id.name}】；请选择覆盖。"
                )
            if existing_same_id.name != candidate.name:
                self.creation.character_manager._characters.pop(existing_same_id.name, None)
                party_sheet = self.creation.world_state.party_sheet
                if party_sheet is not None:
                    party_sheet.members = [
                        member
                        for member in party_sheet.members
                        if member.hero_name != existing_same_id.name
                    ]
        elif existing_same_name is not None:
            if mode == "copy":
                candidate.name = self._unique_name(candidate.name)
                profile.hero_name = candidate.name
                candidate.card_id = self._card_id("")
                candidate.card_revision = 1
                warnings.append(f"角色重名，已作为【{candidate.name}】导入。")
            elif mode != "replace":
                raise CharacterCardError(f"角色【{candidate.name}】已经存在；请选择覆盖或导入副本。")
        self.creation.character_manager.add(candidate)
        self.creation.hero_profiles[candidate.name] = deepcopy(profile)
        self._upsert_party_member(candidate, profile)
        self.creation.world_state.add_memory(
            f"角色卡导入：{candidate.name}，由 {profile.player_name or '未记录玩家'} 操控。"
        )
        return candidate, profile, warnings

    def _candidate_from_card(
        self,
        card: dict[str, Any],
        *,
        warnings: list[str],
    ) -> tuple[Character, HeroCreationProfile]:
        build = card["build"]
        profile = self.profile_from_build(build, allow_progressed=True)
        state = card["state"]
        level = self._positive_int(state.get("level", 5), "state.level", maximum=50)
        classes = self.creation.normalize_classes(profile.classes)
        is_starting_build = level == 5 and sum(classes.values()) == 5 and 2 <= len(classes) <= 3
        if is_starting_build:
            fate_roll = self._fate_roll(build.get("fate_roll") or [], allow_empty=False)
            result = self.creation.create_player_character(
                profile,
                fate_roll=fate_roll,
                register=False,
            )
            candidate = result.character
        else:
            snapshot = self._mapping(card.get("character_snapshot"), "character_snapshot")
            if not snapshot:
                raise CharacterCardError("进阶角色卡缺少 character_snapshot，无法无损恢复。")
            candidate = self._advanced_character(snapshot, profile, level=level)
            warnings.append("这是进阶角色卡：已校验职业、技能、属性和数值边界，并按完整快照恢复。")
        self._apply_portable_state(candidate, state)
        metadata = card["card"]
        candidate.card_id = self._card_id(metadata.get("id") or "")
        candidate.card_revision = self._positive_int(
            metadata.get("revision", 1), "card.revision", maximum=1_000_000
        )
        candidate.player_name = profile.player_name
        candidate.appearance = deepcopy(card["presentation"].get("appearance", {}))
        candidate.portrait = deepcopy(card["presentation"].get("portrait", {}))
        candidate.extensions = deepcopy(card.get("extensions", {}))
        candidate.notes = list(profile.notes)
        return candidate, profile

    def _advanced_character(
        self,
        snapshot: dict[str, Any],
        profile: HeroCreationProfile,
        *,
        level: int,
    ) -> Character:
        attributes = self.creation.normalize_attribute_keys(profile.attributes)
        if set(attributes) != set(REQUIRED_ATTRIBUTES):
            raise CharacterCardError("进阶角色的四项属性不完整。")
        if any(value not in {6, 8, 10, 12} for value in attributes.values()):
            raise CharacterCardError("进阶角色属性骰必须是 d6、d8、d10 或 d12。")
        classes = self.creation.normalize_classes(profile.classes)
        if sum(classes.values()) != level:
            raise CharacterCardError("进阶角色的职业等级总和必须等于角色等级。")
        if any(rank < 1 or rank > 10 for rank in classes.values()):
            raise CharacterCardError("每个职业等级必须位于 1 到 10 之间。")
        skills = self.creation.validate_skills(classes, profile.skills)
        allowed_fields = {field.name for field in fields(Character)}
        kwargs = {
            key: deepcopy(value)
            for key, value in snapshot.items()
            if key in allowed_fields and not key.startswith("npc_")
        }
        kwargs.update(
            {
                "name": profile.hero_name,
                "identity": profile.identity,
                "theme": profile.theme,
                "origin": profile.origin,
                "attributes": attributes,
                "classes": classes,
                "skills": skills,
                "level": level,
            }
        )
        for required in ("max_hp", "hp", "max_mp", "mp"):
            kwargs[required] = self._bounded_int(
                kwargs.get(required, 0), f"character_snapshot.{required}", 0, 5000
            )
        if kwargs["max_hp"] <= 0 or kwargs["max_mp"] <= 0:
            raise CharacterCardError("进阶角色的 HP/MP 上限必须大于 0。")
        kwargs["bonds"] = self._bonds(snapshot.get("bonds", []))
        kwargs["statuses"] = self._statuses(snapshot.get("statuses", []))
        for field_name in (
            "permanent_status_immunities",
            "equipment_status_immunities",
            "temporary_status_immunities",
        ):
            kwargs[field_name] = set(self._statuses(snapshot.get(field_name, [])))
        for field_name in ("trigger_cooldowns", "permanent_trigger_keys"):
            kwargs[field_name] = set(
                self._string_list(snapshot.get(field_name, []), field_name)
            )
        for field_name in ("affinities", "temporary_affinities", "equipment_affinities"):
            kwargs[field_name] = self._affinities(snapshot.get(field_name, {}), field_name)
        on_hit = snapshot.get("equipment_on_hit_status")
        kwargs["equipment_on_hit_status"] = self._status(on_hit) if on_hit else None
        kwargs["traits"] = ["pc"]
        character = Character(**kwargs)
        character.hp = min(character.hp, character.max_hp)
        character.mp = min(character.mp, character.max_mp)
        character.crisis_threshold = character.max_hp // 2
        return character

    def _apply_portable_state(self, character: Character, state: dict[str, Any]) -> None:
        character.hp = self._bounded_int(state.get("hp", character.hp), "state.hp", 0, character.max_hp)
        character.mp = self._bounded_int(state.get("mp", character.mp), "state.mp", 0, character.max_mp)
        max_ip = character.max_inventory_points or character.inventory_points or 6
        character.inventory_points = self._bounded_int(
            state.get("inventory_points", character.inventory_points),
            "state.inventory_points",
            0,
            max_ip,
        )
        character.fabula_points = self._bounded_int(
            state.get("fabula_points", character.fabula_points),
            "state.fabula_points",
            0,
            999,
        )
        character.experience_points = self._bounded_int(
            state.get("experience_points", character.experience_points),
            "state.experience_points",
            0,
            999,
        )
        character.zenit = self._bounded_int(state.get("zenit", character.zenit), "state.zenit", 0, 10_000_000)
        character.statuses = self._statuses(state.get("statuses", []))

    def _normalize_card(self, card: dict[str, Any], *, warnings: list[str]) -> dict[str, Any]:
        if not isinstance(card, dict):
            raise CharacterCardError("角色卡顶层必须是 JSON 对象。")
        schema = str(card.get("$schema") or card.get("schema") or "").strip()
        if schema not in {CHARACTER_CARD_SCHEMA, *LEGACY_CHARACTER_CARD_SCHEMAS}:
            raise CharacterCardError(f"不支持的角色卡类型：{schema or '未声明'}。")
        if schema in LEGACY_CHARACTER_CARD_SCHEMAS:
            warnings.append("这是旧版 FU-GM 角色卡，已转换为 Fabula Ultima 角色卡格式。")
        version = self._positive_int(card.get("schema_version", 0), "schema_version", maximum=999)
        if version != CHARACTER_CARD_SCHEMA_VERSION:
            raise CharacterCardError(f"不支持的角色卡版本：{version}。")
        ruleset = self._mapping(card.get("ruleset"), "ruleset")
        if str(ruleset.get("id") or "") != CHARACTER_CARD_RULESET:
            raise CharacterCardError("角色卡规则集与当前《最终物语》核心规则不一致。")
        if str(ruleset.get("catalog_revision") or "") != CHARACTER_CARD_CATALOG_REVISION:
            warnings.append("角色卡目录修订号不同，已按当前规则重新校验。")
        normalized = deepcopy(card)
        normalized["$schema"] = CHARACTER_CARD_SCHEMA
        normalized.pop("schema", None)
        normalized["ruleset"] = deepcopy(ruleset)
        normalized["ruleset"]["catalog_revision"] = CHARACTER_CARD_CATALOG_REVISION
        normalized["card"] = self._mapping(card.get("card"), "card")
        normalized["card"]["id"] = self._card_id(normalized["card"].get("id") or "")
        normalized["card"]["revision"] = self._positive_int(
            normalized["card"].get("revision", 1), "card.revision", maximum=1_000_000
        )
        normalized["build"] = self._mapping(card.get("build"), "build")
        normalized["state"] = self._mapping(card.get("state"), "state")
        normalized["presentation"] = self._mapping(card.get("presentation"), "presentation")
        normalized["presentation"]["appearance"] = self._mapping(
            normalized["presentation"].get("appearance"), "presentation.appearance"
        )
        normalized["presentation"]["portrait"] = self._mapping(
            normalized["presentation"].get("portrait"), "presentation.portrait"
        )
        normalized["extensions"] = self._mapping(card.get("extensions"), "extensions")
        for key in ("presentation", "extensions"):
            self._validate_tree(normalized[key], key)
        return normalized

    def profile_from_build(
        self,
        build: dict[str, Any],
        *,
        allow_progressed: bool = False,
    ) -> HeroCreationProfile:
        required_text = ("hero_name", "identity", "theme", "origin")
        missing = [key for key in required_text if not self._text(build.get(key), key)]
        if missing:
            raise CharacterCardError("角色资料缺少：" + "、".join(missing))
        classes = self._rank_map(build.get("classes"), "classes")
        if allow_progressed:
            classes = self.creation.normalize_classes(classes)
        attributes = self._rank_map(build.get("attributes"), "attributes")
        bonds = self._bonds(build.get("bonds", []))
        skills = self._rank_map(build.get("skills"), "skills")
        skill_options = self._string_list_map(build.get("skill_options", {}), "skill_options")
        equipment_slots = {
            self._text(key, "equipment_slots key"): self._text(value, "equipment_slots value", allow_empty=True)
            for key, value in self._mapping(build.get("equipment_slots"), "equipment_slots").items()
        }
        profile = HeroCreationProfile(
            player_name=self._text(build.get("player_name", ""), "player_name", allow_empty=True),
            hero_name=self._text(build.get("hero_name"), "hero_name"),
            identity=self._text(build.get("identity"), "identity"),
            theme=self._text(build.get("theme"), "theme"),
            origin=self._text(build.get("origin"), "origin"),
            classes=classes,
            attributes=attributes,
            bonds=bonds,
            skills=skills,
            skill_options=skill_options,
            spells=self._string_list(build.get("spells", []), "spells"),
            bound_arcana=self._string_list(build.get("bound_arcana", []), "bound_arcana"),
            abilities=self._string_list(build.get("abilities", []), "abilities"),
            equipment=self._string_list(build.get("equipment", []), "equipment"),
            equipment_slots=equipment_slots,
            notes=self._string_list(build.get("notes", []), "notes"),
        )
        if not allow_progressed:
            profile.classes = self.creation.normalize_classes(profile.classes)
        return profile

    @staticmethod
    def build_from_profile(profile: HeroCreationProfile) -> dict[str, Any]:
        return {
            "player_name": profile.player_name,
            "hero_name": profile.hero_name,
            "identity": profile.identity,
            "theme": profile.theme,
            "origin": profile.origin,
            "classes": dict(profile.classes),
            "attributes": dict(profile.attributes),
            "bonds": [asdict(bond) for bond in profile.bonds],
            "skills": dict(profile.skills),
            "skill_options": {key: list(values) for key, values in profile.skill_options.items()},
            "spells": list(profile.spells),
            "bound_arcana": list(profile.bound_arcana),
            "abilities": list(profile.abilities),
            "equipment": list(profile.equipment),
            "equipment_slots": dict(profile.equipment_slots),
            "notes": list(profile.notes),
        }

    @staticmethod
    def build_from_character(character: Character, *, player_name: str = "") -> dict[str, Any]:
        return {
            "player_name": player_name or character.player_name,
            "hero_name": character.name,
            "identity": character.identity,
            "theme": character.theme,
            "origin": character.origin,
            "classes": dict(character.classes),
            "attributes": dict(character.attributes),
            "bonds": [asdict(bond) for bond in character.bonds],
            "skills": dict(character.skills),
            "skill_options": {key: list(values) for key, values in character.skill_options.items()},
            "spells": list(character.spells),
            "bound_arcana": list(character.bound_arcana),
            "abilities": list(character.abilities),
            "equipment": list(character.equipment),
            "equipment_slots": {
                "main_hand": character.equipped_main_hand,
                "off_hand": character.equipped_off_hand,
                "armor": character.equipped_armor,
                "shield": character.equipped_shield,
            },
            "notes": list(character.notes),
            "fate_roll": list(character.creation_fate_roll),
        }

    @staticmethod
    def state_payload(character: Character) -> dict[str, Any]:
        return {
            "level": character.level,
            "hp": character.hp,
            "mp": character.mp,
            "inventory_points": character.inventory_points,
            "fabula_points": character.fabula_points,
            "experience_points": character.experience_points,
            "zenit": character.zenit,
            "statuses": [status.value for status in character.statuses],
        }

    @staticmethod
    def derived_payload(character: Character) -> dict[str, Any]:
        return {
            "max_hp": character.max_hp,
            "crisis_threshold": character.crisis_threshold or character.max_hp // 2,
            "max_mp": character.max_mp,
            "max_inventory_points": character.max_inventory_points or character.inventory_points,
            "physical_defense": character.defenses.get("physical", 0),
            "magic_defense": character.defenses.get("magic", 0),
            "initiative": character.initiative,
            "weapon": {
                "name": character.equipped_main_hand,
                "accuracy_attributes": list(character.weapon_accuracy_attributes),
                "accuracy_modifier": character.weapon_accuracy_modifier,
                "damage_bonus": character.weapon_damage,
                "damage_type": character.weapon_type,
                "range": character.weapon_range,
            },
            "starting_zenit": character.zenit,
        }

    def summary_payload(self, character: Character) -> dict[str, Any]:
        return {
            "card_id": character.card_id,
            "name": character.name,
            "player_name": character.player_name,
            "identity": character.identity,
            "theme": character.theme,
            "origin": character.origin,
            "level": character.level,
            "classes": dict(character.classes),
            "attributes": dict(character.attributes),
            "state": self.state_payload(character),
            "derived": self.derived_payload(character),
            "portrait": deepcopy(character.portrait),
        }

    def _character_snapshot(self, character: Character) -> dict[str, Any]:
        from fu_gm.components.sheet_exporter import SheetExporter

        return SheetExporter().to_jsonable(character)

    def _upsert_party_member(self, character: Character, profile: HeroCreationProfile) -> None:
        world = self.creation.world_state
        sheet = world.party_sheet
        if sheet is None:
            sheet = PartySheet(
                group_concept=world.world_profile.group_concept,
                starting_region=world.world_profile.starting_region,
            )
            world.party_sheet = sheet
        entry = PartyMemberEntry(
            player_name=profile.player_name,
            hero_name=character.name,
            identity=character.identity,
            theme=character.theme,
            origin=character.origin,
            classes=dict(character.classes),
            skills=dict(character.skills),
            skill_options={key: list(values) for key, values in character.skill_options.items()},
            equipment=list(character.equipment),
            zenit=character.zenit,
            bonds=[f"{bond.target}：{'、'.join(bond.emotions)}" for bond in character.bonds],
        )
        replaced = False
        for index, member in enumerate(sheet.members):
            if member.hero_name == character.name:
                sheet.members[index] = entry
                replaced = True
                break
        if not replaced:
            sheet.members.append(entry)

    def _unique_name(self, base: str) -> str:
        for number in range(2, 1000):
            candidate = f"{base}（{number}）"
            if not self.creation.character_manager.exists(candidate):
                return candidate
        raise CharacterCardError("无法为重名角色生成可用名称。")

    def _extract_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CharacterCardError("建卡数据必须是 JSON 对象。")
        build = payload.get("build", payload)
        return self._mapping(build, "build")

    def _card_id(self, value: object) -> str:
        clean = str(value or "").strip() or str(uuid.uuid4())
        if not _CARD_ID_PATTERN.fullmatch(clean):
            raise CharacterCardError("角色卡 ID 格式不合法。")
        return clean

    def _fate_roll(self, value: object, *, allow_empty: bool) -> tuple[int, ...]:
        if value in (None, "", []):
            if allow_empty:
                return ()
            raise CharacterCardError("完成角色卡前需要掷出两枚起始 d6。")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise CharacterCardError("起始命运骰必须包含两枚 d6。")
        result = tuple(int(item) for item in value)
        if any(item < 1 or item > 6 for item in result):
            raise CharacterCardError("起始命运骰结果必须位于 1 到 6。")
        return result  # type: ignore[return-value]

    def _bonds(self, value: object) -> list[Bond]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or len(value) > 6:
            raise CharacterCardError("羁绊必须是至多六项的列表。")
        bonds: list[Bond] = []
        for index, raw in enumerate(value):
            if isinstance(raw, str):
                bond = self.creation.parse_bond_text(raw)
            elif isinstance(raw, dict):
                target = self._text(raw.get("target"), f"bonds[{index}].target")
                emotions = self._string_list(raw.get("emotions", []), f"bonds[{index}].emotions")
                if len(emotions) > 3 or any(item not in _BOND_EMOTIONS for item in emotions):
                    raise CharacterCardError(f"bonds[{index}] 包含不合法的羁绊情感。")
                groups = [self._emotion_group(item) for item in emotions]
                if len(groups) != len(set(groups)):
                    raise CharacterCardError(f"bonds[{index}] 的同组对立情感不能并存。")
                bond = Bond(target=target, emotions=emotions)
            else:
                raise CharacterCardError(f"bonds[{index}] 必须是对象或文本。")
            bonds.append(bond)
        return bonds

    @staticmethod
    def _emotion_group(value: str) -> str:
        if value in {"钦佩", "自卑"}:
            return "esteem"
        if value in {"信赖", "猜忌"}:
            return "trust"
        return "affection"

    def _statuses(self, value: object) -> list[StatusEffect]:
        if value in (None, ""):
            return []
        if not isinstance(value, (list, tuple, set)) or len(value) > 32:
            raise CharacterCardError("异常状态列表格式不合法。")
        return [self._status(item) for item in value]

    @staticmethod
    def _status(value: object) -> StatusEffect:
        try:
            return value if isinstance(value, StatusEffect) else StatusEffect(str(value))
        except ValueError as exc:
            raise CharacterCardError(f"未知异常状态：{value}") from exc

    def _affinities(self, value: object, field_name: str) -> dict[str, Affinity]:
        raw = self._mapping(value, field_name)
        affinities: dict[str, Affinity] = {}
        for key, item in raw.items():
            try:
                affinities[str(key)] = item if isinstance(item, Affinity) else Affinity(str(item))
            except ValueError as exc:
                raise CharacterCardError(f"{field_name}.{key} 的伤害相性不合法。") from exc
        return affinities

    def _rank_map(self, value: object, field_name: str) -> dict[str, int]:
        raw = self._mapping(value, field_name)
        if len(raw) > _MAX_COLLECTION:
            raise CharacterCardError(f"{field_name} 项目过多。")
        result: dict[str, int] = {}
        for key, item in raw.items():
            name = self._text(key, f"{field_name} key")
            result[name] = self._positive_int(item, f"{field_name}.{name}", maximum=100)
        return result

    def _string_list_map(self, value: object, field_name: str) -> dict[str, list[str]]:
        raw = self._mapping(value, field_name)
        return {
            self._text(key, f"{field_name} key"): self._string_list(item, f"{field_name}.{key}")
            for key, item in raw.items()
        }

    def _string_list(self, value: object, field_name: str) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, (list, tuple, set)) or len(value) > _MAX_COLLECTION:
            raise CharacterCardError(f"{field_name} 必须是列表且项目不能超过 {_MAX_COLLECTION}。")
        return [self._text(item, f"{field_name} item") for item in value]

    @staticmethod
    def _mapping(value: object, field_name: str) -> dict[str, Any]:
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise CharacterCardError(f"{field_name} 必须是对象。")
        return value

    @staticmethod
    def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
        clean = " ".join(str(value or "").split()).strip()
        if not clean and not allow_empty:
            raise CharacterCardError(f"{field_name} 不能为空。")
        if len(clean) > _MAX_TEXT:
            raise CharacterCardError(f"{field_name} 过长。")
        return clean

    @staticmethod
    def _positive_int(value: object, field_name: str, *, maximum: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise CharacterCardError(f"{field_name} 必须是整数。") from exc
        if result < 1 or result > maximum:
            raise CharacterCardError(f"{field_name} 必须位于 1 到 {maximum}。")
        return result

    @staticmethod
    def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise CharacterCardError(f"{field_name} 必须是整数。") from exc
        if result < minimum or result > maximum:
            raise CharacterCardError(f"{field_name} 必须位于 {minimum} 到 {maximum}。")
        return result

    def _validate_tree(self, value: object, field_name: str, *, depth: int = 0) -> None:
        if depth > 8:
            raise CharacterCardError(f"{field_name} 嵌套过深。")
        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, str):
            if len(value) > _MAX_TEXT:
                raise CharacterCardError(f"{field_name} 含有过长文本。")
            return
        if isinstance(value, list):
            if len(value) > _MAX_COLLECTION:
                raise CharacterCardError(f"{field_name} 列表项目过多。")
            for item in value:
                self._validate_tree(item, field_name, depth=depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > _MAX_COLLECTION:
                raise CharacterCardError(f"{field_name} 对象字段过多。")
            for key, item in value.items():
                self._text(key, f"{field_name} key")
                self._validate_tree(item, field_name, depth=depth + 1)
            return
        raise CharacterCardError(f"{field_name} 包含不可序列化的值。")
