from __future__ import annotations

from pathlib import Path

import pytest

from fu_gm.components.map_renderer import NortantisMapRenderer, NortantisMapRendererConfig
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import MapRouteSegment


def renderer(tmp_path: Path) -> NortantisMapRenderer:
    config = NortantisMapRendererConfig(
        project_dir=tmp_path,
        nortantis_dir=tmp_path / "integrations" / "nortantis",
        output_dir=tmp_path / "maps",
        java_exe="java",
        jar_path=tmp_path / "Nortantis.jar",
    )
    return NortantisMapRenderer(config)


def test_nortantis_renderer_builds_brief_from_world_state_graph(tmp_path: Path) -> None:
    world = WorldState()
    world.world_profile.campaign_title = "齿轮与藤蔓"
    world.world_profile.continent_name = "希尔大陆"
    world.world_profile.kingdoms = {
        "索朗帝国": "曾经强盛的蒸汽帝国，旧都仍留有机械巨兽遗迹。",
        "自然联邦": "崇尚自然的王国联邦，守护古老林海。",
    }
    world_map = WorldMapManager(world)
    world_map.add_location("索朗旧都", x=0, y=0, terrain="城市", feature_type="settlement", description="齿轮与藤蔓缠绕的废墟。", faction="索朗帝国")
    world_map.add_location("藤蔓战场", x=5, y=3, terrain="草原", feature_type="region", description="禁忌仪式留下的旧战场。")
    world_map.add_location("自然联邦林海", x=10, y=4, terrain="广袤森林", feature_type="forest", description="古老林海。", faction="自然联邦")
    world_map.add_route(
        "索朗旧都",
        "自然联邦林海",
        route_id="solang_to_forest",
        distance_days=4,
        default_threat_level="high",
        terrain="山丘与草地",
        segments=[
            MapRouteSegment(region="藤蔓战场", distance_days=2, threat_level="medium"),
            MapRouteSegment(region="自然联邦林海", distance_days=2, threat_level="high"),
        ],
    )

    brief = renderer(tmp_path).build_brief(
        world,
        output_path=tmp_path / "world.png",
        settings_path=tmp_path / "world.nort",
    )

    assert brief["outputPath"].endswith("world.png")
    assert brief["settingsPath"].endswith("world.nort")
    assert brief["fontFamily"] == "PingFangSaTuoTi"
    assert brief["fontFile"] == ""
    assert brief["artPack"] == "nortantis"
    assert brief["drawGridOverlay"] is False
    assert brief["generatedWidth"] == 4096
    assert brief["generatedHeight"] == 2531
    assert brief["resolution"] == pytest.approx(1.0)
    assert brief["worldSize"] == 8000
    assert brief["regionCount"] == 7
    assert brief["landShape"] == "Continents"
    assert brief["terrainSeedAttempts"] == 8
    assert brief["minCityHopDistance"] == 5
    assert brief["fu_gm_metadata"]["style"] == "sepia_parchment"
    assert brief["landColor"] == "173,157,106,255"
    assert brief["regionBaseColor"] == "176,151,102,255"
    assert brief["oceanColor"] == "214,203,171,255"
    assert brief["borderColor"] == "173,157,106,255"
    assert brief["generateBackground"] is True
    assert brief["generateBackgroundFromTexture"] is False
    assert brief["solidColorBackground"] is False
    assert brief["backgroundRandomSeed"] == 427953844
    assert brief["lineStyle"] == "SplinesWithSmoothedCoastlines"
    assert brief["coastlineWidth"] == pytest.approx(2.7)
    assert brief["coastShadingLevel"] == 0
    assert brief["coastShadingColor"] == "86,78,53,65"
    assert brief["oceanShadingLevel"] == 13
    assert brief["oceanShadingColor"] == "65,61,48,87"
    assert brief["oceanWavesType"] == "ConcentricWaves"
    assert brief["concentricWaveCount"] == 3
    assert brief["fadeConcentricWaves"] is True
    assert brief["jitterToConcentricWaves"] is True
    assert brief["brokenLinesForConcentricWaves"] is True
    assert brief["oceanWavesColor"] == "103,96,79,204"
    assert brief["regionBoundaryStyleType"] == "Solid"
    assert brief["roadStyleType"] == "Dashes"
    assert brief["roadWidth"] == pytest.approx(2.7)
    assert brief["edgeLandToWaterProbability"] == pytest.approx(0.33)
    assert brief["centerLandToWaterProbability"] == pytest.approx(0.67)
    assert brief["drawBorder"] is True
    assert brief["drawGrunge"] is True
    assert brief["frayedBorder"] is True
    assert brief["frayedBorderSize"] == 13
    assert brief["frayedBorderBlurLevel"] == 134
    assert brief["grungeWidth"] == 1406
    assert brief["titleFontSize"] == 38
    assert brief["regionFontSize"] == 21
    assert brief["mountainRangeFontSize"] == 15
    assert brief["otherMountainsFontSize"] == 12
    assert brief["citiesFontSize"] == 12
    assert brief["riverFontSize"] == 10
    assert brief["labels"][0] == {
        "text": "希尔大陆",
            "type": "Title",
            "snapToLand": False,
            "snapToOcean": True,
            "x": 0.5,
            "y": 0.82,
        }
    assert brief["generatedNamePool"] == []
    assert brief["generateRandomCityRoads"] is False
    assert brief["cityProbability"] == 0.0
    assert brief["fu_gm_metadata"]["continent_name"] == "希尔大陆"
    assert brief["fu_gm_metadata"]["needs_continent_name"] is False
    label_by_name = {label["text"]: label for label in brief["labels"][1:]}
    assert label_by_name["索朗旧都"]["type"] == "City"
    assert label_by_name["索朗旧都"]["terrain"] == "城市"
    assert label_by_name["索朗旧都"]["preference"] == "land"
    assert label_by_name["索朗旧都"]["drawIcon"] is True
    assert label_by_name["索朗旧都"]["snapToLand"] is True
    assert label_by_name["索朗旧都"]["x"] == pytest.approx(0.1)
    assert label_by_name["自然联邦林海"]["type"] == "Region"
    assert label_by_name["自然联邦林海"]["preference"] == "forest"
    assert label_by_name["自然联邦林海"]["drawIcon"] is False
    assert label_by_name["自然联邦林海"]["x"] == pytest.approx(0.9)
    assert brief["roads"][0]["route_id"] == "solang_to_forest"
    assert brief["roads"][0]["threat_level"] == "high"
    assert brief["roads"][0]["path"] == [
        {"name": "索朗旧都", "x": label_by_name["索朗旧都"]["x"], "y": label_by_name["索朗旧都"]["y"]},
        {"name": "藤蔓战场", "x": label_by_name["藤蔓战场"]["x"], "y": label_by_name["藤蔓战场"]["y"]},
        {"name": "自然联邦林海", "x": label_by_name["自然联邦林海"]["x"], "y": label_by_name["自然联邦林海"]["y"]},
    ]
    regions = {region["name"]: region for region in brief["politicalRegions"]}
    assert set(regions) == {"索朗帝国", "自然联邦"}
    assert regions["索朗帝国"]["anchors"] == [
        {
            "name": "索朗旧都",
            "x": label_by_name["索朗旧都"]["x"],
            "y": label_by_name["索朗旧都"]["y"],
            "terrain": "城市",
        }
    ]
    assert regions["自然联邦"]["anchors"] == [
        {
            "name": "自然联邦林海",
            "x": label_by_name["自然联邦林海"]["x"],
            "y": label_by_name["自然联邦林海"]["y"],
            "terrain": "广袤森林",
        }
    ]
    assert {region["color"] for region in regions.values()}.issubset({"#b09766", "#ab986d", "#b0a76a"})
    assert brief["fu_gm_metadata"]["political_region_count"] == 2
    assert brief["fu_gm_metadata"]["rules_truth"] == "WorldState.map_routes"


