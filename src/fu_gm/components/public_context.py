from __future__ import annotations

import re


RECENT_PUBLIC_CONTEXT_RULE = """
recent_public_context 按真实发生顺序排列，越靠后的消息越新。审查连续性时必须优先采用较新的明确事实：
较早的“尚未、是否、准备、可以选择”只描述当时状态；若后文明确记录行动已经完成、条件已经满足或选择已经作出，
不得再拿较早的待定状态否定后来的完成事实。反过来，只有提议而没有后续确认时，仍不得把它当成已经发生。
候选内容此前没有被提到，本身不构成矛盾；NPC、对立方或环境可以在自身权限内作出新的行动或揭示新信息。
只有候选与较新的公开事实、当前权威状态或明确权限边界不相容时才能拒绝。
""".strip()


def sanitize_recent_public_context(value: object, *, max_chars: int = 2200) -> str:
    """Keep table-visible chat while removing internal prompt envelopes."""

    text = str(value or "")
    text = re.sub(
        r"<scene_intent_contract\b[^>]*>.*?</scene_intent_contract>",
        "",
        text,
        flags=re.S | re.I,
    )
    for marker in (
        "【GM裁定一致性复核，后台指令，不得原样输出】",
        "【GM后台",
    ):
        if marker in text:
            text = text.split(marker, 1)[0]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    limit = max(200, int(max_chars))
    return "\n".join(lines)[-limit:]


def append_recent_public_context(
    base_context: object,
    recent_context: object,
    *,
    max_chars: int = 2200,
) -> str:
    """Append a chronological public transcript to a structured scene prompt."""

    base = str(base_context or "").strip()
    recent = sanitize_recent_public_context(recent_context, max_chars=max_chars)
    if not recent:
        return base
    section = (
        "近期公开消息（按发生顺序，越靠后越新；后发生的完成事实优先于先前待定状态）：\n"
        + recent
    )
    return "\n".join(part for part in (base, section) if part)
