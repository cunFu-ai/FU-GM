from __future__ import annotations

import re


def _strip_speaker_prefix(text: str) -> str:
    for separator in ("：", ":"):
        if separator in text:
            return text.split(separator, 1)[1].strip()
    return text.strip()


def infer_scene_target_from_text(chat: str, default: str = "当前线索") -> str:
    """Extract the public object a player is examining from natural language."""

    bracket = re.search(r"[【\[](?P<name>[^】\]]+)[】\]]", chat)
    if bracket:
        candidate = bracket.group("name").strip()
        if candidate and "+" not in candidate and candidate.upper() not in {"DEX", "INS", "MIG", "WLP"}:
            return candidate

    explicit_focus = re.search(
        r"(?:调查|观察|检查|盯住|看向|看看|确认)"
        r"(?:一下|一眼)?(?P<target>[^，,。；;\n]{1,24}?)"
        r"(?=是否|是不是|会不会|有没有|看起来|更像|的反应|到底|怎么|为何|为什么)",
        chat,
    )
    if explicit_focus:
        candidate = explicit_focus.group("target").strip(" 的着了并来以把将")
        candidate = re.sub(r"^(?:她|他|我|我们|你们|那个|这个|那处|这处)", "", candidate).strip()
        if candidate.startswith(("失忆旅人", "失名旅人", "旅人")):
            return "失忆旅人"
        if candidate and candidate not in {"大家", "现场", "情况"}:
            return candidate

    if "谁" in chat and any(token in chat for token in ("反应过度", "反应异常", "异样反应", "神色不对")):
        return "听证厅中的异常反应"
    if "正午大钟" in chat and "回应" in chat and any(token in chat for token in ("旅人", "名字", "姓名")):
        return "正午大钟对旅人名字的回应"
    if "潮生藤" in chat:
        return "潮生藤"
    if "旅人" in chat and any(token in chat for token in ("失忆", "灰晶", "记忆", "呼吸", "钟声")):
        return "失忆旅人"

    # Preserve the evidence as the target. Otherwise the generic verb parser
    # stops at the first possessive in ``检查旧路闸门旁的车辙``.
    if "车辙" in chat:
        if any(token in chat for token in ("财团", "巡逻队", "追兵", "抵达", "多久", "泥痕")):
            return "财团车辙"
        return "车辙"

    if "旧路入口" in chat:
        if any(token in chat for token in ("机关", "锁扣", "水痕", "钟塔", "遗迹")):
            return "旧路入口机关"
        if any(token in chat for token in ("退路", "潮水", "封死")):
            return "旧路入口与退路"
        return "旧路入口"
    if "钟塔遗迹" in chat and any(token in chat for token in ("入口", "机关", "旧路", "锁扣")):
        return "钟塔遗迹入口机关"
    if "锁扣" in chat and "水痕" in chat:
        return "锁扣与水痕"
    if "退路" in chat and any(token in chat for token in ("潮水", "封死", "撤离")):
        return "潮水退路"
    if "遮挡" in chat and any(token in chat for token in ("通路", "退路", "入口", "缺口")):
        return "现场遮挡与通路"

    verb_pattern = (
        r"(?:调查|观察|检查|确认|判断|分析|研究|拆解|破解|修复|稳定|寻找|找出|看看|看清|盯住|留意)"
        r"(?P<target>[^，,。；;\n]{2,36})"
    )
    for match in re.finditer(verb_pattern, chat):
        candidate = match.group("target").strip(" 的着了并来以把将，,。；;")
        candidate = re.sub(r"^(?:一下|一眼|一圈|一遍|那个|这个|那处|这处)", "", candidate).strip()
        candidate = re.split(
            r"(?:是不是|会不会|是否|能否|有没有|可不可以|到底|是什么|的|并|来|以|把|将|想|试图|尝试|多久|哪里)",
            candidate,
            maxsplit=1,
        )[0]
        candidate = candidate.strip(" 的着了，,。；;")
        if (
            candidate
            and len(candidate) >= 2
            and not candidate.startswith(("不是", "有人", "没人", "会不会"))
            and candidate not in {"大家", "队伍", "敌人", "现场", "情况", "目标", "当前目标", "当前线索"}
        ):
            return candidate

    priority_targets = [
        "失忆旅人",
        "旅人",
        "守望会会长",
        "白花守望会",
        "财团车辙",
        "潮生藤",
        "风铃廊",
        "风铃",
        "旧钟",
        "潮汐下的钟塔遗迹入口",
        "钟塔遗迹入口",
        "车辙",
        "锁扣",
        "水痕",
        "退路",
    ]
    for target in priority_targets:
        if target in chat:
            return target
    return default