def test_nortantis_renderer_marks_natural_features_without_city_icons(tmp_path: Path) -> None:
    world = WorldState()
    world_map = WorldMapManager(world)
    world_map.add_location("镜线湖", x=0, y=0, terrain="内陆湖", feature_type="lake", description="藤蔓倒映在湖心。")
    world_map.add_location("翠缆林海", x=5, y=0, terrain="古老林海", feature_type="forest")
    world_map.add_location("镜湖镇", x=10, y=0, terrain="城镇", feature_type="settlement", description="建在湖畔的贸易镇。")
    world_map.add_route("镜湖镇", "翠缆林海", route_id="town_to_forest")

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")

    labels = {label["text"]: label for label in brief["labels"][1:]}
    assert labels["镜线湖"]["type"] == "Region"
    assert labels["镜线湖"]["preference"] == "lake"
    assert labels["镜线湖"]["drawIcon"] is False
    assert labels["翠缆林海"]["type"] == "Region"
    assert labels["翠缆林海"]["preference"] == "forest"
    assert labels["翠缆林海"]["drawIcon"] is False
    assert labels["镜湖镇"]["type"] == "City"
    assert labels["镜湖镇"]["preference"] == "land"
    assert labels["镜湖镇"]["drawIcon"] is True


def test_nortantis_renderer_uses_semantic_geography_and_separates_relative_locations(tmp_path: Path) -> None:
    world = WorldState()
    world.world_profile.continent_name = "绯雨大陆"
    world_map = WorldMapManager(world)
    world_map.add_location("镜线内海", feature_type="inland_sea", position_hint="center", draw_icon=False)
    world_map.add_location(
        "钟鸣公国", feature_type="country", relative_to="镜线内海", relative_position="north",
        faction="钟鸣公国", draw_icon=False,
    )
    world_map.add_location(
        "潮鸢群岛", feature_type="archipelago", position_hint="southeast", faction="潮鸢群岛", draw_icon=False
    )
    world_map.add_location("鸦羽山脉", feature_type="mountain_range", position_hint="west", draw_icon=False)
    world_map.add_location("白花碑驿站", feature_type="settlement", position_hint="south", draw_icon=True)

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")
    labels = {label["text"]: label for label in brief["labels"][1:]}

    assert labels["潮鸢群岛"]["preference"] == "archipelago"
    assert labels["潮鸢群岛"]["featureType"] == "archipelago"
    assert labels["潮鸢群岛"]["drawIcon"] is False
    assert labels["鸦羽山脉"]["preference"] == "mountain"
    assert labels["鸦羽山脉"]["drawIcon"] is False
    assert labels["钟鸣公国"]["y"] < labels["镜线内海"]["y"]
    assert (labels["钟鸣公国"]["x"], labels["钟鸣公国"]["y"]) != (
        labels["潮鸢群岛"]["x"], labels["潮鸢群岛"]["y"]
    )
    assert labels["白花碑驿站"]["type"] == "City"
    assert labels["白花碑驿站"]["drawIcon"] is True


