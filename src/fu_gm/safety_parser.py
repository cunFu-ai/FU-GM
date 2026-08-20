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
    cleaned = re.sub(r"^(?:请|帮我|给我)?\s*(?:把|将)\s*", "", cleaned)
    cleaned = re.sub(
        r"^(?:不要|不出现|不详细描写|不明确描写|不正面描写|别|禁止)\s*(?:出现|有|包含|涉及|提到|描写|描述)?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"^(?:出现|有|包含|涉及|提到|看到|看见|任何|所有|关于|这类|这种|这样的|相关的)\s*", "", cleaned)
    cleaned = re.sub(
        r"(?:这种内容|这类内容|相关内容|这部分|这些|这种|这类|能不能|可不可以|可以吗|好不好|行不行|请|吧|了)$",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:都)?\s*(?:采用|用|以)?\s*(?:淡出处理|淡出|带过|一笔带过|黑屏处理|拉灯处理|幕后处理|放到幕后|放在幕后|放到帷幕后|放在帷幕后|不要细写|不要详细描写|不作详细描写|不做详细描写|不详细描写|不明确描写)$",
        "",
        cleaned,
    )
    # Natural phrasing such as “X 我希望少一点或者淡出” is split before
    # “淡出” by the veil matcher. Do not preserve the unfinished connector as
    # part of the safety topic shown in summaries and dashboards.
    cleaned = re.sub(r"(?:我)?希望(?:少一点|少些|减少一些|减少)?(?:或者|或)?$", "", cleaned)
    return cleaned.strip().strip("。；; ，,")


