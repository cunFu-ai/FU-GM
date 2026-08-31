from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum

from fu_gm.equipment_catalog import get_equipment_example, search_equipment_examples
from fu_gm.gm_tool_contracts import (
    GMToolDefinition,
    GMToolExecutionContext,
    GMToolParameter,
    GMToolReceipt,
    GMToolRegistry,
)
from fu_gm.models import EquipmentItemType
from fu_gm.skill_library import (
    CLASS_REFERENCES,
    SKILL_REFERENCES,
    get_class_reference,
    get_skill_reference,
    search_class_references,
    search_skill_references,
    skill_implementation_coverage,
)
from fu_gm.spellbook import (
    canonical_spell_names,
    get_spell_definition,
    is_known_spell,
    normalize_spell_name,
    spell_names_for_school,
    spell_school_for,
)


_ATTRIBUTE_LABELS = {
    "DEX": "敏捷",
    "INS": "洞察",
    "MIG": "力量",
    "WLP": "意志",
}
_DAMAGE_LABELS = {
    "physical": "物理",
    "wind": "风",
    "lightning": "雷",
    "dark": "暗",
    "earth": "土",
    "fire": "火",
    "ice": "冰",
    "light": "光",
    "poison": "毒",
    "arcane": "奥术",
}
_SPELL_EFFECT_TAGS = {
    "damage": ("伤害",),
    "mp_damage": ("精神伤害", "控制"),
    "heal": ("治疗", "支援"),
    "defense_buff": ("防护",),
    "defense_floor": ("防护",),
    "affinity_buff": ("防护",),
    "status_apply": ("控制", "异常状态"),
    "status_clear": ("净化", "支援"),
    "status_immunity": ("防护", "净化"),
    "weapon_enchant": ("附魔", "支援"),
    "attribute_buff": ("强化", "支援"),
    "extra_action": ("行动", "支援"),
    "survive_once": ("防护",),
    "dispel": ("驱散", "支援"),
    "check_bonus": ("强化", "支援"),
    "damage_vulnerability": ("削弱", "控制"),
    "immediate_attack": ("攻击",),
    "narrative": ("功能",),
}


