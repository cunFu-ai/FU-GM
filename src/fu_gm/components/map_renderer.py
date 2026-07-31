from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

from fu_gm.components.map_icon_registry import MapIconRegistry, MapIconSpec
from fu_gm.components.semantic_map_manager import SemanticMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import MapLocation, MapRouteEdge


class MapRenderer(Protocol):
    """把 FU-GM 的机读地图转换为人类可看的地图产物。"""

    def build_brief(self, world_state: WorldState, *, output_path: str | Path, settings_path: str | Path | None = None) -> dict:
        ...

    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> "MapRenderResult":
        ...


@dataclass
class NortantisMapRendererConfig:
    project_dir: Path
    nortantis_dir: Path
    output_dir: Path
    custom_images_dir: Path | None = None
    icon_catalog_dir: Path | None = None
    java_exe: str = "java"
    jar_path: Path | None = None
    font_family: str = "PingFangSaTuoTi"
    font_file: Path | None = None
    generated_width: int = 4096
    generated_height: int = 2531
    resolution: float = 1.0
    world_size: int = 8000
    region_count: int = 7
    land_shape: str = "Continents"
    map_style: str = "sepia_parchment"
    terrain_seed_attempts: int = 12
    min_city_hop_distance: int = 5
    wonder_icons_enabled: bool = True
    wonder_icon_group: str = "fu_gm_world_wonders"
    wonder_icon_width: int = 48
    wonder_icon_scale_multiplier: float = 0.8
    timeout_seconds: float = 180.0
    auto_build: bool = False

    def __post_init__(self) -> None:
        if self.font_file is not None:
            return
        default_font = self.project_dir / "assets" / "fonts" / "PingFangSaTuoTi" / "PingFangSaTuoTi-2.ttf"
        if default_font.exists():
            self.font_file = default_font

    @classmethod
    def from_env(cls) -> "NortantisMapRendererConfig":
        project_dir = Path(os.environ.get("FU_GM_PROJECT_DIR", Path.cwd())).resolve()
        nortantis_dir = Path(
            os.environ.get("FU_GM_NORTANTIS_DIR", project_dir / "integrations" / "nortantis")
        ).resolve()
        output_dir = Path(
            os.environ.get(
                "FU_GM_NORTANTIS_OUTPUT_DIR",
                project_dir / ".runtime" / ".fu-gm" / "data" / "nortantis_maps",
            )
        ).resolve()
        custom_images_dir = Path(
            os.environ.get(
                "FU_GM_NORTANTIS_CUSTOM_IMAGES_DIR",
                project_dir / "assets" / "nortantis_custom",
            )
        ).resolve()
        icon_catalog_dir = Path(
            os.environ.get(
                "FU_GM_NORTANTIS_ICON_CATALOG_DIR",
                custom_images_dir / "world_wonders",
            )
        ).resolve()
        java_exe = (
            os.environ.get("FU_GM_JAVA_EXE", "").strip()
            or os.environ.get("FU_GM_JAVA_BIN", "").strip()
            or cls._default_java_exe(project_dir)
        )
        jar_path = Path(os.environ.get("FU_GM_NORTANTIS_JAR", nortantis_dir / "build" / "libs" / "Nortantis.jar"))
        font_file_raw = os.environ.get("FU_GM_NORTANTIS_FONT_FILE", "").strip()
        font_file = Path(font_file_raw).resolve() if font_file_raw else (
            project_dir / "assets" / "fonts" / "PingFangSaTuoTi" / "PingFangSaTuoTi-2.ttf"
        ).resolve()
        if not font_file.exists():
            font_file = None
        return cls(
            project_dir=project_dir,
            nortantis_dir=nortantis_dir,
            output_dir=output_dir,
            custom_images_dir=custom_images_dir,
            icon_catalog_dir=icon_catalog_dir,
            java_exe=java_exe,
            jar_path=jar_path.resolve(),
            font_family=os.environ.get("FU_GM_NORTANTIS_FONT_FAMILY", "PingFangSaTuoTi"),
            font_file=font_file,
            generated_width=4096,
            generated_height=2531,
            resolution=float(os.environ.get("FU_GM_NORTANTIS_RESOLUTION", "1.0")),
            world_size=8000,
            region_count=int(os.environ.get("FU_GM_NORTANTIS_REGION_COUNT", "7")),
            land_shape=os.environ.get("FU_GM_NORTANTIS_LAND_SHAPE", "Continents"),
            map_style=os.environ.get("FU_GM_NORTANTIS_STYLE", "sepia_parchment"),
            terrain_seed_attempts=int(os.environ.get("FU_GM_NORTANTIS_TERRAIN_SEED_ATTEMPTS", "12")),
            min_city_hop_distance=max(0, int(os.environ.get("FU_GM_NORTANTIS_MIN_CITY_HOPS", "5"))),
            wonder_icons_enabled=os.environ.get("FU_GM_NORTANTIS_WONDER_ICONS", "1").lower()
            in {"1", "true", "yes", "enabled", "on"},
            wonder_icon_group=os.environ.get("FU_GM_NORTANTIS_WONDER_ICON_GROUP", "fu_gm_world_wonders"),
            wonder_icon_width=max(1, int(os.environ.get("FU_GM_NORTANTIS_WONDER_ICON_WIDTH", "48"))),
            wonder_icon_scale_multiplier=max(
                0.1,
                float(os.environ.get("FU_GM_NORTANTIS_WONDER_ICON_SCALE_MULTIPLIER", "0.8")),
            ),
            timeout_seconds=float(os.environ.get("FU_GM_NORTANTIS_TIMEOUT_SECONDS", "180")),
            auto_build=os.environ.get("FU_GM_NORTANTIS_AUTO_BUILD", "").lower()
            in {"1", "true", "yes", "enabled", "on"},
        )

    @staticmethod
    def _default_java_exe(project_dir: Path | None = None) -> str:
        java_home = os.environ.get("FU_GM_JAVA_HOME", "").strip() or os.environ.get("JAVA_HOME", "").strip()
        if java_home:
            candidate = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
            if candidate.exists():
                return str(candidate)
        if project_dir is not None:
            runtime_jdks = project_dir / ".runtime" / "jdks"
            candidates = [
                *runtime_jdks.glob("*/Contents/Home/bin/java"),
                *runtime_jdks.glob("*/bin/java"),
            ]
            for candidate in sorted(candidates):
                if candidate.exists():
                    return str(candidate)
        common_windows = Path(r"C:\Program Files\Java\jdk-25.0.3\bin\java.exe")
        if common_windows.exists():
            return str(common_windows)
        return "java"


