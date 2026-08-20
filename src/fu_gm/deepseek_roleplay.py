from __future__ import annotations

import re


INNER_OS_MARKER = (
    "\n\n【角色沉浸要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
    "1. 请以角色第一人称进行内心独白，用括号包裹内心活动，例如\"（心想：……）\"或\"(内心OS：……)\"\n"
    "2. 用第一人称描写角色的内心感受，例如\"我心想\"\"我觉得\"\"我暗自\"等\n"
    "3. 思考内容应沉浸在角色中，通过内心独白分析剧情和规划回复"
)


NO_INNER_OS_MARKER = (
    "\n\n【思维模式要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
    "1. 禁止使用圆括号包裹内心独白，例如\"（心想：……）\"或\"(内心OS：……)\"，所有分析内容直接陈述即可\n"
    "2. 禁止以角色第一人称描写内心活动，例如\"我心想\"\"我觉得\"\"我暗自\"等，请用分析性语言替代\n"
    "3. 思考内容应聚焦于剧情走向分析和回复内容规划，不要在思考中进行角色扮演式的内心戏表演"
)


def normalize_deepseek_roleplay_mode(value: object) -> str:
    """Return the supported DeepSeek reasoning style.

    Role immersion is only an authorization value here. Callers must still
    opt an individual creative request into thinking; rules and tool receipts
    can therefore share one client without inheriting the immersive style.
    """

    normalized = str(value or "default").strip().lower().replace("-", "_")
    if normalized in {"inner_os", "immersive", "role_immersion"}:
        return "inner_os"
    if normalized in {"no_inner_os", "analysis", "pure_analysis"}:
        return "no_inner_os"
    return "default"


def apply_deepseek_reasoning_style(
    user_content: str,
    *,
    model: str,
    mode: object,
    thinking_enabled: bool,
) -> str:
    """Append an optional thinking-only marker without changing cache prefix."""

    content = str(user_content or "")
    normalized_model = str(model or "").strip().lower()
    if not (normalized_model.startswith("deepseek-v4") and bool(thinking_enabled)):
        return content
    normalized_mode = normalize_deepseek_roleplay_mode(mode)
    if normalized_mode == "inner_os":
        return content + INNER_OS_MARKER
    if normalized_mode == "no_inner_os":
        return content + NO_INNER_OS_MARKER
    return content


def strip_deepseek_reasoning_leakage(text: str) -> str:
    """Remove unmistakable reasoning wrappers from player-facing text."""

    clean = re.sub(r"<think>[\s\S]*?</think>", "", str(text or ""), flags=re.I)
    clean = re.sub(
        r"[（(]\s*(?:心想|内心\s*OS|内心独白|我心想|我觉得|我暗自)\s*[：:]?[\s\S]*?[）)]",
        "",
        clean,
        flags=re.I,
    )
    return clean.strip()


__all__ = [
    "INNER_OS_MARKER",
    "NO_INNER_OS_MARKER",
    "apply_deepseek_reasoning_style",
    "normalize_deepseek_roleplay_mode",
    "strip_deepseek_reasoning_leakage",
]
