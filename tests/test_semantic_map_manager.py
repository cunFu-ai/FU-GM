from __future__ import annotations

import json

import pytest

from fu_gm.components.semantic_map_manager import SemanticMapManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState


def test_relative_location_candidates_are_computed_from_placed_reference() -> None:
    world = WorldState()
    world_map = WorldMapManager(world)
    world_map.add_location(
        "西国",
        feature_type="country",
        position_hint="west",
        semantic_cell="E06",
    )
    world_map.add_location(
        "东塔",
        feature_type="landmark",
        relative_to="西国",
        relative_position="east",
    )
    manager = SemanticMapManager()
    manager.initialize(world)

    candidates = manager.candidates(world, "东塔", limit=6)

    assert candidates
    assert all(
        manager.cell_xy(str(item["cell"]))[0]
        > manager.cell_xy("E06")[0]
        for item in candidates
    )


def test_place_rejects_cells_outside_the_inspected_candidate_set() -> None:
    world = WorldState()
    WorldMapManager(world).add_location(
        "钟鸣公国",
        feature_type="country",
        position_hint="north",
    )
    manager = SemanticMapManager()
    allowed = {
        "钟鸣公国": {
            str(item["cell"])
            for item in manager.candidates(world, "钟鸣公国", limit=3)
        }
    }

    with pytest.raises(ValueError, match="合法候选"):
        manager.place(
            world,
            [{"location_name": "钟鸣公国", "grid_cell": "T12"}],
            allowed_cells=allowed,
        )


def test_legacy_brief_is_migrated_without_a_visual_model(tmp_path) -> None:
    world = WorldState()
    WorldMapManager(world).add_location(
        "星落尖塔",
        feature_type="landmark",
    )
    brief_path = tmp_path / "old.brief.json"
    brief_path.write_text(
        json.dumps(
            {
                "labels": [
                    {"text": "旧大陆", "type": "Title", "x": 0.5, "y": 0.8},
                    {
                        "text": "星落尖塔",
                        "type": "City",
                        "x": 0.75,
                        "y": 0.25,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    world.record_memory_event(
        "旧地图",
        kind="world_map_visual",
        payload={"brief_path": str(brief_path)},
    )

    layout = SemanticMapManager().initialize(world)

    assert layout.source == "legacy_brief"
    assert layout.location_cells["星落尖塔"] == "O04"
    assert world.map_locations["星落尖塔"].semantic_cell == "O04"


def test_nortantis_manifest_updates_actual_terrain_and_anchor_cells() -> None:
    world = WorldState()
    WorldMapManager(world).add_location(
        "沉默森林",
        feature_type="forest",
    )
    manager = SemanticMapManager()
    rows = manager.view(world).terrain_rows
    rows[4] = rows[4][:8] + "F" + rows[4][9:]

    layout = manager.apply_manifest(
        world,
        {
            "version": 1,
            "grid_width": 20,
            "grid_height": 12,
            "terrain_rows": rows,
            "locations": {
                "沉默森林": {
                    "cell": "I05",
                    "normalized_x": 0.42,
                    "normalized_y": 0.36,
                    "terrain": "F",
                    "anchor_kind": "label_center",
                }
            },
        },
        manifest_path="/tmp/map.layout.json",
    )

    assert layout.source == "nortantis_manifest"
    assert layout.location_cells["沉默森林"] == "I05"
    assert layout.location_points["沉默森林"]["terrain"] == "F"
    assert manager.terrain_at(layout, "I05") == "F"


def test_ascii_grid_exposes_terrain_and_location_legend() -> None:
    world = WorldState()
    WorldMapManager(world).add_location(
        "赤砂帝国",
        feature_type="country",
        semantic_cell="D07",
    )
    manager = SemanticMapManager()
    manager.initialize(world)

    text = manager.ascii_grid(world)

    assert "A B C D" in text
    assert "地形：" in text
    assert "赤砂帝国@D07" in text
