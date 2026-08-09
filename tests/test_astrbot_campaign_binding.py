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
heartbeat_campaign_candidates = campaign_binding.heartbeat_campaign_candidates
is_fugm_command_message = campaign_binding.is_fugm_command_message
remove_deleted_campaign_bindings = campaign_binding.remove_deleted_campaign_bindings


def test_fugm_command_detection_handles_stripped_slash() -> None:
    assert is_fugm_command_message("/fugm_campaign default")
    assert is_fugm_command_message("fugm_campaign default")
    assert is_fugm_command_message("  FUGM_STATUS  ")
    assert not is_fugm_command_message("请检查 fugm_campaign 的状态")
    assert not is_fugm_command_message("fugm_campaigns_extra")
    assert not is_fugm_command_message("")


def test_idle_heartbeat_excludes_webchat_and_explicit_private_origins() -> None:
    bindings = {
        "200000001": "default",
        "webchat!astrbot!synthetic-session": "default",
        "private:direct-session": "1",
    }

    assert heartbeat_campaign_candidates(bindings) == [("200000001", "default")]


def test_persisted_group_binding_remains_eligible_after_restart() -> None:
    bindings = {
        "200000001": "default",
        "webchat!astrbot!synthetic-session": "default",
    }

    assert heartbeat_campaign_candidates(bindings) == [("200000001", "default")]


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


def test_private_switch_never_creates_a_group_heartbeat_candidate() -> None:
    channels = {"200000001": "1"}
    users = {"100000001": "1"}

    apply_confirmed_campaign_binding(
        {"ok": True, "active_campaign_id": "default"},
        is_private=True,
        channel_id="100000001",
        user_key="100000001",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert heartbeat_campaign_candidates(channels) == [("200000001", "1")]
    assert "100000001" not in channels
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


def test_deleted_campaign_is_removed_from_all_group_and_private_bindings() -> None:
    channels = {
        "group-1": "待删除团",
        "group-2": "待删除团",
        "group-3": "保留团",
    }
    users = {
        "user-1": "待删除团",
        "user-2": "待删除团",
        "user-3": "保留团",
    }

    removal = remove_deleted_campaign_bindings(
        "待删除团",
        channel_campaigns=channels,
        user_campaigns=users,
    )

    assert removal.channel_count == 2
    assert removal.user_count == 2
    assert channels == {"group-3": "保留团"}
    assert users == {"user-3": "保留团"}
    assert heartbeat_campaign_candidates(channels) == [("group-3", "保留团")]
