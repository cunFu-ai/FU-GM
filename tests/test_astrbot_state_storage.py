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
