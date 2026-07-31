from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import MutableMapping


@dataclass(frozen=True)
class CampaignBindingUpdate:
    campaign_id: str = ""
    channel_changed: bool = False
    user_changed: bool = False


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