def test_nortantis_renderer_does_not_use_campaign_title_as_continent_name(tmp_path: Path) -> None:
    world = WorldState()
    world.world_profile.campaign_title = "齿轮与藤蔓"
    world_map = WorldMapManager(world)
    world_map.add_location("公开村庄", x=0, y=0, terrain="村庄", feature_type="settlement")

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")

    assert brief["labels"][0]["text"] == "未命名大陆"
    assert brief["labels"][0]["snapToOcean"] is True
    assert brief["fu_gm_metadata"]["continent_name"] == ""
    assert brief["fu_gm_metadata"]["needs_continent_name"] is True


def test_nortantis_renderer_uses_continent_name_only_as_title_when_duplicate_location_exists(tmp_path: Path) -> None:
    world = WorldState()
    world.world_profile.continent_name = "绯雨大陆"
    world_map = WorldMapManager(world)
    world_map.add_location("绯雨大陆", x=0, y=0, terrain="草原", feature_type="country", draw_icon=False)
    world_map.add_location("奥涅里亚", x=4, y=1, terrain="草原", feature_type="settlement")

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")

    assert brief["labels"][0]["text"] == "绯雨大陆"
    assert brief["labels"][0]["type"] == "Title"
    assert [label["text"] for label in brief["labels"]].count("绯雨大陆") == 1
    assert brief["fu_gm_metadata"]["location_count"] == 1


def test_nortantis_renderer_caps_geological_region_count_at_nortantis_limit(tmp_path: Path) -> None:
    world = WorldState()
    world.world_profile.kingdoms = {f"政权{i}": "测试政权" for i in range(24)}
    world_map = WorldMapManager(world)
    for index, faction in enumerate(world.world_profile.kingdoms):
        world_map.add_location(f"{faction}首府", x=index, y=index % 3, terrain="城市", feature_type="settlement", faction=faction)

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")

    assert brief["regionCount"] == 20
    assert brief["fu_gm_metadata"]["political_region_count"] == 24


def test_nortantis_renderer_skips_hidden_locations_and_routes(tmp_path: Path) -> None:
    world = WorldState()
    world_map = WorldMapManager(world)
    world_map.add_location("公开村庄", x=0, y=0, terrain="村庄", feature_type="settlement")
    world_map.add_location("隐藏神殿", x=10, y=0, terrain="遗迹", discovered=False)
    world_map.add_route("公开村庄", "隐藏神殿", route_id="secret_path", discovered=False)

    brief = renderer(tmp_path).build_brief(world, output_path=tmp_path / "world.png")

    texts = [label["text"] for label in brief["labels"]]
    assert "公开村庄" in texts
    assert "隐藏神殿" not in texts
    assert brief["roads"] == []


def test_nortantis_renderer_requires_existing_jar_unless_auto_build(tmp_path: Path) -> None:
    world = WorldState()
    world_map = WorldMapManager(world)
    world_map.add_location("公开村庄", x=0, y=0, terrain="村庄", feature_type="settlement")

    with pytest.raises(FileNotFoundError, match="Nortantis jar 不存在"):
        renderer(tmp_path).render(world, campaign_id="demo")


def test_nortantis_renderer_keeps_fixed_canvas_and_world_size_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FU_GM_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("FU_GM_NORTANTIS_WIDTH", "2048")
    monkeypatch.setenv("FU_GM_NORTANTIS_HEIGHT", "1152")
    monkeypatch.setenv("FU_GM_NORTANTIS_WORLD_SIZE", "14000")

    config = NortantisMapRendererConfig.from_env()

    assert config.generated_width == 4096
    assert config.generated_height == 2531
    assert config.world_size == 8000
    assert config.region_count == 7
    assert config.min_city_hop_distance == 5
    assert config.font_family == "PingFangSaTuoTi"
    assert config.font_file is None
