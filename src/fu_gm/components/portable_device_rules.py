from __future__ import annotations


PORTABLE_DEVICE_TYPES = ("炼金装置", "注魔装置", "魔导装置")
PORTABLE_DEVICE_TIER_NAMES = ("基础", "进阶", "顶级")

_DEVICE_ALIASES = {
    "炼金": "炼金装置",
    "炼金术": "炼金装置",
    "炼金装置": "炼金装置",
    "alchemy": "炼金装置",
    "注魔": "注魔装置",
    "注魔术": "注魔装置",
    "注魔装置": "注魔装置",
    "灌注": "注魔装置",
    "灌注术": "注魔装置",
    "infusion": "注魔装置",
    "魔导": "魔导装置",
    "魔导装置": "魔导装置",
    "魔科技": "魔导装置",
    "magitech": "魔导装置",
    "magictech": "魔导装置",
}


def normalize_portable_device_name(raw_name: str) -> str:
    clean = str(raw_name or "").strip()
    canonical = _DEVICE_ALIASES.get(clean) or _DEVICE_ALIASES.get(clean.lower())
    if canonical is None:
        raise ValueError(
            f"未知便携装置类型：【{clean}】；请选择炼金装置、注魔装置或魔导装置。"
        )
    return canonical


def validate_portable_device_choices(
    rank: int,
    choices: list[str] | tuple[str, ...] | None,
    *,
    require_complete: bool,
) -> list[str]:
    """Validate one device allocation for every Portable Benefits rank.

    Each occurrence unlocks the basic benefit for a new device type, or raises
    an already unlocked device from basic to advanced and then supreme.  The
    ordered list therefore preserves every level-up decision without adding a
    gadget-specific field to the character model.
    """

    expected = max(0, int(rank or 0))
    normalized = [normalize_portable_device_name(item) for item in (choices or [])]
    if expected == 0 and normalized:
        raise ValueError("未取得【便携装置】技能，不能选择装置类型。")
    if len(normalized) > expected:
        raise ValueError(
            f"【便携装置】只有 {expected} 级，但记录了 {len(normalized)} 次装置选择。"
        )
    if require_complete and len(normalized) != expected:
        missing = expected - len(normalized)
        raise ValueError(f"【便携装置】还需要选择 {missing} 次装置类型或升级。")

    counts: dict[str, int] = {}
    for device in normalized:
        counts[device] = counts.get(device, 0) + 1
        if counts[device] > len(PORTABLE_DEVICE_TIER_NAMES):
            raise ValueError(f"【{device}】已经达到顶级，不能再次升级。")
    return normalized


def portable_device_tiers(choices: list[str] | tuple[str, ...] | None) -> dict[str, int]:
    tiers: dict[str, int] = {}
    for raw_choice in choices or []:
        device = normalize_portable_device_name(raw_choice)
        tiers[device] = min(len(PORTABLE_DEVICE_TIER_NAMES), tiers.get(device, 0) + 1)
    return tiers


def portable_device_tier_label(tier: int) -> str:
    if tier <= 0:
        return "未解锁"
    return PORTABLE_DEVICE_TIER_NAMES[min(tier, len(PORTABLE_DEVICE_TIER_NAMES)) - 1]