def _explicit_declarations(message: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    pattern = re.compile(r"(界限|帷幕|面纱)\s*[:：]\s*(.+?)(?=(?:界限|帷幕|面纱)\s*[:：]|$)")
    for match in pattern.finditer(message):
        label = match.group(1)
        # An explicit declaration owns its sentence, not every later Session 0
        # topic in the same chat message.  Without this boundary, a following
        # tone or campaign-pacing sentence is stored as part of the veil.
        raw_value = re.split(r"[。！？\n]", match.group(2), maxsplit=1)[0]
        # “界限：A；B 放在帷幕后”包含两种处理方式。显式标签只管
        # 它后面的第一段，其余分句交给自然语言解析，避免把 A+B 整段
        # 先记成界限、随后又把 B 记成帷幕。
        if label == "界限" and re.search(r"[；;].*(?:帷幕|面纱|幕后|淡出|带过)", raw_value):
            raw_value = re.split(r"[；;]", raw_value, maxsplit=1)[0]
        for raw_item in re.split(r"[，,]", raw_value):
            item = str(raw_item or "").strip()
            if label in {"帷幕", "面纱"}:
                item = re.split(r"(?:只作为|仅作为)(?:远景)?背景", item, maxsplit=1)[0]
                item = re.sub(r"(?:不要|不必)(?:详细|正面)?描写(?:其)?过程$", "", item)
            item = clean_safety_item(item)
            if item:
                declarations.append((label, item))
    prefix_pattern = re.compile(
        r"(?:加(?:个|一条)?|补(?:个|一条)?|新增|设置|设定|记录|记下|声明)\s*"
        r"(?:一个|一条)?\s*(界限|帷幕|面纱)\s*(?:[:：，,为是叫])?\s*"
        r"(?P<item>[^，,。！？；;\n]+)"
    )
    for match in prefix_pattern.finditer(message):
        item = clean_safety_item(match.group("item"))
        if item:
            declarations.append((match.group(1), item))
    suffix_pattern = re.compile(
        r"(?P<item>[^，,。！？；;\n]+?)\s*"
        r"(?:(?<!不)是|(?<!不)作为|(?<!不)算作|(?<!不)算|设为|设成|当作|归为|记为|记录为|列为)\s*"
        r"(?:我的|我们的)?\s*(界限|帷幕|面纱)"
    )
    for match in suffix_pattern.finditer(message):
        item = clean_safety_item(match.group("item"))
        if item:
            declarations.append((match.group(2), item))
    return declarations


def _natural_declarations(message: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    clauses = [clause.strip() for clause in re.split(r"[。！？；;，,\n]", message) if clause.strip()]
    for clause in clauses:
        if re.search(r"(?:界限|帷幕|面纱)\s*[:：]", clause):
            continue
        # “X 请淡出处理，不要细讲”中的后一分句只是延续前一条
        # 帷幕的处理方式，不是名为“细讲”的新界限。只有处理动词而
        # 没有安全主题的独立分句必须忽略。
        if _looks_like_treatment_only_clause(clause):
            continue
        veil_item = _extract_veil_item(clause)
        if veil_item:
            declarations.append(("veil", veil_item))
            continue
        line_item = _extract_line_item(clause)
        if line_item:
            declarations.append(("line", line_item))
    return declarations


def _looks_like_treatment_only_clause(clause: str) -> bool:
    """Recognize a continuation that only describes veil treatment."""

    text = re.sub(r"\s+", "", str(clause or ""))
    return bool(
        re.fullmatch(
            r"(?:请)?(?:不要|不必|别)(?:再)?(?:详细|正面|明确|直接|过度)?"
            r"(?:细讲|细说|细写|展开|聚焦|描写|描述)(?:了|吧)?",
            text,
        )
    )


def _extract_line_item(clause: str) -> str:
    # “要不要先调查宝箱？”是桌面讨论，不是“不要出现 X”的安全声明。
    if re.search(r"(?:要\s*不要|愿不愿意)", clause):
        return ""
    if _looks_like_table_coordination(clause):
        return ""
    if _looks_like_non_safety_preference(clause):
        return ""
    patterns = [
        r"(?:我|我们)?\s*(?:不希望|不想|不愿意|不接受|不能接受|接受不了|受不了|希望不要|请不要|不要|别(?!人)|禁止)\s*(?:在游戏中|在游戏里|游戏中|游戏里|故事里|剧情里)?\s*(?:出现|有|包含|涉及|提到)?\s*(?P<item>[^，,。！？；;\n]+)",
        r"(?P<item>[^，,。！？；;\n]+?)\s*(?:我|我们)?\s*(?:不接受|不能接受|接受不了|受不了|不舒服|很不舒服|会不适|有雷|是雷点)",
        r"(?P<item>[^，,。！？；;\n]+?)\s*(?:不要出现|不能出现|别出现|禁止出现|不要有|别有|接受不了|受不了)",
        r"(?:我|我们)?\s*(?:对)?\s*(?P<item>[^，,。！？；;\n]+?)\s*(?:不舒服|很不舒服|会不适|有雷|是雷点)",
    ]
    return _first_match_item(patterns, clause)


def _extract_veil_item(clause: str) -> str:
    if _looks_like_table_coordination(clause):
        return ""
    if _looks_like_non_safety_preference(clause):
        return ""
    background_match = re.search(
        r"(?P<item>[^，,。！？；;\n]+?)(?:只作为|仅作为)(?:远景)?背景(?:处理)?(?:，|,)?(?:不要|不必)(?:详细|正面)?描写(?:其)?过程",
        clause,
    )
    if background_match:
        return clean_safety_item(background_match.group("item"))
    patterns = [
        r"(?:我|我们)?\s*(?:不希望|不想|不愿意|希望不要|请不要|不要|别(?!人))\s*(?:明确|直接|详细|细致|正面|过度)?\s*(?:描写|描述|展开|聚焦|细写|细说)\s*(?P<item>[^，,。！？；;\n]+)",
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
    if "雷点" in text and any(token in text for token in ("没有", "没什么", "暂无", "暂时没", "不强", "不算强")):
        return True
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
        "画面",
        "演出",
        "风格",
        "玩法",
        "类型",
        "偏好",
        "Nortantis",
        "基调",
        "氛围",
        "开场",
        "开局",
        "第一章",
        "一上来",
        "拯救世界",
        "世界危机",
        "最终决战",
        "风格",
        "故事",
        "剧情",
        "全程",
        "压抑",
        "黑暗",
        "沉重",
        "严肃",
        "轻松",
        "明亮",
        "王道",
        "冒险感",
        "浮夸",
        "飘",
        "燃",
        "爽点",
        "开无双",
        "无双",
        "难度",
        "战斗强度",
        "挑战性",
        "拆台",
        "队伍",
        "队内",
        "分歧",
        "争论",
        "合作",
        "配合",
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


def _looks_like_table_coordination(clause: str) -> bool:
    """Avoid recording casual pacing/cooperation phrases as safety lines.

    Session 0 intentionally accepts loose safety phrasing, but table talk like
    “别急” or “先别抢行动” is about coordination, not lines and veils.
    """

    text = re.sub(r"\s+", "", str(clause or ""))
    if not text:
        return False
    if any(token in text for token in ("没想法", "没有想法", "没有别的想法", "暂时没想法", "暂时没有想法")):
        return True
    if any(token in text for token in ("互相拆台", "队内拆台", "玩家拆台", "别抢戏", "不要抢戏")):
        return True
    if any(token in text for token in ("界限", "帷幕", "面纱", "雷点", "不舒服", "接受不了")):
        return False
    safety_subject_tokens = (
        "酷刑",
        "性暴力",
        "血腥",
        "病变",
        "亲密",
        "儿童",
        "虐待",
        "自残",
        "自杀",
        "歧视",
        "仇恨",
        "恐怖",
        "蜘蛛",
        "虫",
        "身体",
        "疾病",
        "创伤",
    )
    flow_control_tokens = (
        "开第一章",
        "进入第一章",
        "开序章",
        "进入序章",
        "开场",
        "开团",
        "跑团",
        "继续",
        "推进",
        "催",
        "点名",
        "回复",
        "说话",
        "插话",
        "抢话",
        "抢行动",
    )
    if any(token in text for token in flow_control_tokens) and not any(
        token in text for token in safety_subject_tokens
    ):
        return True
    coordination_patterns = (
        r"^(?:大家|我们|先|都)?(?:别|不要|不必)(?:太)?(?:急|着急|急着|慌|抢|催|打断|插队)$",
        r"^(?:大家|我们)?(?:先)?(?:慢慢来|别急|不急|等一下|等下|稍等|先等等)$",
        r"^(?:先)?别急着.+$",
        r"^不要急着.+$",
    )
    return any(re.search(pattern, text) for pattern in coordination_patterns)
