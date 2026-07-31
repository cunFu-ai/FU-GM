from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

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


def test_registry_materializes_black_ink_alpha_mask_style(tmp_path: Path) -> None:
    catalog_root = tmp_path / "world_wonders"
    icon_dir = catalog_root / "test"
    icon_dir.mkdir(parents=True)
    icon_path = icon_dir / "colored_symbol.png"
    image = Image.new("RGBA", (2, 1))
    image.putdata([(80, 120, 40, 255), (255, 255, 255, 255)])
    image.save(icon_path)
    (icon_dir / "catalog.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "style": "nortantis_black_ink_alpha_mask",
                "alpha_max": 205,
                "icons": [
                    {
                        "icon_id": "colored_symbol",
                        "name_zh": "彩色符号",
                        "file": icon_path.name,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry = MapIconRegistry.from_root(catalog_root)

    target_dir = registry.materialize_custom_pack(
        tmp_path / "custom_images",
        group_id="fu_gm_world_wonders",
        encoded_width=48,
    )
    materialized = Image.open(target_dir / "colored_symbol width=48.png").convert("RGBA")
    pixels = list(materialized.getdata())

    assert pixels[0][:3] == (0, 0, 0)
    assert 0 < pixels[0][3] <= 205
    assert pixels[1] == (0, 0, 0, 0)


def test_default_country_icons_are_neutral_black_alpha_masks() -> None:
    project = Path(__file__).resolve().parents[1]
    icon_paths = sorted((project / "assets" / "nortantis_custom" / "world_wonders" / "defaults").glob("default_country*.png"))

    assert icon_paths
    for path in icon_paths:
        image = Image.open(path).convert("RGBA")
        visible_pixels = [pixel for pixel in image.getdata() if pixel[3] > 0]
        assert visible_pixels, path.name
        assert all(pixel[:3] == (0, 0, 0) for pixel in visible_pixels), path.name
        assert max(pixel[3] for pixel in visible_pixels) <= 205, path.name


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


def test_renderer_assigns_default_country_icon_even_for_legacy_disabled_flag(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    custom_images = tmp_path / "custom_images"
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=custom_images,
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    WorldMapManager(world).add_location(
        "测试国",
        feature_type="country",
        faction="测试国",
        draw_icon=False,
    )

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    label = next(item for item in brief["labels"] if item["text"] == "测试国")

    assert label["type"] == "City"
    assert label["featureType"] == "country"
    assert label["drawIcon"] is True
    assert label["iconName"] == "default_country_seat"
    assert label["iconPlaceKind"] == "default_country_anchor"
    assert label["iconScale"] == 0.96
    assert label["iconLabelOffset"] == 20.0
    assert label["iconLabelLockBelow"] is True
    assert label["iconRenderType"] == "decorations"
    assert (custom_images / "decorations" / "fu_gm_world_wonders" / "default_country_seat width=48.png").is_file()


def test_renderer_assigns_unique_default_country_icons_per_map(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    custom_images = tmp_path / "custom_images"
    config = NortantisMapRendererConfig(
        project_dir=project,
        nortantis_dir=project / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        custom_images_dir=custom_images,
        icon_catalog_dir=project / "assets" / "nortantis_custom" / "world_wonders",
        jar_path=project / "integrations" / "nortantis" / "build" / "libs" / "Nortantis.jar",
    )
    world = WorldState()
    manager = WorldMapManager(world)
    for name in ("钟鸣公国", "奥涅里亚", "辉钢财团", "树誓村社"):
        manager.add_location(name, feature_type="country", faction=name, draw_icon=True)
    manager.add_location("白花碑驿站", feature_type="settlement", draw_icon=True)

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    labels = {item["text"]: item for item in brief["labels"]}
    country_labels = [labels[name] for name in ("钟鸣公国", "奥涅里亚", "辉钢财团", "树誓村社")]
    country_icon_names = [label["iconName"] for label in country_labels]

    assert len(country_icon_names) == len(set(country_icon_names))
    assert labels["钟鸣公国"]["iconName"] == "default_country_clocktower"
    assert labels["辉钢财团"]["iconName"] == "default_country_merchant_hall"
    assert labels["树誓村社"]["iconName"] == "default_country_grove_hall"
    assert all(name.startswith("default_country_") for name in country_icon_names)
    assert all(label["iconPlaceKind"] == "default_country_anchor" for label in country_labels)
    assert all(label["iconLabelLockBelow"] is True for label in country_labels)
    assert labels["白花碑驿站"]["featureType"] == "settlement"
    assert labels["白花碑驿站"].get("iconName") is None


def test_renderer_prefers_country_icon_style_from_location_semantics(tmp_path: Path) -> None:
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
    manager.add_location("钟鸣公国", feature_type="country", description="钟楼、机关与公国议政厅。")
    manager.add_location("奥涅里亚", feature_type="country", description="灯塔舰队与王室海图牵涉的海上国家。")
    manager.add_location("树誓村社", feature_type="country", description="沉默森林旁的自然村社同盟。")
    manager.add_location("符文档案国", feature_type="country", description="以档案馆、符文与知识议会立国。")
    manager.add_location("水晶庭邦", feature_type="country", description="水晶与奥术学院支撑的魔法国度。")

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    labels = {item["text"]: item for item in brief["labels"]}

    assert labels["钟鸣公国"]["iconName"] == "default_country_clocktower"
    assert labels["奥涅里亚"]["iconName"] == "default_country_lighthouse_palace"
    assert labels["树誓村社"]["iconName"] == "default_country_grove_hall"
    assert labels["符文档案国"]["iconName"] == "default_country_rune_archive"
    assert labels["水晶庭邦"]["iconName"] == "default_country_crystal_court"


def test_renderer_never_reuses_default_country_icon_when_pool_is_exhausted(tmp_path: Path) -> None:
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
    country_count = len(NortantisMapRenderer.DEFAULT_COUNTRY_ICON_IDS) + 1
    for index in range(country_count):
        manager.add_location(f"测试国{index}", feature_type="country", faction=f"测试国{index}", draw_icon=True)

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    country_labels = [item for item in brief["labels"] if item.get("featureType") == "country"]
    country_icon_names = [item.get("iconName") for item in country_labels if item.get("iconName")]
    text_only_labels = [item for item in country_labels if not item.get("iconName")]

    assert len(country_icon_names) == len(NortantisMapRenderer.DEFAULT_COUNTRY_ICON_IDS)
    assert len(country_icon_names) == len(set(country_icon_names))
    assert len(text_only_labels) == 1
    assert text_only_labels[0]["drawIcon"] is False


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


def test_renderer_prefers_seventh_excavator_icon_over_default_country_icon(tmp_path: Path) -> None:
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
    manager.add_location("第七采掘城", feature_type="country", faction="辉钢财团", draw_icon=True)
    manager.add_location("钟鸣公国", feature_type="country", faction="钟鸣公国", draw_icon=True)

    brief = NortantisMapRenderer(config).build_brief(world, output_path=tmp_path / "world.png")
    labels = {item["text"]: item for item in brief["labels"]}

    assert labels["第七采掘城"]["iconName"] == "excavator_seven"
    assert labels["第七采掘城"]["iconPlaceKind"] == "prepared_mobile_city"
    assert labels["第七采掘城"]["iconLabelLockBelow"] is False
    assert labels["钟鸣公国"]["iconName"] == "default_country_clocktower"
    assert labels["钟鸣公国"]["iconPlaceKind"] == "default_country_anchor"