@dataclass
class MapRenderResult:
    renderer: str
    brief_path: str
    output_path: str
    settings_path: str
    command: list[str]
    manifest_path: str = ""
    stdout: str = ""
    stderr: str = ""
    seed: int = 0
    terrain_seed: int = 0


class NortantisMapRenderer:
    """将 WorldState 的地点/路线 Graph 转成 Nortantis brief，并调用本地 Nortantis 导出器。

    注意：这里的坐标只用于视觉布局。旅行日、威胁等级和可选路线仍由 WorldState.map_routes
    作为规则真相；渲染器不会读取图片、量像素或反推距离。
    """

    EXPORTER_CLASS = "nortantis.tools.FuGmHeadlessExporter"
    DEFAULT_COUNTRY_ICON_ID = "default_country_seat"
    DEFAULT_COUNTRY_ICON_IDS = (
        "default_country_seat",
        "default_country_crown_gate",
        "default_country_lighthouse_palace",
        "default_country_mountain_keep",
        "default_country_temple_spire",
        "default_country_clocktower",
        "default_country_merchant_hall",
        "default_country_grove_hall",
        "default_country_banner_citadel",
        "default_country_crystal_court",
        "default_country_sky_bastion",
        "default_country_rune_archive",
    )
    COUNTRY_ICON_STYLE_HINTS = {
        "default_country_clocktower": (
            "钟",
            "时间",
            "时钟",
            "钟楼",
            "齿轮",
            "机关",
            "clock",
            "time",
            "gear",
        ),
        "default_country_lighthouse_palace": (
            "海",
            "港",
            "湾",
            "潮",
            "舰",
            "灯塔",
            "海图",
            "航",
            "水手",
            "coast",
            "harbor",
            "fleet",
            "sea",
        ),
        "default_country_mountain_keep": (
            "山",
            "岭",
            "峰",
            "矿",
            "采掘",
            "要塞",
            "边境",
            "堡",
            "mountain",
            "mine",
            "fort",
        ),
        "default_country_temple_spire": (
            "神",
            "圣",
            "教",
            "祭",
            "仪式",
            "信仰",
            "圣焰",
            "temple",
            "holy",
            "ritual",
        ),
        "default_country_merchant_hall": (
            "商",
            "财团",
            "行会",
            "议会",
            "联邦",
            "共和国",
            "贸易",
            "商盟",
            "merchant",
            "guild",
            "league",
            "republic",
        ),
        "default_country_grove_hall": (
            "森",
            "林",
            "树",
            "誓",
            "村社",
            "自然",
            "藤",
            "花",
            "forest",
            "grove",
            "tree",
            "nature",
        ),
        "default_country_banner_citadel": (
            "守望",
            "军",
            "骑士",
            "壁垒",
            "旗",
            "城塞",
            "城墙",
            "watch",
            "banner",
            "citadel",
            "military",
        ),
        "default_country_crystal_court": (
            "晶",
            "水晶",
            "魔法",
            "魔导",
            "奥术",
            "学院",
            "辉钢",
            "crystal",
            "arcane",
            "magic",
        ),
        "default_country_sky_bastion": (
            "空",
            "天空",
            "浮空",
            "云",
            "飞空艇",
            "星",
            "sky",
            "floating",
            "airship",
            "cloud",
        ),
        "default_country_rune_archive": (
            "符文",
            "档案",
            "书",
            "知识",
            "学者",
            "图书",
            "记忆",
            "archive",
            "rune",
            "book",
            "lore",
        ),
        "default_country_crown_gate": (
            "王",
            "王国",
            "皇",
            "帝国",
            "贵族",
            "王室",
            "crown",
            "kingdom",
            "empire",
        ),
        "default_country_seat": (
            "首都",
            "都城",
            "王庭",
            "国家",
            "政权",
            "capital",
            "seat",
        ),
    }
    DEFAULT_GENERATED_NAME_POOL = [
        "阿古斯",
        "德罗斯",
        "克里森提亚",
        "纳拉",
        "托伦",
        "阿加尔塔",
        "杜诺瓦",
        "克林斯",
        "涅西斯",
        "瓦利卡",
        "阿刻戎",
        "多玛",
        "寇凡德",
        "佩西亚",
        "维莱亚",
        "阿奎莱亚",
        "恩德尔",
        "克伽",
        "彭博尔",
        "乌里安",
        "阿斯特莱德",
        "恩提吉亚",
        "奎维拉",
        "普拉提亚",
        "乌里安",
        "阿瓦隆",
        "费洛尔",
        "拉卡里亚",
        "瑞尔德",
        "希里亚",
        "埃尔萨",
        "伽拉忒亚",
        "雷加利亚",
        "萨拉扎",
        "仙那度",
        "奥斯卡拉",
        "吉扎尔",
        "伦迪尼乌姆",
        "塞穆尔",
        "亚历山德里亚",
        "巴别",
        "加拉菲斯",
        "罗断顿",
        "索特拉",
        "伊西拉",
        "布尔戈",
        "杰瑞瓦",
        "马拉巴",
        "塔尔塔洛斯",
        "伊提亚",
        "布里冈德",
        "喀迈斯",
        "迈吉多",
        "特里西亚",
        "尤德福特",
        "达格达",
        "卡利巴",
        "梅佳拉",
        "图勒",
        "泽普洛",
    ]
    STYLE_PRESETS = {
        "sepia_parchment": {
            "artPack": "nortantis",
            "landColor": "173,157,106,255",
            "regionBaseColor": "176,151,102,255",
            "oceanColor": "214,203,171,255",
            "riverColor": "51,46,30,255",
            "roadColor": "0,0,0,255",
            "textColor": "0,0,0,255",
            "coastlineColor": "0,0,0,255",
            "coastShadingColor": "86,78,53,65",
            "oceanShadingColor": "65,61,48,87",
            "oceanWavesColor": "103,96,79,204",
            "regionBoundaryColor": "0,0,0,255",
            "borderColor": "173,157,106,255",
            "frayedBorderColor": "51,46,31,255",
            "boldBackgroundColor": "#cfc4a3",
            "generateBackground": True,
            "generateBackgroundFromTexture": False,
            "solidColorBackground": False,
            "backgroundRandomSeed": 427953844,
            "frayedBorderSeed": 427953844,
            "regionsRandomSeed": 427953844,
            "lineStyle": "SplinesWithSmoothedCoastlines",
            "coastlineWidth": 2.7,
            "coastShadingLevel": 0,
            "oceanShadingLevel": 13,
            "oceanWavesType": "ConcentricWaves",
            "concentricWaveCount": 3,
            "fadeConcentricWaves": True,
            "jitterToConcentricWaves": True,
            "brokenLinesForConcentricWaves": True,
            "oceanWavesLevel": 25,
            "drawOceanEffectsInLakes": True,
            "drawRegionColors": True,
            "drawRegionBoundaries": True,
            "regionBoundaryStyleType": "Solid",
            "regionBoundaryWidth": 2.7,
            "drawBorder": True,
            "borderType": "lines",
            "borderWidth": 135,
            "borderPosition": "Outside_map",
            "borderColorOption": "Ocean_color",
            "drawGrunge": True,
            "drawBoldBackground": False,
            "frayedBorder": True,
            "frayedBorderSize": 13,
            "frayedBorderBlurLevel": 134,
            "grungeWidth": 1406,
            "roadStyleType": "Dashes",
            "roadWidth": 2.7,
            "mountainScale": 1.2,
            "hillScale": 1.2,
            "duneScale": 1.2,
            "treeHeightScale": 0.4,
            "cityScale": 1.2,
            "cityProbability": 0.0,
            "titleFontSize": 38,
            "regionFontSize": 21,
            "mountainRangeFontSize": 15,
            "otherMountainsFontSize": 12,
            "citiesFontSize": 12,
            "riverFontSize": 10,
            "edgeLandToWaterProbability": 0.33,
            "centerLandToWaterProbability": 0.67,
        },
        "classic_parchment": {
            "artPack": "nortantis",
            "landColor": "#c8b887",
            "oceanColor": "#8ca7ad",
            "riverColor": "#658b9a",
            "roadColor": "#6f5238",
            "textColor": "#3b2518",
            "drawRegionColors": True,
            "drawRegionBoundaries": True,
        },
    }

    def __init__(self, config: NortantisMapRendererConfig | None = None) -> None:
        self.config = config or NortantisMapRendererConfig.from_env()
        self.semantic_maps = SemanticMapManager()
        self.custom_images_dir = (
            self.config.custom_images_dir
            or self.config.project_dir / "assets" / "nortantis_custom"
        ).resolve()
        self.icon_catalog_dir = (
            self.config.icon_catalog_dir
            or self.custom_images_dir / "world_wonders"
        ).resolve()
        self.icon_registry = (
            MapIconRegistry.from_root(self.icon_catalog_dir)
            if self.config.wonder_icons_enabled
            else MapIconRegistry()
        )

    def build_brief(
        self,
        world_state: WorldState,
        *,
        output_path: str | Path,
        settings_path: str | Path | None = None,
        discovered_only: bool = True,
    ) -> dict:
        all_locations = [
            location
            for location in self._visible_locations(world_state, discovered_only=discovered_only)
            if not self._is_redundant_continent_label(world_state, location)
        ]
        locations, hidden_country_anchors = self._map_label_locations(all_locations)
        coordinates = self._normalized_coordinates(world_state, all_locations)
        for hidden_name, country_name in hidden_country_anchors.items():
            if country_name in coordinates:
                coordinates[hidden_name] = coordinates[country_name]
        if self.icon_registry:
            self.icon_registry.materialize_custom_pack(
                self.custom_images_dir,
                group_id=self.config.wonder_icon_group,
                encoded_width=self.config.wonder_icon_width,
            )
        country_icon_assignments = self._country_icon_assignments(locations)
        labels = [self._title_label(world_state)]
        custom_icon_count = 0
        for location in locations:
            x, y = coordinates[location.name]
            label_type = self._label_type(location)
            preference = self._location_preference(location, label_type)
            country_icon_id = country_icon_assignments.get(location.name, "")
            custom_icon = (
                self._custom_icon(location, country_icon_id)
                if self._prepared_icon_overrides_country(location) or country_icon_id or self._raw_feature_type(location) != "country"
                else None
            )
            draw_icon = self._draw_icon(location)
            if self.icon_registry and self._raw_feature_type(location) == "country" and custom_icon is None:
                draw_icon = False
            if custom_icon is not None and location.draw_icon is not False:
                draw_icon = True
            label = {
                "text": location.name,
                "type": label_type,
                "terrain": location.terrain,
                "tags": location.tags,
                "featureType": self._feature_type(location),
                "positionHint": location.position_hint,
                "relativeTo": location.relative_to,
                "relativePosition": location.relative_position,
                "preference": preference,
                "drawIcon": draw_icon,
                "snapToLand": True,
                "x": x,
                "y": y,
            }
            if custom_icon is not None and draw_icon:
                label.update(
                    {
                        "iconId": custom_icon.icon_id,
                        "iconArtPack": "custom",
                        "iconGroup": self.config.wonder_icon_group,
                        "iconName": custom_icon.icon_id,
                        "iconScale": self._custom_icon_scale(custom_icon),
                        "iconBaseWidth": self.config.wonder_icon_width,
                        "iconAspectRatio": round(custom_icon.aspect_ratio, 4),
                        "iconPlaceKind": custom_icon.place_kind,
                        "iconPreferredTerrain": list(custom_icon.preferred_terrain),
                        "iconPlacement": custom_icon.placement,
                        "iconAnchorMode": custom_icon.anchor_mode,
                        "iconRenderType": custom_icon.nortantis_icon_type,
                        "iconLabelOffset": self._custom_icon_label_offset(custom_icon),
                        "iconLabelLockBelow": custom_icon.place_kind == "default_country_anchor",
                    }
                )
                custom_icon_count += 1
            labels.append(label)

        roads = []
        for route in self._visible_routes(world_state, discovered_only=discovered_only):
            path = self._route_path(route, coordinates)
            if len(path) >= 2:
                roads.append(
                    {
                        "route_id": route.route_id,
                        "terrain": route.terrain,
                        "distance_days": route.distance_days,
                        "threat_level": route.default_threat_level.value,
                        "path": path,
                    }
                )

        style = self._style_brief()
        # A same-root local anchor may be hidden as text while still helping
        # Nortantis shape the country it belongs to.
        political_regions = self._political_regions(world_state, all_locations, coordinates)
        # Nortantis uses regionCount as both a political-region and tectonic/geology
        # knob. Let FU-GM political complexity influence geology, while staying
        # within Nortantis' native generated-region range.
        region_count = max(2, min(max(self.config.region_count, len(political_regions)), 20))
        brief = {
            "outputPath": str(Path(output_path)),
            "settingsPath": str(Path(settings_path)) if settings_path else "",
            "manifestPath": str(Path(output_path).with_suffix(".layout.json")),
            "semanticGridWidth": self.semantic_maps.GRID_WIDTH,
            "semanticGridHeight": self.semantic_maps.GRID_HEIGHT,
            "seed": self._seed_for_world(world_state),
            "fontFamily": self.config.font_family,
            "fontFile": str(self.config.font_file) if self.config.font_file else "",
            "landShape": self.config.land_shape,
            "worldSize": self.config.world_size,
            "regionCount": region_count,
            "generatedWidth": self.config.generated_width,
            "generatedHeight": self.config.generated_height,
            "resolution": self.config.resolution,
            "customImagesPath": str(self.custom_images_dir) if self.icon_registry else "",
            "terrainSeedAttempts": self.config.terrain_seed_attempts,
            "minCityHopDistance": self.config.min_city_hop_distance,
            "drawText": True,
            "drawRoads": bool(roads),
            "generateRandomCityRoads": False,
            "drawGridOverlay": False,
            "labels": labels,
            "roads": roads,
            "politicalRegions": political_regions,
            "generatedNamePool": [],
            "fu_gm_metadata": {
                "source": "WorldState.map_locations/map_routes",
                "rules_truth": "WorldState.map_routes",
                "style": self.config.map_style,
                "continent_name": self._continent_name(world_state),
                "needs_continent_name": not bool(self._continent_name(world_state)),
                "location_count": len(all_locations),
                "label_count": len(locations),
                "hidden_country_anchors": dict(hidden_country_anchors),
                "route_count": len(roads),
                "political_region_count": len(political_regions),
                "custom_icon_count": custom_icon_count,
            },
        }
        terrain_seed = self._previous_terrain_seed(world_state)
        if terrain_seed is not None:
            brief["terrainSeed"] = terrain_seed
        brief.update(style)
        return brief

    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> MapRenderResult:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = self._clean_slug(campaign_id)
        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{slug}_{timestamp}.png"
        settings_path = output_dir / f"{slug}_{timestamp}.nort"
        manifest_path = output_dir / f"{slug}_{timestamp}.layout.json"
        brief_path = output_dir / f"{slug}_{timestamp}.brief.json"
        brief = self.build_brief(world_state, output_path=output_path, settings_path=settings_path)
        brief["manifestPath"] = str(manifest_path)
        brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

        jar_path = self._ensure_jar()
        command = [
            self.config.java_exe,
            "-Xmx4g",
            "--enable-native-access=ALL-UNNAMED",
            "-cp",
            str(jar_path),
            self.EXPORTER_CLASS,
            "--brief",
            str(brief_path),
        ]
        completed = subprocess.run(
            command,
            cwd=self.config.nortantis_dir,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Nortantis map render failed.\n"
                f"Command: {' '.join(command)}\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
        if not output_path.exists():
            raise RuntimeError(f"Nortantis render completed but output image is missing: {output_path}")
        terrain_seed = self._settings_random_seed(settings_path)
        return MapRenderResult(
            renderer="nortantis",
            brief_path=str(brief_path),
            output_path=str(output_path),
            settings_path=str(settings_path),
            command=command,
            manifest_path=str(manifest_path) if manifest_path.exists() else "",
            stdout=completed.stdout,
            stderr=completed.stderr,
            seed=int(brief["seed"]),
            terrain_seed=terrain_seed,
        )

    def _ensure_jar(self) -> Path:
        jar_path = self.config.jar_path or (self.config.nortantis_dir / "build" / "libs" / "Nortantis.jar")
        if jar_path.exists():
            return jar_path
        if not self.config.auto_build:
            raise FileNotFoundError(
                f"Nortantis jar 不存在：{jar_path}。请先在 {self.config.nortantis_dir} 执行 .\\gradlew.bat --no-daemon jar，"
                "或设置 FU_GM_NORTANTIS_AUTO_BUILD=1。"
            )
        gradlew = self.config.nortantis_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
        subprocess.run(
            [str(gradlew), "--no-daemon", "jar"],
            cwd=self.config.nortantis_dir,
            text=True,
            capture_output=True,
            timeout=self.config.timeout_seconds,
            check=True,
        )
        return jar_path

    def _visible_locations(self, world_state: WorldState, *, discovered_only: bool) -> list[MapLocation]:
        locations = [
            location
            for location in world_state.map_locations.values()
            if location.discovered or not discovered_only
        ]
        return sorted(locations, key=lambda item: (item.y, item.x, item.name))

    def _style_brief(self) -> dict:
        style_name = self.config.map_style.strip() or "sepia_parchment"
        preset = self.STYLE_PRESETS.get(style_name, self.STYLE_PRESETS["sepia_parchment"])
        style = dict(preset)
        env_overrides = {
            "FU_GM_NORTANTIS_LAND_COLOR": "landColor",
            "FU_GM_NORTANTIS_REGION_BASE_COLOR": "regionBaseColor",
            "FU_GM_NORTANTIS_OCEAN_COLOR": "oceanColor",
            "FU_GM_NORTANTIS_RIVER_COLOR": "riverColor",
            "FU_GM_NORTANTIS_ROAD_COLOR": "roadColor",
            "FU_GM_NORTANTIS_TEXT_COLOR": "textColor",
            "FU_GM_NORTANTIS_BORDER_COLOR": "borderColor",
        }
        for env_key, brief_key in env_overrides.items():
            value = os.environ.get(env_key, "").strip()
            if value:
                style[brief_key] = value
        font_size_overrides = {
            "FU_GM_NORTANTIS_TITLE_FONT_SIZE": "titleFontSize",
            "FU_GM_NORTANTIS_REGION_FONT_SIZE": "regionFontSize",
            "FU_GM_NORTANTIS_MOUNTAIN_RANGE_FONT_SIZE": "mountainRangeFontSize",
            "FU_GM_NORTANTIS_OTHER_MOUNTAINS_FONT_SIZE": "otherMountainsFontSize",
            "FU_GM_NORTANTIS_CITIES_FONT_SIZE": "citiesFontSize",
            "FU_GM_NORTANTIS_RIVER_FONT_SIZE": "riverFontSize",
        }
        overridden_font_keys: set[str] = set()
        for env_key, brief_key in font_size_overrides.items():
            value = os.environ.get(env_key, "").strip()
            if value:
                style[brief_key] = max(1, int(value))
                overridden_font_keys.add(brief_key)
        self._scale_style_for_canvas(style, overridden_font_keys)
        return style

    def _scale_style_for_canvas(self, style: dict, overridden_font_keys: set[str]) -> None:
        """Keep debug/thumbnail renders visually comparable to full-size maps."""

        width_scale = self.config.generated_width / 4096
        height_scale = self.config.generated_height / 2531
        scale = min(width_scale, height_scale)
        if abs(scale - 1.0) < 0.03:
            return
        minimums = {
            "titleFontSize": 18,
            "regionFontSize": 10,
            "mountainRangeFontSize": 8,
            "otherMountainsFontSize": 7,
            "citiesFontSize": 7,
            "riverFontSize": 6,
        }
        for key, minimum in minimums.items():
            if key in overridden_font_keys or key not in style:
                continue
            style[key] = max(minimum, int(round(float(style[key]) * scale)))
        line_minimums = {
            "coastlineWidth": 0.85,
            "regionBoundaryWidth": 0.9,
            "roadWidth": 0.9,
        }
        for key, minimum in line_minimums.items():
            if key in style:
                style[key] = round(max(minimum, float(style[key]) * scale), 2)
        integer_minimums = {
            "borderWidth": 46,
            "frayedBorderSize": 6,
            "frayedBorderBlurLevel": 58,
            "grungeWidth": 520,
            "oceanWavesLevel": 11,
        }
        for key, minimum in integer_minimums.items():
            if key in style:
                style[key] = max(minimum, int(round(float(style[key]) * scale)))
        alpha_scale = max(0.45, min(1.0, scale ** 0.75))
        for key in ("coastShadingColor", "oceanShadingColor", "oceanWavesColor"):
            if key in style:
                style[key] = self._scaled_rgba_alpha(str(style[key]), alpha_scale)
        line_alpha_scale = max(0.62, min(1.0, scale ** 0.5))
        for key in ("coastlineColor", "regionBoundaryColor", "roadColor"):
            if key in style:
                style[key] = self._scaled_rgba_alpha(str(style[key]), line_alpha_scale)

    @staticmethod
    def _scaled_rgba_alpha(value: str, alpha_scale: float) -> str:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            return value
        try:
            red, green, blue = (int(parts[0]), int(parts[1]), int(parts[2]))
            alpha = int(parts[3])
        except ValueError:
            return value
        scaled_alpha = max(0, min(255, int(round(alpha * alpha_scale))))
        return f"{red},{green},{blue},{scaled_alpha}"

    def _visible_routes(self, world_state: WorldState, *, discovered_only: bool) -> list[MapRouteEdge]:
        routes = [
            route
            for route in world_state.map_routes.values()
            if route.discovered or not discovered_only
        ]
        return sorted(routes, key=lambda item: item.route_id)

    def _political_regions(
        self,
        world_state: WorldState,
        locations: list[MapLocation],
        coordinates: dict[str, tuple[float, float]],
    ) -> list[dict]:
        anchors_by_power: dict[str, list[dict]] = {}
        powers = self._known_political_powers(world_state)
        for location in locations:
            power = self._location_power(location, powers)
            if not power or location.name not in coordinates:
                continue
            x, y = coordinates[location.name]
            anchors_by_power.setdefault(power, []).append(
                {
                    "name": location.name,
                    "x": x,
                    "y": y,
                    "terrain": location.terrain,
                }
            )

        return [
            {
                "name": power,
                "color": self._political_color(index),
                "anchors": anchors,
            }
            for index, (power, anchors) in enumerate(sorted(anchors_by_power.items()))
            if anchors
        ]

    def _known_political_powers(self, world_state: WorldState) -> list[str]:
        powers: list[str] = []
        for source in (
            getattr(world_state.world_profile, "kingdoms", {}),
            getattr(world_state.world_profile, "factions", {}),
        ):
            for name in source:
                if name and name not in powers:
                    powers.append(name)
        if world_state.world_sheet is not None:
            for name in world_state.world_sheet.factions:
                if name and name not in powers:
                    powers.append(name)
        return sorted(powers, key=len, reverse=True)

    def _location_power(self, location: MapLocation, powers: list[str]) -> str:
        if location.faction:
            return location.faction
        haystack = " ".join(
            [
                location.name,
                location.description,
                location.terrain,
                *location.tags,
                *location.notes,
            ]
        )
        for power in powers:
            if power in haystack:
                return power
            short = power.removesuffix("帝国").removesuffix("王国").removesuffix("联邦").removesuffix("公国")
            if len(short) >= 2 and short in haystack:
                return power
        return ""

    def _political_color(self, index: int) -> str:
        palette = (
            "#b09766",
            "#ab986d",
            "#b0a76a",
            "#a19160",
            "#b09766",
            "#a38d67",
            "#a8966d",
            "#b09766",
        )
        return palette[index % len(palette)]

    def _normalized_coordinates(
        self,
        world_state: WorldState,
        locations: list[MapLocation],
    ) -> dict[str, tuple[float, float]]:
        if not locations:
            return {}
        anchors = {
            "north": (0.50, 0.20), "northeast": (0.76, 0.25), "east": (0.80, 0.50),
            "southeast": (0.76, 0.73), "south": (0.50, 0.78), "southwest": (0.24, 0.73),
            "west": (0.20, 0.50), "northwest": (0.24, 0.25), "center": (0.50, 0.50),
        }
        relative_offsets = {
            "north": (0.0, -0.17), "northeast": (0.15, -0.14), "east": (0.18, 0.0),
            "southeast": (0.15, 0.14), "south": (0.0, 0.17), "southwest": (-0.15, 0.14),
            "west": (-0.18, 0.0), "northwest": (-0.15, -0.14), "center": (0.0, 0.0),
        }
        min_x = min(location.x for location in locations)
        max_x = max(location.x for location in locations)
        min_y = min(location.y for location in locations)
        max_y = max(location.y for location in locations)
        padding = 0.10

        def normalize(value: int, low: int, high: int) -> float:
            if high == low:
                return 0.5
            ratio = (value - low) / (high - low)
            return round(padding + ratio * (1.0 - padding * 2), 4)

        fallback = ((0.30, 0.34), (0.50, 0.32), (0.70, 0.36), (0.32, 0.58), (0.55, 0.57), (0.72, 0.62), (0.48, 0.74))
        has_coordinate_span = min_x != max_x or min_y != max_y
        result: dict[str, tuple[float, float]] = {}
        for index, location in enumerate(locations):
            semantic_position = self.semantic_maps.normalized_position(
                world_state,
                location,
            )
            if semantic_position is not None:
                result[location.name] = semantic_position
                continue
            hint = str(location.position_hint or "").strip().lower()
            if hint in anchors:
                result[location.name] = anchors[hint]
            elif has_coordinate_span:
                result[location.name] = (normalize(location.x, min_x, max_x), normalize(location.y, min_y, max_y))
            else:
                result[location.name] = fallback[index % len(fallback)]

        for _ in range(2):
            for location in locations:
                reference = result.get(location.relative_to)
                direction = str(location.relative_position or "").strip().lower()
                if reference is None or direction not in relative_offsets:
                    continue
                dx, dy = relative_offsets[direction]
                result[location.name] = (round(min(0.88, max(0.12, reference[0] + dx)), 4), round(min(0.86, max(0.14, reference[1] + dy)), 4))

        placed: list[tuple[float, float]] = []
        nudges = ((0.12, 0.0), (-0.12, 0.0), (0.0, 0.12), (0.0, -0.12), (0.10, 0.10), (-0.10, 0.10))
        for index, location in enumerate(locations):
            x, y = result[location.name]
            attempt = 0
            while any((x - px) ** 2 + (y - py) ** 2 < 0.012 for px, py in placed) and attempt < len(nudges):
                dx, dy = nudges[(index + attempt) % len(nudges)]
                x, y = min(0.88, max(0.12, x + dx)), min(0.86, max(0.14, y + dy))
                attempt += 1
            result[location.name] = (round(x, 4), round(y, 4))
            placed.append((x, y))
        return result

    def _route_path(self, route: MapRouteEdge, coordinates: dict[str, tuple[float, float]]) -> list[dict[str, float]]:
        ordered_names = [route.origin]
        for segment in route.segments:
            if segment.region in coordinates and segment.region not in ordered_names:
                ordered_names.append(segment.region)
        if route.destination not in ordered_names:
            ordered_names.append(route.destination)

        path: list[dict[str, float]] = []
        for name in ordered_names:
            if name not in coordinates:
                continue
            x, y = coordinates[name]
            point = {"name": name, "x": x, "y": y}
            if not path or path[-1] != point:
                path.append(point)
        return path

    def _title_label(self, world_state: WorldState) -> dict:
        title = self._continent_name(world_state) or "未命名大陆"
        return {
            "text": title,
            "type": "Title",
            "snapToLand": False,
            "snapToOcean": True,
            "x": 0.5,
            "y": 0.82,
        }

    def _label_type(self, location: MapLocation) -> str:
        feature_type = self._feature_type(location)
        if feature_type in {"settlement", "fortress", "country"}:
            return "City"
        if self._draw_icon(location) and feature_type not in {
            "archipelago",
            "ocean",
            "inland_sea",
            "lake",
            "forest",
            "mountain_range",
            "coast",
        }:
            return "City"
        return "Region"

    def _location_preference(self, location: MapLocation, label_type: str) -> str:
        icon = self._icon_spec(location)
        if icon is not None:
            if icon.placement == "ocean":
                return "ocean"
            if icon.placement == "island":
                return "archipelago"
            terrain = set(icon.preferred_terrain)
            if terrain & {"sky_island", "sky", "floating"}:
                return "sky_island"
            if terrain & {"forest", "jungle", "woods", "woodland"}:
                return "forest"
            if terrain & {"mountain", "mountain_foothill", "snow", "ice", "glacier"}:
                return "mountain"
            if terrain & {"coast", "sea", "harbor", "bay"}:
                return "coast"
            if terrain & {"inland_lake", "lake"}:
                if "lake" not in icon.place_kind:
                    return "lake_shore"
                return "lake"
        return {
            "archipelago": "archipelago",
            "ocean": "ocean",
            "inland_sea": "inland_sea",
            "lake": "lake",
            "forest": "forest",
            "mountain_range": "mountain",
            "coast": "coast",
        }.get(self._feature_type(location), "land")

    def _feature_type(self, location: MapLocation) -> str:
        icon = self._icon_spec(location)
        raw_feature = self._raw_feature_type(location)
        if icon is not None and icon.placement == "ocean":
            return "ocean"
        if icon is not None and icon.placement == "island" and raw_feature in {"", "region", "landmark", "coast"}:
            return "archipelago"
        if icon is not None and raw_feature in {"", "region", "landmark"}:
            terrain = set(icon.preferred_terrain)
            if terrain & {"sky_island", "sky", "floating"}:
                return "sky_island"
            if terrain & {"forest", "jungle", "woods", "woodland"}:
                return "forest"
            if terrain & {"mountain", "mountain_foothill", "snow", "ice", "glacier"}:
                return "mountain_range"
            if terrain & {"inland_lake", "lake"}:
                return "lake"
            if terrain & {"coast", "sea", "harbor", "bay"}:
                return "coast"
        return raw_feature

    def _draw_icon(self, location: MapLocation) -> bool:
        if self._raw_feature_type(location) == "country":
            # Older Session 0 prompts wrote countries with draw_icon=false.
            # Treat a country label as its political/seat-of-power anchor so it remains visible.
            return True
        if location.draw_icon is not None:
            return location.draw_icon
        return self._feature_type(location) in {"settlement", "fortress"}

    def _custom_icon(self, location: MapLocation, country_icon_id: str = "") -> MapIconSpec | None:
        if not self.icon_registry:
            return None
        if location.draw_icon is False and self._raw_feature_type(location) != "country":
            return None
        return self._icon_spec(location, country_icon_id=country_icon_id)

    def _custom_icon_scale(self, custom_icon: MapIconSpec) -> float:
        scale = custom_icon.default_scale * self.config.wonder_icon_scale_multiplier
        return round(max(0.1, scale), 3)

    def _custom_icon_label_offset(self, custom_icon: MapIconSpec) -> float:
        if custom_icon.place_kind == "default_country_anchor":
            return 20.0
        return 14.0

    def _icon_spec(self, location: MapLocation, *, country_icon_id: str = "") -> MapIconSpec | None:
        if not self.icon_registry:
            return None
        icon = self.icon_registry.resolve(icon_id=location.icon_id)
        if icon is not None:
            return icon
        prepared_country_icon = self._prepared_country_icon(location)
        if prepared_country_icon is not None:
            return prepared_country_icon
        if self._raw_feature_type(location) == "country":
            country_icon = self.icon_registry.resolve(icon_id=country_icon_id or self.DEFAULT_COUNTRY_ICON_ID)
            if country_icon is not None:
                return country_icon
        icon = self.icon_registry.resolve(semantic_name=location.name)
        if icon is not None:
            return icon
        from fu_gm.prepared_locations import prepared_location_by_name

        prepared = prepared_location_by_name(location.name)
        if prepared is None:
            return None
        return self.icon_registry.resolve(semantic_name=prepared.icon_name)

    def _prepared_country_icon(self, location: MapLocation) -> MapIconSpec | None:
        if not self.icon_registry or self._raw_feature_type(location) != "country":
            return None
        from fu_gm.prepared_locations import prepared_location_by_name

        prepared = prepared_location_by_name(location.name)
        if prepared is None:
            return None
        icon = self.icon_registry.resolve(semantic_name=prepared.icon_name)
        if icon is None:
            return None
        if icon.place_kind in {"prepared_mobile_city"}:
            return icon
        return None

    def _prepared_icon_overrides_country(self, location: MapLocation) -> bool:
        return self._prepared_country_icon(location) is not None

    def _country_icon_assignments(self, locations: Sequence[MapLocation]) -> dict[str, str]:
        if not self.icon_registry:
            return {}

        available = [
            icon_id
            for icon_id in self.DEFAULT_COUNTRY_ICON_IDS
            if self.icon_registry.resolve(icon_id=icon_id) is not None
        ]
        if not available:
            return {}

        assignments: dict[str, str] = {}
        used: set[str] = set()
        for location in locations:
            if self._raw_feature_type(location) != "country":
                continue
            prepared_icon = self._prepared_country_icon(location)
            if prepared_icon is not None:
                assignments[location.name] = prepared_icon.icon_id
                continue
            explicit = self.icon_registry.resolve(icon_id=location.icon_id)
            if explicit is None:
                continue
            assignments[location.name] = explicit.icon_id
            used.add(explicit.icon_id)

        for location in locations:
            if self._raw_feature_type(location) != "country" or location.name in assignments:
                continue
            if self._prepared_icon_overrides_country(location):
                continue
            next_icon = self._best_country_icon_id(location, available, used)
            if not next_icon:
                continue
            assignments[location.name] = next_icon
            used.add(next_icon)
        return assignments

    def _best_country_icon_id(self, location: MapLocation, available: Sequence[str], used: set[str]) -> str:
        unused = [icon_id for icon_id in available if icon_id not in used]
        if not unused:
            return ""
        scored = [
            (self._country_icon_style_score(location, icon_id), -self.DEFAULT_COUNTRY_ICON_IDS.index(icon_id), icon_id)
            for icon_id in unused
        ]
        best_score, _, best_icon_id = max(scored)
        if best_score > 0:
            return best_icon_id
        return unused[0]

    def _country_icon_style_score(self, location: MapLocation, icon_id: str) -> int:
        text = self._country_style_text(location)
        return sum(1 for hint in self.COUNTRY_ICON_STYLE_HINTS.get(icon_id, ()) if hint in text)

    def _country_style_text(self, location: MapLocation) -> str:
        return " ".join(
            [
                location.name,
                location.faction,
                location.terrain,
                location.description,
                location.position_hint,
                location.relative_position,
                *location.tags,
                *location.notes,
            ]
        ).lower()

    def _raw_feature_type(self, location: MapLocation) -> str:
        return str(location.feature_type or "region").strip().lower()

    def _location_text(self, location: MapLocation) -> str:
        return " ".join(
            [
                location.name,
                location.terrain,
                location.description,
                *location.tags,
                *location.notes,
            ]
        ).lower()

    def _seed_for_world(self, world_state: WorldState) -> int:
        saved_seed = self._previous_map_seed(world_state)
        if saved_seed is not None:
            return saved_seed

        # Once a map exists its seed is persisted in the visual event. Before
        # the first render, derive terrain only from the map identity so adding
        # or moving locations does not silently replace the whole continent.
        seed_source = self._continent_name(world_state).strip() or "fu-gm"
        value = 0
        for char in seed_source:
            value = (value * 131 + ord(char)) % 2_147_483_647
        return value or 424242

    @staticmethod
    def _previous_map_seed(world_state: WorldState) -> int | None:
        for event in reversed(world_state.memory_events):
            if str(getattr(event, "kind", "") or "") != "world_map_visual":
                continue
            payload = dict(getattr(event, "payload", {}) or {})
            raw_seed = payload.get("map_seed")
            try:
                seed = int(raw_seed)
            except (TypeError, ValueError):
                seed = 0
            if seed > 0:
                return seed

            # Compatibility with maps generated before map_seed was recorded.
            brief_path = str(payload.get("brief_path") or "").strip()
            if not brief_path:
                continue
            try:
                brief = json.loads(Path(brief_path).expanduser().read_text(encoding="utf-8"))
                seed = int(brief.get("seed") or 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if seed > 0:
                return seed
        return None

    @staticmethod
    def _previous_terrain_seed(world_state: WorldState) -> int | None:
        for event in reversed(world_state.memory_events):
            if str(getattr(event, "kind", "") or "") != "world_map_visual":
                continue
            payload = dict(getattr(event, "payload", {}) or {})
            raw_seed = payload.get("terrain_seed")
            try:
                return int(raw_seed)
            except (TypeError, ValueError):
                pass

            settings_path = str(payload.get("settings_path") or "").strip()
            seed = NortantisMapRenderer._settings_random_seed(
                Path(settings_path).expanduser()
            )
            if seed:
                return seed
        return None

    @staticmethod
    def _settings_random_seed(settings_path: Path) -> int:
        if not settings_path or not settings_path.is_file():
            return 0
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            return int(settings.get("randomSeed") or 0)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0

    def _continent_name(self, world_state: WorldState) -> str:
        if world_state.world_sheet is not None and getattr(world_state.world_sheet, "continent_name", ""):
            return world_state.world_sheet.continent_name.strip()
        return getattr(world_state.world_profile, "continent_name", "").strip()

    def _is_redundant_continent_label(self, world_state: WorldState, location: MapLocation) -> bool:
        continent_name = self._continent_name(world_state)
        if not continent_name or location.name.strip() != continent_name:
            return False
        feature_type = str(location.feature_type or "").strip().lower()
        return feature_type in {"", "region", "country", "continent", "landmass", "landmark"}

    def _map_label_locations(
        self,
        locations: Sequence[MapLocation],
    ) -> tuple[list[MapLocation], dict[str, str]]:
        countries = [
            location
            for location in locations
            if self._raw_feature_type(location) == "country"
        ]
        hidden_country_anchors: dict[str, str] = {}
        visible: list[MapLocation] = []
        for location in locations:
            original_feature = str(location.feature_type or "").strip().lower()
            matching_country = next(
                (
                    country
                    for country in countries
                    if country.name != location.name
                    and self._country_name_root(country.name) == location.name.strip()
                    and (not location.faction or location.faction == country.name)
                ),
                None,
            )
            if matching_country is not None and original_feature in {"", "region"}:
                hidden_country_anchors[location.name] = matching_country.name
                continue
            visible.append(location)
        return visible, hidden_country_anchors

    @staticmethod
    def _country_name_root(name: str) -> str:
        value = str(name or "").strip()
        for suffix in (
            "神圣王国",
            "人民共和国",
            "共和国",
            "保护国",
            "联合王国",
            "帝国",
            "王国",
            "联邦",
            "公国",
            "城邦",
            "部落",
        ):
            if value.endswith(suffix) and len(value) > len(suffix):
                return value[: -len(suffix)].strip()
        return value

    def _clean_slug(self, value: str) -> str:
        keep: list[str] = []
        for char in value or "default":
            if char.isascii() and (char.isalnum() or char in "-_"):
                keep.append(char)
            elif "\u4e00" <= char <= "\u9fff":
                keep.append(char)
            else:
                keep.append("_")
        slug = "".join(keep).strip("_")
        return slug[:48] or "default"
