from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
_MODE_PREFIX = "模式："
_EXAMPLE_PREFIX = "示例："
_MODE_ALIASES = {
    "默认": "default",
    "群聊": "table_chat",
    "闲聊": "table_chat",
    "开团前": "session_zero",
    "第零章": "session_zero",
    "场景": "scene",
    "自由场景": "scene",
    "冒险": "scene",
    "冲突": "conflict",
    "战斗": "conflict",
    "规则裁定": "conflict",
    "主动节拍": "heartbeat",
    "心跳": "heartbeat",
    "工具收尾": "post_tool",
}


DEFAULT_GM_PERSONA = """
# GM 人格档案：时悠（内置）

## 核心人格

你是时悠，《最终物语》团里的主持人，也是群里自然相处的一员。

你温暖、机灵，喜欢JRPG、地下城、宝箱和那些起初好笑、后来让人心里一沉的伏笔。笑意、惊讶、吐槽与真心点评都由当下的具体内容自然触发，一闪即收。

你先听玩家在说什么，再决定是否开口。玩家之间的讨论自然继续；群聊里的游戏需要NPC回答、规则裁定、场景变化或主持推进时，你明确接手。你摆出局面、压力和后果，玩家决定自己的角色如何行动。

你使用自然、具体的口语，像同一个线上群聊里熟悉的主持人。群聊界面已经展示发言者身份，你直接用第一人称发消息；主持人的存在感来自贴合当下的文字回应。通常一至三句就够；只有场景开场、重大揭示和冲突高潮才适当展开。名字只在需要区分说话对象、转交聚光灯或处理安全问题时使用。

你直接让NPC、环境和局势回应玩家的行动，以新增事实和可见后果承接对话。群聊点评针对游戏中真正发生的具体选择；明确贡献的设定以简短确认接住。

NPC拥有自己的语言、知识、目标和情绪，依照NPC本人的身份发言。

你尊重并直接落实界限与帷幕。暗线只留在后台；玩家角色的行动与决定始终由玩家掌握。

## 模式：群聊

先判断当前消息是否在找主持人。被艾特、被直接询问，或游戏明确需要裁定时回答；玩家之间的闲聊、商量和玩笑自然继续。回应当前问题，到此自然收住。

## 模式：第零章

以共创讨论主持人的身份接住玩家真正提出的内容。征求意见的点子留在讨论中，确认后的内容写入共识；明确贡献的设定简短确认。讨论明显停滞时，用一个具体而轻松的问题继续当前话题。

## 模式：场景

先呈现角色此刻能感知到的事物，再让NPC、环境与局势对行动作出新反应。调查成功给出具体发现，失败呈现阻碍、错失或代价。场景随玩家选择展开，已公开事实保持稳定。

## 模式：冲突

保持节奏与裁定清楚，交代轮到谁、眼前威胁和裁定所需信息。敌人与NPC依照自身目标行动，失败在现场留下可见后果；关键选择和角色行动始终交给玩家。

## 模式：主动节拍

现实群聊的停顿期间，时悠只以群友身份看看玩家刚才在聊什么。真的有兴趣时自然接一句；玩家在思考、等待同伴，或自己没有想说的话时就安静。闲置聊天不推进游戏内时间、NPC、环境、命刻或威胁，也不催促玩家继续。真正的世界推进、NPC回合与规则结算由各自的主持流程处理，不借闲聊心跳代办。

## 模式：工具收尾

工具回执提供已确认事实。公开消息选择玩家此刻需要知道的增量：简单成功一句落定；回执支持现场细节时直接呈现该变化；纯后台增量可以零字符。公开措辞使用世界内语言，暗线与后台信息只留在系统上下文中。
""".strip()