def looks_like_environment_threat_watch(chat: str) -> bool:
    text = str(chat or "")
    watch_markers = ("巡夜", "警戒", "望风", "留意", "确认有没有", "看看有没有", "观察周边", "查看周边", "侦察周边")
    clue_markers = ("火光", "暗号", "动静", "烟", "灯", "哨声", "脚印", "踪迹")
    countdown_markers = ("多久", "何时", "几时", "抵达", "快到", "多远", "距离", "时间", "路线", "封锁线")
    return (
        any(marker in text for marker in watch_markers)
        and any(marker in text for marker in clue_markers)
        and not any(marker in text for marker in countdown_markers)
    )


def infer_imminent_threat_target_from_text(chat: str) -> str:
    """Prefer the countdown evidence when a player investigates pressure."""

    if looks_like_environment_threat_watch(chat):
        return "周边环境"
    if "旧路入口" in chat and any(token in chat for token in ("机关", "锁扣", "水痕", "钟塔", "遗迹")):
        return infer_scene_target_from_text(chat)
    if "钟塔遗迹" in chat and any(token in chat for token in ("入口", "机关", "旧路", "锁扣")):
        return infer_scene_target_from_text(chat)
    priority_targets = [
        "财团车辙",
        "巡逻队脚步",
        "追兵踪迹",
        "警报源头",
        "潮水退路",
        "车辙",
        "脚步",
        "警报",
        "潮水",
    ]
    for target in priority_targets:
        if target in chat:
            if target == "车辙" and "财团" in chat:
                return "财团车辙"
            if target == "脚步" and "巡逻" in chat:
                return "巡逻队脚步"
            return target
    return infer_scene_target_from_text(chat)


def should_establish_threat_clock_from_investigation(chat: str) -> bool:
    if looks_like_environment_threat_watch(chat):
        return False
    text = str(chat or "")
    countdown_markers = ("多久", "何时", "几时", "抵达", "快到", "多远", "距离", "时间", "路线", "封锁线")
    concrete_pressure_markers = ("车辙", "脚步", "警报源头", "警报", "潮水", "涨潮", "没顶")
    return any(marker in text for marker in countdown_markers) or any(marker in text for marker in concrete_pressure_markers)


def should_override_scene_investigation_target(chat: str, current_target: str, inferred_target: str) -> bool:
    generic_targets = {
        "",
        "当前线索",
        "当前目标",
        "目标",
        "线索",
        "当前对象",
        "这两样",
        "那两样",
        "这两个",
        "那两个",
        "这些",
    }
    current = str(current_target or "").strip()
    inferred = str(inferred_target or "").strip()
    if not inferred or inferred in generic_targets:
        return False
    if current in generic_targets:
        return True
    if current.startswith(("不是", "是不是", "是否", "有没有", "有人", "没人", "会不会")):
        return True
    if current == inferred:
        return False
    text = _strip_speaker_prefix(str(chat or ""))
    investigation_focus = rf"(?:调查|观察|检查|确认|判断|分析|研究|寻找|找出|看看|看清|盯住|留意)[^，,。；;\n]{{0,18}}{re.escape(inferred)}"
    current_focus = rf"(?:调查|观察|检查|确认|判断|分析|研究|寻找|找出|看看|看清|盯住|留意)[^，,。；;\n]{{0,18}}{re.escape(current)}"
    if re.search(investigation_focus, text) and not re.search(current_focus, text):
        return True
    if inferred in text and current not in text:
        return True
    if inferred == "失忆旅人" and "旅人" in text and current not in {"旅人", "失忆旅人"}:
        traveler_focus = ("观察", "呼吸", "灰晶", "记忆", "钟声", "反应", "判断", "灵魂")
        return any(token in text for token in traveler_focus)
    if inferred in {
        "门口与柜台周边",
        "门槛盐痕与侧路拖痕",
        "守门人与门边木牌",
        "院门与守门人",
    }:
        return True
    if inferred in {"旧路入口机关", "旧路入口与退路", "钟塔遗迹入口机关", "锁扣与水痕", "潮水退路"} and current not in text:
        return True
    return False
