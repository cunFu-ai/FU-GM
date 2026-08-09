from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import MutableMapping


FUGM_COMMANDS = frozenset(
    {
        "fugm",
        "fugm_beat",
        "fugm_chat",
        "fugm_s0",
        "fugm_safety",
        "fugm_end",
        "fugm_campaign",
        "fugm_campaigns",
        "fugm_save",
        "fugm_load",
        "fugm_delete_save",
        "fugm_delete_campaign",
        "fugm_away",
        "fugm_back",
        "fugm_status",
        "fugm_health",
        "fugm_heartbeat",
    }
)


def is_fugm_command_message(message: str) -> bool:
    """Recognize FU-GM commands even if AstrBot stripped the leading slash."""

    text = str(message or "").strip()
    if not text:
        return False
    command = text.split(maxsplit=1)[0].lstrip("/").lower()
    return command in FUGM_COMMANDS


def heartbeat_campaign_candidates(
    channel_campaigns: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Return persisted group bindings while excluding non-group transports.

    AstrBot WebChat sessions use ``webchat!`` origins and must never be treated
    as QQ-like channels that support unsolicited group delivery. Real group
    bindings remain eligible across plugin restarts.
    """

    candidates: list[tuple[str, str]] = []
    for channel_id, campaign_id in channel_campaigns.items():
        clean_channel = str(channel_id or "").strip()
        clean_campaign = str(campaign_id or "").strip()
        if not clean_channel or not clean_campaign:
            continue
        lowered = clean_channel.lower()
        if lowered.startswith(("webchat!", "private!", "private:")):
            continue
        candidates.append((clean_channel, clean_campaign))
    return candidates


@dataclass(frozen=True)
class CampaignBindingUpdate:
    campaign_id: str = ""
    channel_changed: bool = False
    user_changed: bool = False


@dataclass(frozen=True)
class CampaignBindingRemoval:
    channel_count: int = 0
    user_count: int = 0


def remove_deleted_campaign_bindings(
    deleted_campaign_id: str,
    *,
    channel_campaigns: MutableMapping[str, str],
    user_campaigns: MutableMapping[str, str],
) -> CampaignBindingRemoval:
    """Remove every local binding that targets a backend-deleted campaign."""

    clean_campaign = str(deleted_campaign_id or "").strip()
    if not clean_campaign:
        return CampaignBindingRemoval()
    channel_keys = [
        key
        for key, value in channel_campaigns.items()
        if str(value or "").strip() == clean_campaign
    ]
    user_keys = [
        key
        for key, value in user_campaigns.items()
        if str(value or "").strip() == clean_campaign
    ]
    for key in channel_keys:
        channel_campaigns.pop(key, None)
    for key in user_keys:
        user_campaigns.pop(key, None)
    return CampaignBindingRemoval(
        channel_count=len(channel_keys),
        user_count=len(user_keys),
    )


def bind_known_channel_members(
    *,
    channel_id: str,
    campaign_id: str,
    channel_members: Mapping[str, Sequence[str]],
    user_campaigns: MutableMapping[str, str],
) -> bool:
    """Move every previously observed member with a confirmed group switch."""

    clean_channel = str(channel_id or "").strip()
    clean_campaign = str(campaign_id or "").strip()
    if not clean_channel or not clean_campaign:
        return False
    changed = False
    for raw_user_key in channel_members.get(clean_channel, ()):
        user_key = str(raw_user_key or "").strip()
        if user_key and user_campaigns.get(user_key) != clean_campaign:
            user_campaigns[user_key] = clean_campaign
            changed = True
    return changed


def apply_confirmed_campaign_binding(
    response: dict,
    *,
    is_private: bool,
    channel_id: str,
    user_key: str,
    confirmed_user_key: str = "",
    channel_campaigns: MutableMapping[str, str],
    user_campaigns: MutableMapping[str, str],
) -> CampaignBindingUpdate:
    """Apply only a backend-confirmed campaign switch.

    The bridge never infers a campaign from chat text. A successful FU-GM
    response is the authority for both the group and the speaking user's next
    request.
    """

    if response.get("ok") is False:
        return CampaignBindingUpdate()
    campaign_id = str(response.get("active_campaign_id") or "").strip()
    if not campaign_id:
        return CampaignBindingUpdate()

    channel_changed = False
    if (
        not is_private
        and channel_id
        and channel_campaigns.get(channel_id) != campaign_id
    ):
        channel_campaigns[channel_id] = campaign_id
        channel_changed = True

    binding_user_key = str(confirmed_user_key or user_key or "").strip()
    user_changed = False
    if binding_user_key and user_campaigns.get(binding_user_key) != campaign_id:
        user_campaigns[binding_user_key] = campaign_id
        user_changed = True

    return CampaignBindingUpdate(
        campaign_id=campaign_id,
        channel_changed=channel_changed,
        user_changed=user_changed,
    )