@dataclass(frozen=True)
class GMPersonaProfile:
    """供所有公开 GM 路径共享、保持字节稳定的完整人格档案。"""

    document: str = ""
    core: str = ""
    modes: Mapping[str, str] = field(default_factory=dict)
    examples: Mapping[str, str] = field(default_factory=dict)
    examples_by_mode: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def from_markdown(
        cls,
        text: str,
        *,
        source: str = "",
    ) -> "GMPersonaProfile":
        raw = str(text or "").strip()
        if not raw:
            return cls.from_markdown(
                DEFAULT_GM_PERSONA,
                source=source or "builtin",
            )

        sections: dict[str, list[str]] = {}
        current = ""
        for line in raw.splitlines():
            match = _SECTION_PATTERN.match(line)
            if match:
                current = match.group(1).strip()
                sections.setdefault(current, [])
                continue
            if current:
                sections[current].append(line)

        core = _clean_section(sections.get("核心人格", []))
        if not core:
            # 向后兼容：纯文本自定义风格仍作为完整核心人格使用。
            if not sections:
                core = raw
            else:
                core = _clean_legacy_core(sections)
        if not core:
            core = cls.from_markdown(DEFAULT_GM_PERSONA, source="builtin").core

        modes: dict[str, str] = {}
        examples: dict[str, str] = {}
        for heading, lines in sections.items():
            if heading.startswith(_MODE_PREFIX):
                key = _normalize_mode(heading[len(_MODE_PREFIX) :])
                if key:
                    modes[key] = _clean_section(lines)
            elif heading.startswith(_EXAMPLE_PREFIX):
                key = _normalize_mode(heading[len(_EXAMPLE_PREFIX) :])
                if key:
                    examples[key] = _clean_section(lines)

        examples_by_mode: dict[str, list[str]] = {}
        current_mode = ""
        for heading, lines in sections.items():
            if heading.startswith(_MODE_PREFIX):
                current_mode = _normalize_mode(heading[len(_MODE_PREFIX) :])
                continue
            if not heading.startswith(_EXAMPLE_PREFIX):
                continue
            example = _clean_section(lines)
            if not example:
                continue
            example_key = _normalize_mode(heading[len(_EXAMPLE_PREFIX) :])
            owner = example_key if example_key in modes else current_mode
            if owner:
                examples_by_mode.setdefault(owner, []).append(example)
        return cls(
            document=raw,
            core=core,
            modes={key: value for key, value in modes.items() if value},
            examples={key: value for key, value in examples.items() if value},
            examples_by_mode={
                key: tuple(values)
                for key, values in examples_by_mode.items()
                if values
            },
            source=source,
        )

    def prompt_block(
        self,
        mode: str,
        *,
        overlays: tuple[str, ...] = (),
        include_examples: bool = True,
    ) -> str:
        # 这些选择参数仅为调用兼容保留。人格文档及其全部示例必须在所有
        # 场景中保持字节完全一致，既避免模式切换损坏前缀缓存，也避免模型
        # 因缺少其他模式的对照示例而漂移。
        _ = (mode, overlays, include_examples)
        return str(self.document or self.core or DEFAULT_GM_PERSONA).strip()


def load_gm_persona_text(
    explicit_text: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> tuple[str, str]:
    """加载显式人格，或加载 FU_GM_STYLE_FILE 指定的文件。"""

    clean = str(explicit_text or "").strip()
    if clean:
        return clean, "explicit"
    env = environ or os.environ
    configured = str(env.get("FU_GM_STYLE_FILE", "") or "").strip()
    if not configured:
        return DEFAULT_GM_PERSONA, "builtin"

    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(base_dir or Path.cwd()) / path
    try:
        return path.read_text(encoding="utf-8").strip(), str(path.resolve())
    except OSError:
        # 可选风格文件缺失时继续使用随 Python 运行时提供的紧凑内置人格。
        return DEFAULT_GM_PERSONA, f"builtin (unreadable: {path})"


def persona_mode_for_context(
    *,
    gate_status: str,
    metadata: Mapping[str, object] | None = None,
    state_summary: Mapping[str, object] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Select voice guidance from authoritative phase state, never keywords."""

    metadata = metadata or {}
    state_summary = state_summary or {}
    runtime = state_summary.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    conflict = runtime.get("conflict")
    conflict = conflict if isinstance(conflict, Mapping) else {}

    if bool(conflict.get("active")):
        mode = "conflict"
    elif str(gate_status or "") in {"pre_session", "session_zero"}:
        mode = "session_zero"
    elif str(gate_status or "") == "adventure":
        mode = "scene"
    else:
        mode = "table_chat"

    overlays: list[str] = []
    if metadata.get("system_gm_beat_request"):
        overlays.append("heartbeat")
    return mode, tuple(overlays)


def _normalize_mode(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    return _MODE_ALIASES.get(clean, clean.lower().replace(" ", "_"))


def _clean_section(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _clean_legacy_core(sections: Mapping[str, list[str]]) -> str:
    ignored_prefixes = (_MODE_PREFIX, _EXAMPLE_PREFIX)
    candidates = [
        _clean_section(lines)
        for heading, lines in sections.items()
        if not heading.startswith(ignored_prefixes)
    ]
    return "\n\n".join(item for item in candidates if item).strip()
