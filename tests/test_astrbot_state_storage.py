from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


def _load_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "state_storage.py"
    )
    spec = importlib.util.spec_from_file_location(
        "fu_gm_bridge_state_storage",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


state_storage = _load_module()


def _load_campaign_binding_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "campaign_binding.py"
    )
    spec = importlib.util.spec_from_file_location(
        "fu_gm_bridge_campaign_binding_for_storage",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


campaign_binding = _load_campaign_binding_module()


def test_atomic_binding_write_replaces_complete_json(tmp_path: Path) -> None:
    path = tmp_path / "channel_campaigns.json"

    state_storage.write_json_map_atomic(
        path,
        {"group-1": "宁姆格福", "group-2": "default"},
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "group-1": "宁姆格福",
        "group-2": "default",
    }
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_replace_keeps_previous_binding_file(tmp_path: Path) -> None:
    path = tmp_path / "channel_campaigns.json"
    path.write_text('{"group-1": "old"}\n', encoding="utf-8")

    with patch.object(state_storage.os, "replace", side_effect=OSError("disk")):
        with pytest.raises(OSError, match="disk"):
            state_storage.write_json_map_atomic(path, {"group-1": "new"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"group-1": "old"}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_json_write_supports_observed_member_lists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "channel_members.json"

    state_storage.write_json_atomic(
        path,
        {
            "group-1": ["user-1", "user-2"],
        },
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "group-1": ["user-1", "user-2"],
    }


def test_deleted_campaign_bindings_stay_removed_after_plugin_restart(
    tmp_path: Path,
) -> None:
    channel_path = tmp_path / "channel_campaigns.json"
    user_path = tmp_path / "user_campaigns.json"
    channels = {"group-1": "待删除团", "group-2": "保留团"}
    users = {"user-1": "待删除团", "user-2": "保留团"}
    state_storage.write_json_map_atomic(channel_path, channels)
    state_storage.write_json_map_atomic(user_path, users)

    restarted_channels = json.loads(channel_path.read_text(encoding="utf-8"))
    restarted_users = json.loads(user_path.read_text(encoding="utf-8"))
    removal = campaign_binding.remove_deleted_campaign_bindings(
        "待删除团",
        channel_campaigns=restarted_channels,
        user_campaigns=restarted_users,
    )
    state_storage.write_json_map_atomic(channel_path, restarted_channels)
    state_storage.write_json_map_atomic(user_path, restarted_users)

    second_restart_channels = json.loads(
        channel_path.read_text(encoding="utf-8")
    )
    second_restart_users = json.loads(user_path.read_text(encoding="utf-8"))
    assert removal.channel_count == 1
    assert removal.user_count == 1
    assert second_restart_channels == {"group-2": "保留团"}
    assert second_restart_users == {"user-2": "保留团"}
    assert campaign_binding.heartbeat_campaign_candidates(
        second_restart_channels
    ) == [("group-2", "保留团")]
