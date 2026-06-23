from __future__ import annotations

import json
from pathlib import Path

from fu_gm.components.map_icon_registry import MapIconRegistry
from fu_gm.components.map_renderer import NortantisMapRenderer, NortantisMapRendererConfig
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState


def enabled_catalog(tmp_path: Path) -> tuple[Path, Path]:
    catalog_root = tmp_path / "world_wonders"
    icon_dir = catalog_root / "test"
    icon_dir.mkdir(parents=True)
    icon_path = icon_dir / "floating_spire.png"
    icon_path.write_bytes(b"candidate-icon")
    (icon_dir / "catalog.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "icons": [
                    {
                        "icon_id": "floating_spire",
                        "name_zh": "断裂登神塔",
                        "file": icon_path.name,
                        "place_kind": "world_wonder_tower",
                        "preferred_terrain": ["mountain", "land"],
                        "default_scale": 1.25,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return catalog_root, icon_path


def test_registry_resolves_only_explicit_id_or_exact_name(tmp_path: Path) -> None:
    catalog_root, _ = enabled_catalog(tmp_path)
    registry = MapIconRegistry.from_root(catalog_root)

    assert registry.resolve(icon_id="floating_spire").name_zh == "断裂登神塔"
    assert registry.resolve(semantic_name="断裂登神塔").icon_id == "floating_spire"
    assert registry.resolve(semantic_name="登神塔") is None
    assert registry.resolve(semantic_name="北境断裂登神塔遗迹") is None


def test_registry_materializes_nortantis_custom_icon_pack(tmp_path: Path) -> None:
    catalog_root, icon_path = enabled_catalog(tmp_path)
    registry = MapIconRegistry.from_root(catalog_root)

    target_dir = registry.materialize_custom_pack(
        tmp_path / "custom_images",
        group_id="fu_gm_world_wonders",
        encoded_width=72,
    )

    target = target_dir / "floating_spire width=72.png"
    assert target.read_bytes() == icon_path.read_bytes()


def test_renderer_emits_persisted_custom_icon_metadata(tmp_path: Path) -> None:
    catalog_root, _ = enabled_catalog(tmp_path)
    custom_images = tmp_path / "custom_images"
    config = NortantisMapRendererConfig(
        project_dir=tmp_path,
        nortantis_dir=tmp_path / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=custom_images,
        icon_catalog_dir=catalog_root,
        jar_path=tmp_path / "Nortantis.jar",
    )
    world = WorldState()
    WorldMapManager(world).add_location(
        "玩家命名的高塔",
        feature_type="landmark",
        icon_id="floating_spire",
        draw_icon=True,
    )

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    label = next(item for item in brief["labels"] if item["text"] == "玩家命名的高塔")

    assert label["iconId"] == "floating_spire"
    assert label["iconName"] == "floating_spire"
    assert label["iconGroup"] == "fu_gm_world_wonders"
    assert label["iconScale"] == 1.0
    assert label["iconPlaceKind"] == "world_wonder_tower"
    assert label["iconPreferredTerrain"] == ["mountain", "land"]
    assert label["iconRenderType"] == "decorations"
    assert label["iconAnchorMode"] == "ground"
    assert label["iconPlacement"] == "land"
    assert label["drawIcon"] is True
    assert brief["customImagesPath"] == str(custom_images)
    assert brief["fu_gm_metadata"]["custom_icon_count"] == 1
    assert (custom_images / "decorations" / "fu_gm_world_wonders" / "floating_spire width=48.png").is_file()


def test_renderer_uses_prepared_location_icon_name_without_keywords(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=tmp_path / "custom_images",
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    WorldMapManager(world).add_location("灵魂网络中枢", feature_type="landmark")

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    label = next(item for item in brief["labels"] if item["text"] == "灵魂网络中枢")

    assert label["iconName"] == "soul_nexus"
    assert label["drawIcon"] is True


def test_renderer_emits_sky_island_metadata_from_catalog(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=tmp_path / "custom_images",
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    WorldMapManager(world).add_location("撒拉菲姆", feature_type="landmark")

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    label = next(item for item in brief["labels"] if item["text"] == "撒拉菲姆")

    assert label["iconName"] == "seraphim"
    assert label["iconPlaceKind"] == "prepared_sky_island"
    assert label["iconScale"] == 0.84
    assert label["preference"] == "sky_island"
    assert label["featureType"] == "sky_island"
    assert label["iconPlacement"] == "land"


def test_renderer_places_prepared_settlement_icons_on_land_not_lake_shore(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=tmp_path / "custom_images",
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    WorldMapManager(world).add_location("奥涅里亚", feature_type="landmark")

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    label = next(item for item in brief["labels"] if item["text"] == "奥涅里亚")

    assert label["iconName"] == "oneria"
    assert label["iconPlaceKind"] == "prepared_settlement"
    assert label["preference"] == "land"
    assert label["iconPlacement"] == "land"


def test_renderer_resolves_deprecated_prepared_location_names_to_canonical_icons(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=tmp_path / "custom_images",
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    manager = WorldMapManager(world)
    manager.add_location("边境起始王国", feature_type="landmark")
    manager.add_location("第七采掘城", feature_type="landmark")

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    labels = {item["text"]: item for item in brief["labels"]}

    assert labels["边境起始王国"]["iconName"] == "oneria"
    assert labels["边境起始王国"]["iconPlaceKind"] == "prepared_settlement"
    assert labels["第七采掘城"]["iconName"] == "excavator_seven"
    assert labels["第七采掘城"]["iconPlaceKind"] == "prepared_mobile_city"
