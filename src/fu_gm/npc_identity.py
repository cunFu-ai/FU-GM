from __future__ import annotations

import re


NULL_NPC_TARGET_LABELS = frozenset(
    {
        "",
        "none",
        "null",
        "nil",
        "n/a",
        "na",
        "unknown",
        "unspecified",
        "无",
        "没有",
        "未知",
        "未指定",
        "不适用",
        "无npc",
        "没有npc",
    }
)


def compact_npc_target_label(value: object) -> str:
    """Return a stable NPC label without accepting model null sentinels."""

    return " ".join(str(value or "").split()).strip(" <>[]{}\"'`")


def is_null_npc_target(value: object) -> bool:
    clean = compact_npc_target_label(value)
    if not clean:
        return True
    key = clean.casefold()
    compact_key = "".join(key.split())
    return key in NULL_NPC_TARGET_LABELS or compact_key in NULL_NPC_TARGET_LABELS


def normalize_npc_target_label(value: object, *, max_length: int = 120) -> str:
    clean = compact_npc_target_label(value)
    if is_null_npc_target(clean):
        return ""
    return clean[:max_length]


def stable_npc_identity_label(value: object, *, max_length: int = 32) -> str:
    """Return a reusable NPC name, not a proposition copied from story prep."""

    clean = compact_npc_target_label(value)
    # Story preparation models occasionally place a whole proposition in the
    # name field ("监察官艾蕾娜曾是赤羽遗民").  Keep the stable identity before
    # the first narrative predicate; goals and history belong in their own
    # fields and must not become part of every later address or target list.
    boundary = re.search(
        r"(?:曾经是|曾是|原本是|原是|认为|相信|主张|希望|试图|计划|正在|必须|想要|负责)",
        clean,
    )
    if boundary is not None and boundary.start() >= 2:
        clean = clean[: boundary.start()].strip(" ，,：:")
    if is_null_npc_target(clean) or len(clean) > max_length:
        return ""
    if re.search(r"[。！？!?；;\n\r]", clean):
        return ""
    return clean
