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
    SKILL_REFERENCES,
    get_skill_reference,
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


class GMReferenceToolService:
    """Read-only rule catalog exposed to the GM semantic agent.

    Catalog text is reference material. Numeric resolution still belongs to
    the interceptor and domain managers, so these tools can never mutate a
    character merely because the model quoted a rule.
    """

    _KINDS = ("skill", "spell", "equipment")

    def register_tools(self, registry: GMToolRegistry) -> None:
        registry.register(
            GMToolDefinition(
                name="get_rule_reference",
                description=(
                    "按精确名称或已登记别名查询技能、法术或装备规则。"
                    "玩家询问可选项或具体规则时先查目录，不凭模型记忆补写数值。"
                ),
                handler=self.get_rule_reference,
                parameters=(
                    GMToolParameter("kind", "string", "规则类别。", required=True, enum=self._KINDS),
                    GMToolParameter("name", "string", "规则书名称或已登记别名。", required=True),
                ),
            )
        )
        registry.register(
            GMToolDefinition(
                name="search_rule_references",
                description=(
                    "按职业、学派、装备类别或关键词列出规则书候选；"
                    "只有玩家主动询问选项或GM确实需要裁定时使用，不主动倾倒整张表。"
                ),
                handler=self.search_rule_references,
                parameters=(
                    GMToolParameter("kind", "string", "规则类别。", required=True, enum=self._KINDS),
                    GMToolParameter("text", "string", "可选名称、效果或标签关键词。"),
                    GMToolParameter("class_name", "string", "技能所属职业。"),
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
                    GMToolParameter("limit", "integer", "返回数量，默认10，最多20。"),
                ),
            )
        )

    def state_summary(self, _context: GMToolExecutionContext) -> dict[str, object]:
        return {
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
        if kind == "skill":
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
                "summary": reference.summary,
                "aliases": list(reference.aliases),
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
        return GMToolReceipt(
            tool_name="get_rule_reference",
            ok=True,
            result=result,
            public_fallback_reply=reply,
        )

    def search_rule_references(
        self,
        _context: GMToolExecutionContext,
        arguments: dict[str, object],
    ) -> GMToolReceipt:
        kind = self._clean(arguments.get("kind"))
        text = self._clean(arguments.get("text"))
        limit = max(1, min(20, int(arguments.get("limit") or 10)))
        if kind == "skill":
            rows = [
                {
                    "name": item.name,
                    "display_name": item.display_name,
                    "class_name": item.class_name,
                    "max_ranks": item.max_ranks,
                    "summary": item.summary,
                    "hero_draft_patch": {
                        "skills": {item.name: 1},
                        "increment_skills": True,
                    },
                }
                for item in search_skill_references(
                    kind=(
                        self._clean(arguments.get("skill_kind"))
                        or ("class" if self._clean(arguments.get("class_name")) else "")
                    ),
                    class_name=self._clean(arguments.get("class_name")),
                    text=text,
                    limit=limit,
                )
            ]
        elif kind == "spell":
            school = self._clean(arguments.get("school"))
            names = list(spell_names_for_school(school) if school else canonical_spell_names())
            if text:
                query = text.lower()
                names = [
                    name
                    for name in names
                    if query in name.lower() or query in get_spell_definition(name).description.lower()
                ]
            rows = [self._spell_record(get_spell_definition(name)) for name in names[:limit]]
        else:
            raw_max_price = arguments.get("max_price")
            max_price = int(raw_max_price) if raw_max_price is not None else None
            rows = [
                self._equipment_record(item)
                for item in search_equipment_examples(
                    item_type=self._clean(arguments.get("item_type")) or None,
                    category=self._clean(arguments.get("category")),
                    max_price=max_price,
                    text=text,
                    include_artifacts=bool(arguments.get("include_artifacts", False)),
                    limit=limit,
                )
            ]
        if not rows:
            return self._not_found("search_rule_references", kind, text or "当前筛选条件")
        names = [str(row.get("display_name") or row.get("name") or "") for row in rows]
        return GMToolReceipt(
            tool_name="search_rule_references",
            ok=True,
            result={"kind": kind, "count": len(rows), "references": rows},
            public_fallback_reply="可选项有：" + "、".join(name for name in names if name) + "。",
        )

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
        labels = {"skill": "技能", "spell": "法术", "equipment": "装备"}
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
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()
