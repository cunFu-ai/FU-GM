from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def load_campaign_binding_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "campaign_binding.py"
    )
    spec = importlib.util.spec_from_file_location("fu_gm_bridge_campaign_binding", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


campaign_binding = load_campaign_binding_module()
apply_confirmed_campaign_binding = campaign_binding.apply_confirmed_campaign_binding
bind_known_channel_members = campaign_binding.bind_known_channel_members


def test_successful_load_response_switches_group_and_user_binding() -> None:
    channels = {"200000001": "1"}
    users = {"100000001": "1"}

    update = apply_confirmed_campaign_binding(
        {"ok": True, "active_campaign_id": "default"},
        is_private=False,
        channel_id="200000001",
        user_key="100000001",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert update.campaign_id == "default"
    assert update.channel_changed
    assert update.user_changed
    assert channels["200000001"] == "default"
    assert users["100000001"] == "default"


def test_failed_or_read_only_response_does_not_change_binding() -> None:
    channels = {"200000001": "1"}
    users = {"100000001": "1"}

    failed = apply_confirmed_campaign_binding(
        {"ok": False, "active_campaign_id": "default"},
        is_private=False,
        channel_id="200000001",
        user_key="100000001",
        channel_campaigns=channels,
        user_campaigns=users,
    )
    missing = apply_confirmed_campaign_binding(
        {"ok": True},
        is_private=False,
        channel_id="200000001",
        user_key="100000001",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert not failed.campaign_id
    assert not missing.campaign_id
    assert channels["200000001"] == "1"
    assert users["100000001"] == "1"


def test_private_switch_updates_only_the_user_binding() -> None:
    channels = {"200000001": "1"}
    users = {"100000001": "1"}

    update = apply_confirmed_campaign_binding(
        {"ok": True, "active_campaign_id": "default"},
        is_private=True,
        channel_id="private-session",
        user_key="100000001",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert not update.channel_changed
    assert update.user_changed
    assert channels == {"200000001": "1"}
    assert users["100000001"] == "default"


def test_batched_switch_updates_the_actual_initiator_not_first_handler() -> None:
    channels = {"200000001": "1"}
    users = {"first-handler": "1", "switch-initiator": "1"}

    update = apply_confirmed_campaign_binding(
        {
            "ok": True,
            "active_campaign_id": "default",
            "active_campaign_speaker_id": "switch-initiator",
        },
        is_private=False,
        channel_id="200000001",
        user_key="first-handler",
        confirmed_user_key="switch-initiator",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert update.channel_changed
    assert update.user_changed
    assert channels["200000001"] == "default"
    assert users["switch-initiator"] == "default"
    assert users["first-handler"] == "1"


def test_confirmed_group_switch_updates_every_previously_seen_member() -> None:
    members = {
        "200000001": ["100000001", "100000002"],
    }
    users = {
        "100000001": "旧团",
        "100000002": "旧团",
        "other-group-user": "别团",
    }

    changed = bind_known_channel_members(
        channel_id="200000001",
        campaign_id="新团",
        channel_members=members,
        user_campaigns=users,
    )

    assert changed
    assert users["100000001"] == "新团"
    assert users["100000002"] == "新团"
    assert users["other-group-user"] == "别团"