class GMReferenceToolService:
    """Read-only rule catalog exposed to the GM semantic agent.

    Catalog text is reference material. Numeric resolution still belongs to
    the interceptor and domain managers, so these tools can never mutate a
    character merely because the model quoted a rule.
    """

    _KINDS = ("class", "skill", "spell", "equipment")
    _SEARCH_VIEWS = ("overview", "shortlist", "names")

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_rule_reference",
                description=(
                    "按精确名称或已登记别名查询职业、技能、法术或装备规则。"
                    "玩家询问可选项或具体规则时先查目录，不凭模型记忆补写数值。"
                ),
                handler=self.get_rule_reference,
                parameters=(
                    GMToolParameter("kind", "string", "规则类别。", required=True, enum=self._KINDS),
                    GMToolParameter("name", "string", "规则书名称或已登记别名。", required=True),
                ),
                is_concurrency_safe=True,
            )
        )
        registry.register(
            GMToolDefinition(
                name="search_rule_references",
                description=(
                    "按职业定位、技能所属职业、法术学派、装备类别或用途标签搜索规则候选。"
                    "默认只返回精简候选；玩家明确索要完整名称时才使用names视图。"
                ),
                handler=self.search_rule_references,
                parameters=(
                    GMToolParameter("kind", "string", "规则类别。", required=True, enum=self._KINDS),
                    GMToolParameter("text", "string", "可选名称、效果或自由文本关键词。"),
                    GMToolParameter(
                        "query_tags",
                        "array",
                        "从玩家需求归纳出的用途标签，如防护、支援、控制或旅行。",
                        schema_details={"items": {"type": "string"}, "maxItems": 6},
                    ),
                    GMToolParameter(
                        "class_name",
                        "string",
                        "技能所属的标准职业名；玩家以职业名询问技能列表时填写此项。",
                    ),
                    GMToolParameter(
                        "skill_kind",
                        "string",
                        "技能类别；按职业列出起始技能时使用class。",
                        enum=("class", "hero", "npc"),
                    ),
                    GMToolParameter("school", "string", "法术所属学派。"),
                    GMToolParameter("category", "string", "装备类别，如剑、弓、盾牌。"),
                    GMToolParameter("item_type", "string", "装备类型。", enum=tuple(item.value for item in EquipmentItemType)),
                    GMToolParameter("max_price", "integer", "装备最高价格。"),
                    GMToolParameter("include_artifacts", "boolean", "是否允许神器。"),
                    GMToolParameter(
                        "view",
                        "string",
                        "overview返回分类和样例；shortlist返回少量相关候选；names只返回名称。",
                        enum=self._SEARCH_VIEWS,
                    ),
                    GMToolParameter("cursor", "integer", "继续上一查询时使用的起始位置。"),
                    GMToolParameter("limit", "integer", "本页返回数量，默认3，最多20。"),
                ),
                is_concurrency_safe=True,
                max_model_result_chars=5000,
            )
        )

    def state_summary(self, _context: GMToolExecutionContext) -> dict[str, object]:
        return {
            "class_count": len(CLASS_REFERENCES),
            "skill_count": len(SKILL_REFERENCES),
            "spell_count": len(canonical_spell_names()),
            "rule_reference_tools": ["get_rule_reference", "search_rule_references"],
        }

    def get_rule_reference(
        self,
        _context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        kind = self._clean(arguments.get("kind"))
        name = self._clean(arguments.get("name"))
        if kind == "class":
            reference = get_class_reference(name)
            if reference is None:
                return self._not_found("get_rule_reference", kind, name)
            result = self._class_record(reference, include_skills=True)
            reply = f"【{reference.name}】{reference.summary}"
        elif kind == "skill":
            reference = get_skill_reference(name)
            if reference is None:
                return self._not_found("get_rule_reference", kind, name)
            coverage = skill_implementation_coverage(reference.name)
            result = {
                "kind": "skill",
                "name": reference.name,
                "display_name": reference.display_name,
                "class_name": reference.class_name,
                "max_ranks": reference.max_ranks,
                "rank_notation": self._skill_rank_notation(reference.max_ranks),
                "summary": reference.summary,
                "aliases": list(reference.aliases),
                "choice_requirements": self._skill_choice_records(reference),
                "implementation": self._primitive(coverage) if coverage is not None else None,
            }
            reply = f"【{reference.display_name}】{reference.summary}"
        elif kind == "spell":
            canonical = normalize_spell_name(name)
            if not is_known_spell(canonical):
                return self._not_found("get_rule_reference", kind, name)
            spell = get_spell_definition(canonical)
            result = self._spell_record(spell)
            reply = self._spell_reply(result)
        else:
            equipment = get_equipment_example(name)
            if equipment is None:
                return self._not_found("get_rule_reference", kind, name)
            result = self._equipment_record(equipment)
            reply = equipment.summary
        supports_committed_action = self._supports_committed_action(_context)
        receipt_result = dict(result)
        if not supports_committed_action:
            receipt_result["terminal_public_result"] = True
        return GMToolReceipt(
            tool_name="get_rule_reference",
            ok=True,
            result=receipt_result,
            public_fallback_reply=reply,
            lock_public_reply=not supports_committed_action,
        )

    @staticmethod
    def _supports_committed_action(context: GMToolExecutionContext) -> bool:
        semantics = context.metadata.get("_gm_message_semantics")
        if not isinstance(semantics, dict):
            return False
        return any(
            isinstance(event, dict)
            and str(event.get("action_commitment") or "").strip()
            == "committed"
            for event in list(semantics.get("events") or [])
        )

    def search_rule_references(
        self,
        _context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        kind = self._clean(arguments.get("kind"))
        text = self._clean(arguments.get("text"))
        query_tags = self._clean_string_list(arguments.get("query_tags"))
        view = self._clean(arguments.get("view")) or "shortlist"
        if view not in self._SEARCH_VIEWS:
            view = "shortlist"
        default_limit = 5 if view == "names" else 3
        limit = max(1, min(20, int(arguments.get("limit") or default_limit)))
        cursor = max(0, int(arguments.get("cursor") or 0))
        class_name = self._clean(arguments.get("class_name"))
        school = self._clean(arguments.get("school"))

        if kind == "class":
            candidates = list(
                search_class_references(
                    text=text,
                    tags=query_tags,
                    limit=len(CLASS_REFERENCES),
                )
            )
        elif kind == "skill":
            candidates = list(
                search_skill_references(
                    kind=(
                        self._clean(arguments.get("skill_kind"))
                        or ("class" if class_name else "")
                    ),
                    class_name=class_name,
                    limit=len(SKILL_REFERENCES),
                )
            )
            candidates = self._rank_candidates(
                candidates,
                terms=[text, *query_tags],
                haystack=self._skill_search_text,
            )
        elif kind == "spell":
            names = list(spell_names_for_school(school) if school else canonical_spell_names())
            candidates = [get_spell_definition(name) for name in names]
            candidates = self._rank_candidates(
                candidates,
                terms=[text, *query_tags],
                haystack=self._spell_search_text,
            )
        else:
            raw_max_price = arguments.get("max_price")
            max_price = int(raw_max_price) if raw_max_price is not None else None
            candidates = list(
                search_equipment_examples(
                    item_type=self._clean(arguments.get("item_type")) or None,
                    category=self._clean(arguments.get("category")),
                    max_price=max_price,
                    include_artifacts=bool(arguments.get("include_artifacts", False)),
                    limit=1000,
                )
            )
            candidates = self._rank_candidates(
                candidates,
                terms=[text, *query_tags],
                haystack=self._equipment_search_text,
            )
        if not candidates:
            return self._not_found("search_rule_references", kind, text or "当前筛选条件")

        page = candidates[cursor : cursor + limit]
        rows = [self._compact_search_record(kind, item, view=view) for item in page]
        total_count = len(candidates)
        next_cursor = cursor + len(page)
        has_more = next_cursor < total_count
        scope = {
            key: value
            for key, value in {
                "class_name": class_name,
                "school": school,
                "category": self._clean(arguments.get("category")),
                "item_type": self._clean(arguments.get("item_type")),
                "max_price": arguments.get("max_price"),
            }.items()
            if value not in (None, "")
        }
        query_id = "|".join(
            [kind, view, str(scope), text, ",".join(query_tags)]
        )
        return GMToolReceipt(
            tool_name="search_rule_references",
            ok=True,
            result={
                "kind": kind,
                "view": view,
                "count": len(rows),
                "total_count": total_count,
                "references": rows,
                "categories": self._search_categories(kind, candidates),
                "scope": scope,
                "query_id": query_id,
                "cursor": cursor,
                "has_more": has_more,
                "next_cursor": next_cursor if has_more else None,
            },
            public_fallback_reply=f"规则目录里找到 {total_count} 项相关候选。",
            # Search results are private reference material. The unified GM
            # agent decides how much of this page the player actually needs.
            lock_public_reply=False,
        )

    @classmethod
    def _class_record(cls, reference, *, include_skills: bool) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "class",
            "name": reference.name,
            "summary": reference.summary,
            "tags": list(reference.tags),
            "free_benefits": {
                "hp": reference.hp_bonus,
                "mp": reference.mp_bonus,
                "ip": reference.ip_bonus,
                "abilities": list(reference.abilities),
            },
        }
        if include_skills:
            result["skill_names"] = [
                item.name
                for item in SKILL_REFERENCES
                if item.kind == "class" and item.class_name == reference.name
            ]
        return result

    @classmethod
    def _compact_search_record(
        cls,
        kind: str,
        item,
        *,
        view: str,
    ) -> dict[str, object]:
        if view == "names":
            return {"name": str(item.name)}
        if kind == "class":
            return cls._class_record(item, include_skills=False)
        if kind == "skill":
            return {
                "name": item.name,
                "class_name": item.class_name,
                "max_ranks": item.max_ranks,
                "summary": cls._truncate(item.summary),
                "choice_labels": [choice.label for choice in item.choice_specs],
            }
        if kind == "spell":
            return {
                "name": item.name,
                "school": spell_school_for(item.name),
                "mp_cost": item.mp_cost,
                "target": cls._primitive(item.target),
                "summary": cls._truncate(item.description),
                "tags": cls._spell_tags(item),
            }
        return {
            "name": item.name,
            "item_type": item.item_type.value,
            "price": item.price,
            "category": item.category,
            "summary": cls._truncate(item.summary),
        }

    @staticmethod
    def _rank_candidates(candidates, *, terms: list[str], haystack) -> list:
        normalized_terms = [
            str(term or "").strip().lower()
            for term in terms
            if str(term or "").strip()
        ]
        if not normalized_terms:
            return list(candidates)
        ranked: list[tuple[int, int, object]] = []
        for index, item in enumerate(candidates):
            searchable = str(haystack(item) or "").lower()
            score = sum(1 for term in normalized_terms if term in searchable)
            if score > 0:
                ranked.append((score, -index, item))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [item for _score, _index, item in ranked]

    @classmethod
    def _skill_search_text(cls, reference) -> str:
        return " ".join(
            [
                reference.name,
                reference.class_name,
                reference.summary,
                *reference.aliases,
                *reference.tags,
                *cls._semantic_tags_from_text(reference.summary),
            ]
        )

    @classmethod
    def _spell_search_text(cls, spell) -> str:
        return " ".join(
            [
                spell.name,
                spell_school_for(spell.name),
                spell.description,
                *cls._spell_tags(spell),
            ]
        )

    @classmethod
    def _equipment_search_text(cls, equipment) -> str:
        return " ".join(
            [
                equipment.name,
                equipment.category,
                equipment.damage_type,
                equipment.summary,
                *equipment.effects,
                *equipment.tags,
                *equipment.aliases,
                *cls._semantic_tags_from_text(equipment.summary),
            ]
        )

    @classmethod
    def _spell_tags(cls, spell) -> list[str]:
        tags = {"法术"}
        effect_type = str(cls._primitive(spell.effect_type) or "")
        tags.update(_SPELL_EFFECT_TAGS.get(effect_type, ()))
        deals_damage = effect_type in {"damage", "mp_damage"} or int(
            spell.fixed_damage or 0
        ) > 0
        if deals_damage:
            tags.add("伤害")
        if deals_damage and spell.damage_type:
            tags.add(_DAMAGE_LABELS.get(spell.damage_type, spell.damage_type))
        status = str(cls._primitive(spell.status_effect) or "")
        if status and status not in {"none", "无"}:
            tags.update(("异常状态", "控制"))
        tags.update(cls._semantic_tags_from_text(spell.description))
        return sorted(tags)

    @staticmethod
    def _semantic_tags_from_text(text: str) -> set[str]:
        clean = str(text or "")
        tags: set[str] = set()
        groups = {
            "防护": ("保护", "获得抵抗", "免疫", "物防", "魔防", "屏障"),
            "治疗": ("恢复", "治疗", "治愈"),
            "支援": ("盟友", "目标获得", "属性视为", "解除"),
            "控制": ("迟缓", "眩晕", "动摇", "虚弱", "中毒", "少执行一次行动"),
            "机动": ("移动", "传送", "飞行", "落地", "顺势攻击"),
            "调查": ("调查", "揭示", "提问", "回忆"),
            "旅行": ("旅行", "世界地图", "旅店", "酒馆"),
            "物资": ("物资点", "药剂", "制造", "工程"),
        }
        for tag, markers in groups.items():
            if any(marker in clean for marker in markers):
                tags.add(tag)
        return tags

    @classmethod
    def _search_categories(cls, kind: str, candidates) -> list[dict[str, object]]:
        counts: dict[str, int] = {}
        for item in candidates:
            if kind == "class":
                tags = item.tags
            elif kind == "skill":
                tags = (*item.tags, *cls._semantic_tags_from_text(item.summary))
            elif kind == "spell":
                tags = cls._spell_tags(item)
            else:
                tags = (*item.tags, *cls._semantic_tags_from_text(item.summary))
            for tag in tags:
                clean = str(tag or "").strip()
                if clean:
                    counts[clean] = counts.get(clean, 0) + 1
        ranked = sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:6]
        return [{"name": name, "count": count} for name, count in ranked]

    @staticmethod
    def _clean_string_list(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _truncate(value: object, limit: int = 180) -> str:
        text = " ".join(str(value or "").split()).strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _skill_rank_notation(max_ranks: int) -> dict[str, object]:
        maximum = max(1, int(max_ranks))
        notation = f"（+{maximum}）" if maximum > 1 else ""
        return {
            "notation": notation,
            "repeatable": maximum > 1,
            "maximum_acquisitions": maximum,
            "meaning": (
                f"{notation}表示这项技能最多可以取得{maximum}次；"
                "每次取得使该角色的技能等级提高1。"
                "它不表示角色当前已经达到该等级，也不是检定或数值修正。"
                "角色当前技能等级只能从角色卡读取。"
                if maximum > 1
                else "此技能通常只能取得一次；角色是否已经取得须从角色卡读取。"
            ),
        }

    @classmethod
    def _spell_record(cls, spell) -> dict[str, object]:
        return {
            "kind": "spell",
            "name": spell.name,
            "school": spell_school_for(spell.name),
            "mp_cost": spell.mp_cost,
            "target": cls._primitive(spell.target),
            "effect_type": cls._primitive(spell.effect_type),
            "attributes": [_ATTRIBUTE_LABELS.get(value, value) for value in spell.attributes],
            "requires_check": bool(spell.requires_check),
            "duration": cls._primitive(spell.duration),
            "fixed_damage": spell.fixed_damage,
            "damage_type": _DAMAGE_LABELS.get(spell.damage_type, spell.damage_type),
            "description": spell.description,
            "status_effect": cls._primitive(spell.status_effect),
            "ignore_resist": bool(spell.ignore_resist),
        }

    @classmethod
    def _equipment_record(cls, equipment) -> dict[str, object]:
        return {
            "kind": "equipment",
            "name": equipment.name,
            "item_type": equipment.item_type.value,
            "price": equipment.price,
            "category": equipment.category,
            "accuracy_attributes": [
                _ATTRIBUTE_LABELS.get(value, value) for value in equipment.accuracy_attributes
            ],
            "accuracy_modifier": equipment.accuracy_modifier,
            "damage_bonus": equipment.damage_bonus,
            "damage_type": _DAMAGE_LABELS.get(equipment.damage_type, equipment.damage_type),
            "hands": equipment.hands,
            "range_type": equipment.range_type,
            "physical_defense": equipment.physical_defense,
            "magic_defense": equipment.magic_defense,
            "initiative_modifier": equipment.initiative_modifier,
            "required_ability": equipment.required_ability,
            "effects": list(equipment.effects),
            "aliases": list(equipment.aliases),
            "summary": equipment.summary,
        }

    @staticmethod
    def _spell_reply(result: dict[str, object]) -> str:
        cost = result.get("mp_cost")
        description = str(result.get("description") or "")
        return f"【{result['name']}】消耗 {cost} MP。{description}"

    @staticmethod
    def _not_found(tool_name: str, kind: str, name: str) -> GMToolReceipt:
        labels = {
            "class": "职业",
            "skill": "技能",
            "spell": "法术",
            "equipment": "装备",
        }
        label = labels.get(kind, "规则条目")
        return GMToolReceipt(
            tool_name=tool_name,
            ok=False,
            error_code="RULE_REFERENCE_NOT_FOUND",
            message=f"没有找到{label}【{name}】。",
            correction_hint="检查名称或改用search_rule_references按职业、学派或关键词查询。",
            retryable=True,
            public_fallback_reply=f"我没在当前规则库里找到【{name}】，要不要换个关键词查？",
        )

    @classmethod
    def _primitive(cls, value):
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return cls._primitive(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._primitive(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._primitive(item) for item in value]
        return value

    @staticmethod
    def _skill_choice_records(reference) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for choice in reference.choice_specs:
            allowed_values = list(choice.options)
            if choice.storage_field == "spells" and choice.option_source:
                allowed_values = list(spell_names_for_school(choice.option_source))
            rows.append(
                {
                    "choice_key": choice.key,
                    "choice_label": choice.label,
                    "storage_field": choice.storage_field,
                    "timing": choice.timing,
                    "count_mode": choice.count_mode,
                    "required_for_creation": choice.required_for_creation,
                    "option_source": choice.option_source,
                    "allowed_values": allowed_values,
                    "guidance": choice.guidance,
                }
            )
        return rows

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()
