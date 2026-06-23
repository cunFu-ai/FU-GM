from __future__ import annotations

import re


def extract_safety_declarations(message: str) -> list[tuple[str, str]]:
    """从自然语言中提取界限与帷幕声明。

    返回值中的类型统一为 line 或 veil；调用方负责决定如何写入状态。
    """

    declarations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, item in _explicit_declarations(message) + _natural_declarations(message):
        kind = normalize_safety_type(label)
        clean_item = clean_safety_item(item)
        if not clean_item or (kind, clean_item) in seen:
            continue
        seen.add((kind, clean_item))
        declarations.append((kind, clean_item))
    return declarations


def normalize_safety_type(declaration_type: str) -> str:
    aliases = {
        "line": "line",
        "lines": "line",
        "界限": "line",
        "线": "line",
        "veil": "veil",
        "veils": "veil",
        "帷幕": "veil",
        "面纱": "veil",
    }
    kind = aliases.get(declaration_type.strip().lower()) or aliases.get(declaration_type.strip())
    if kind is None:
        raise ValueError("界限与帷幕类型必须是 line/界限 或 veil/帷幕。")
    return kind


def clean_safety_item(item: str) -> str:
    cleaned = item.strip().strip("。；; ，,")
    cleaned = re.sub(r"^(?:出现|有|包含|涉及|提到|看到|看见|任何|所有|关于|这类|这种|这样的|相关的)\s*", "", cleaned)
    cleaned = re.sub(
        r"(?:这种内容|这类内容|相关内容|这部分|这些|这种|这类|能不能|可不可以|可以吗|好不好|行不行|请|吧|了)$",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:淡出处理|淡出|带过|一笔带过|黑屏处理|拉灯处理|幕后处理|放到幕后|不要细写|不要详细描写|不详细描写|不明确描写)$",
        "",
        cleaned,
    )
    return cleaned.strip().strip("。；; ，,")


def _explicit_declarations(message: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    pattern = re.compile(r"(界限|帷幕|面纱)\s*[:：]\s*(.+?)(?=(?:界限|帷幕|面纱)\s*[:：]|$)")
    for match in pattern.finditer(message):
        label = match.group(1)
        item = clean_safety_item(match.group(2))
        if item:
            declarations.append((label, item))
    return declarations


def _natural_declarations(message: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    clauses = [clause.strip() for clause in re.split(r"[。！？；;，,\n]", message) if clause.strip()]
    for clause in clauses:
        veil_item = _extract_veil_item(clause)
        if veil_item:
            declarations.append(("veil", veil_item))
            continue
        line_item = _extract_line_item(clause)
        if line_item:
            declarations.append(("line", line_item))
    return declarations


def _extract_line_item(clause: str) -> str:
    # “要不要先调查宝箱？”是桌面讨论，不是“不要出现 X”的安全声明。
    if re.search(r"要\s*不要", clause):
        return ""
    if _looks_like_non_safety_preference(clause):
        return ""
    patterns = [
        r"(?:我|我们)?\s*(?:不希望|不想|不愿意|不接受|不能接受|接受不了|受不了|希望不要|请不要|不要|别|禁止)\s*(?:在游戏中|在游戏里|游戏中|游戏里|故事里|剧情里)?\s*(?:出现|有|包含|涉及|提到)?\s*(?P<item>[^，,。！？；;\n]+)",
        r"(?P<item>[^，,。！？；;\n]+?)\s*(?:我|我们)?\s*(?:不接受|不能接受|接受不了|受不了|不舒服|很不舒服|会不适|有雷|是雷点)",
        r"(?P<item>[^，,。！？；;\n]+?)\s*(?:不要出现|不能出现|别出现|禁止出现|不要有|别有|接受不了|受不了)",
        r"(?:我|我们)?\s*(?:对)?\s*(?P<item>[^，,。！？；;\n]+?)\s*(?:不舒服|很不舒服|会不适|有雷|是雷点)",
    ]
    return _first_match_item(patterns, clause)


def _extract_veil_item(clause: str) -> str:
    if _looks_like_non_safety_preference(clause):
        return ""
    patterns = [
        r"(?:我|我们)?\s*(?:不希望|不想|不愿意|希望不要|请不要|不要|别)\s*(?:明确|直接|详细|细致|正面|过度)?\s*(?:描写|描述|展开|聚焦|细写|细说)\s*(?P<item>[^，,。！？；;\n]+)",
        r"(?P<item>[^，,。！？；;\n]+?)\s*(?:可以存在|可以有|能存在|可以出现).*(?:但|但是).*(?:淡出|带过|一笔带过|黑屏|拉灯|幕后|不(?:要)?(?:明确|直接|详细|细致|正面|过度)?(?:描写|描述|展开|聚焦|细写|细说))",
        r"(?:把|请把|希望把)?\s*(?P<item>[^，,。！？；;\n]+?)\s*(?:淡出处理|淡出|带过|一笔带过|黑屏处理|拉灯处理|幕后处理|放到幕后|放在幕后|不要细写|不要详细描写|不详细描写|不明确描写)",
    ]
    return _first_match_item(patterns, clause)


def _first_match_item(patterns: list[str], clause: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, clause)
        if match:
            return clean_safety_item(match.group("item"))
    return ""


def _looks_like_non_safety_preference(clause: str) -> bool:
    """Avoid treating map/style preferences as lines and veils."""

    text = str(clause or "")
    if not any(token in text for token in ("不要", "不想", "不希望", "别")):
        return False
    preference_tokens = (
        "地图",
        "世界形状",
        "环形世界",
        "球形世界",
        "大陆",
        "群岛",
        "海岸",
        "内海",
        "地形",
        "版图",
        "画风",
        "风格",
        "玩法",
        "类型",
        "偏好",
        "Nortantis",
    )
    if not any(token in text for token in preference_tokens):
        return False
    safety_tokens = (
        "不适",
        "不舒服",
        "受不了",
        "接受不了",
        "不能接受",
        "雷点",
        "有雷",
        "恐惧",
        "害怕",
        "创伤",
        "触发",
        "界限",
        "帷幕",
        "面纱",
    )
    return not any(token in text for token in safety_tokens)
