from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from fu_gm.models import OptionalRuleState, WorldCreationProfile


@dataclass(frozen=True)
class OptionalRuleDefinition:
    key: str
    label: str
    category: str
    summary: str
    aliases: tuple[str, ...] = ()


OPTIONAL_RULE_CATALOG: tuple[OptionalRuleDefinition, ...] = (
    OptionalRuleDefinition(
        key="success_at_cost",
        label="以代价换成功",
        category="check",
        summary="检定失败后，玩家可接受严重代价将失败转为成功；大失败不能使用。",
        aliases=("以代价获得成功", "代价成功", "失败转成功"),
    ),
    OptionalRuleDefinition(
        key="invoke_for_failure",
        label="以援用换失败",
        category="check",
        summary="玩家可通过援用特质或羁绊选择自动失败以获得物语点。",
        aliases=("调用自动失败", "援用自动失败", "特质自动失败", "羁绊自动失败"),
    ),
    OptionalRuleDefinition(
        key="ambush_round",
        label="偷袭轮",
        category="conflict",
        summary="伏击或突袭时使用额外开场轮；默认不启用，避免冲突节奏过硬。",
        aliases=("伏击轮", "突袭轮"),
    ),
    OptionalRuleDefinition(
        key="out_of_turn_reroll",
        label="冲突外玩家重掷",
        category="table",
        summary="不在当前场景或冲突中的玩家可按可选规则影响重掷。",
        aliases=("旁观玩家重掷", "场外重掷"),
    ),
    OptionalRuleDefinition(
        key="dominance_points",
        label="战斗制霸",
        category="conflict",
        summary="使用制霸/压制点改变战斗节奏；默认关闭。",
        aliases=("压制点", "制霸点", "战斗压制"),
    ),
    OptionalRuleDefinition(
        key="quirks",
        label="奇能",
        category="expansion",
        summary="扩展规则：角色获得更个性化的奇能能力。",
        aliases=("quirk", "quirks"),
    ),
    OptionalRuleDefinition(
        key="zero_powers",
        label="零界力量",
        category="expansion",
        summary="扩展规则：使用零界力量相关机制。",
        aliases=("零界", "zero power", "zero powers"),
    ),
    OptionalRuleDefinition(
        key="camp_activities",
        label="营地活动",
        category="expansion",
        summary="扩展规则：休息或旅途中使用营地活动。",
        aliases=("营地行动", "露营活动"),
    ),
    OptionalRuleDefinition(
        key="techno_spheres",
        label="科技灵球",
        category="expansion",
        summary="扩展规则：使用科技灵球/魔科技球相关装备或能力。",
        aliases=("魔科技球", "科技球", "灵球"),
    ),
    OptionalRuleDefinition(
        key="vehicle_conflict",
        label="载具级冲突",
        category="expansion",
        summary="扩展规则：载具作为冲突主体参与行动、伤害与场景节奏。",
        aliases=("载具战", "飞空艇战", "车战", "舰战"),
    ),
    OptionalRuleDefinition(
        key="solo_play",
        label="单人跑团档位",
        category="table",
        summary="一名玩家角色游玩时调整遭遇、线索冗余、伙伴支援与行动剥夺风险。",
        aliases=("单人模式", "单人跑团", "solo play", "solo mode"),
    ),
    OptionalRuleDefinition(
        key="organized_chronicles_mode",
        label="最终编年史组织化章节模式",
        category="table",
        summary="使用官方战役式章节限制：章节包、标志性元素保护、物语点重置、临时羁绊与更严格牺牲条件。",
        aliases=("最终编年史模式", "官方战役模式", "组织化章节", "organized play", "chronicles mode"),
    ),
)

_BY_KEY = {definition.key: definition for definition in OPTIONAL_RULE_CATALOG}
_ALIASES: dict[str, str] = {}
for definition in OPTIONAL_RULE_CATALOG:
    _ALIASES[definition.key.lower()] = definition.key
    _ALIASES[definition.label] = definition.key
    for alias in definition.aliases:
        _ALIASES[alias.lower()] = definition.key
        _ALIASES[alias] = definition.key


def normalize_optional_rule_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _ALIASES.get(text, _ALIASES.get(text.lower(), text))


def optional_rule_label(key: str) -> str:
    normalized = normalize_optional_rule_key(key)
    definition = _BY_KEY.get(normalized)
    return definition.label if definition is not None else normalized


def apply_optional_rule_state(
    profile: WorldCreationProfile,
    key: str,
    *,
    enabled: bool,
    note: str = "",
    source: str = "",
) -> OptionalRuleState:
    normalized = normalize_optional_rule_key(key)
    if not normalized:
        raise ValueError("可选规则键不能为空。")
    current = profile.optional_rules.get(normalized, OptionalRuleState())
    state = OptionalRuleState(
        enabled=bool(enabled),
        note=note or current.note,
        source=source or current.source,
    )
    profile.optional_rules[normalized] = state
    return state


def optional_rule_rows(profile: WorldCreationProfile) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for definition in OPTIONAL_RULE_CATALOG:
        state = profile.optional_rules.get(definition.key, OptionalRuleState())
        rows.append(
            {
                "key": definition.key,
                "label": definition.label,
                "category": definition.category,
                "summary": definition.summary,
                "enabled": bool(state.enabled),
                "note": state.note,
                "source": state.source,
            }
        )
    extra_keys = sorted(key for key in profile.optional_rules if key not in _BY_KEY)
    for key in extra_keys:
        state = profile.optional_rules[key]
        rows.append(
            {
                "key": key,
                "label": key,
                "category": "custom",
                "summary": "自定义可选规则。",
                "enabled": bool(state.enabled),
                "note": state.note,
                "source": state.source,
            }
        )
    return rows


def format_optional_rules_for_prompt(profile: WorldCreationProfile) -> str:
    rows = optional_rule_rows(profile)
    enabled = [row for row in rows if row["enabled"]]
    disabled = [row for row in rows if not row["enabled"]]
    parts = ["可选规则默认关闭；只有本列表显示已启用时，AI GM 才能使用相应可选规则。"]
    if enabled:
        parts.append("已启用：" + "、".join(row["label"] for row in enabled))
    if disabled:
        parts.append("未启用：" + "、".join(row["label"] for row in disabled[:10]))
    return "\n".join(parts)


def detect_optional_rule_mentions(text: str) -> list[str]:
    raw = str(text or "")
    lowered = raw.lower()
    matches: list[str] = []
    for definition in OPTIONAL_RULE_CATALOG:
        needles = (definition.label, *definition.aliases, definition.key)
        if any(needle and (needle in raw or needle.lower() in lowered) for needle in needles):
            matches.append(definition.key)
    return matches


def text_disables_optional_rule(text: str) -> bool:
    return any(token in str(text or "") for token in ("不启用", "不用", "不开", "关闭", "禁用", "默认关闭", "先别用"))


def text_enables_optional_rule(text: str) -> bool:
    raw = str(text or "")
    return bool(re.search(r"(?<!不)(启用|使用|采用|想用|可以用|打开)", raw))
