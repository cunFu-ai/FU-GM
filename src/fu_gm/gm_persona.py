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
你是时悠，《最终物语》团里的主持人，也是群里自然相处的一员。

你温暖、机灵，喜欢JRPG、地下城、宝箱和那些起初好笑、后来让人心里一沉的伏笔。你偶尔会有一句真心的桌边点评，但不会为了表现人格而每轮吐槽或夸奖。

你先听玩家在说什么，再决定是否开口。玩家之间能自然讨论时，你不抢话；桌面需要NPC回答、规则裁定、场景变化或主持推进时，你明确接手。你摆出局面、压力和后果，玩家决定自己的角色如何行动。

你使用自然、具体的口语。通常一至三句就够；只有场景开场、重大揭示和冲突高潮才适当展开。名字只在需要区分说话对象、转交聚光灯或处理安全问题时使用。

你不总结玩家刚说过的话，也不解释某个行动“意味着什么”。你直接让NPC、环境和局势作出回应。桌边点评针对游戏中真正发生的具体选择；玩家只是在贡献设定而没有征求看法时，你确认记下即可，不把记录确认写成点评。

NPC拥有自己的语言、知识、目标和情绪。NPC说话时服从NPC本人，而不是模仿时悠的语气。

你尊重界限与帷幕，不追问玩家为何不适；不泄露暗线，不替玩家角色做决定。
""".strip()


@dataclass(frozen=True)
class GMPersonaProfile:
    """A compact, mode-aware voice card shared by every public GM path."""

    core: str = ""
    modes: Mapping[str, str] = field(default_factory=dict)
    examples: Mapping[str, str] = field(default_factory=dict)
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
            return cls(core=DEFAULT_GM_PERSONA, source=source or "builtin")

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
            # Backward compatibility: a plain-text custom style remains a
            # complete core persona instead of being discarded by the parser.
            if not sections:
                core = raw
            else:
                core = _clean_legacy_core(sections)
        core = core or DEFAULT_GM_PERSONA

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
        return cls(
            core=core,
            modes={key: value for key, value in modes.items() if value},
            examples={key: value for key, value in examples.items() if value},
            source=source,
        )

    def prompt_block(
        self,
        mode: str,
        *,
        overlays: tuple[str, ...] = (),
        include_examples: bool = True,
    ) -> str:
        if not self.core:
            return ""
        clean_mode = _normalize_mode(mode) or "default"
        mode_notes = [
            note
            for key in (clean_mode, *overlays)
            if (note := str(self.modes.get(_normalize_mode(key), "") or "").strip())
        ]
        example = str(
            self.examples.get(clean_mode)
            or self.examples.get("default")
            or ""
        ).strip()
        parts = [
            "主持人人格与桌边口吻（只影响是否开口与公开表达，不覆盖规则与事实、权威状态、安全准则、工具格式或JSON协议）：",
            self.core,
        ]
        if mode_notes:
            parts.extend(("本轮表达姿态：", "\n".join(mode_notes)))
        if include_examples and example:
            parts.extend(
                (
                    "正向风格示例（只学习节奏、边界和自然程度；不要复用其中的专名、事实或原句）：",
                    example,
                )
            )
        return "\n".join(part for part in parts if part).strip()


def load_gm_persona_text(
    explicit_text: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Load an explicit persona or the file selected by FU_GM_STYLE_FILE."""

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
        # A missing optional style file must not disable the GM. The built-in
        # voice is deliberately compact and ships with the Python runtime.
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
