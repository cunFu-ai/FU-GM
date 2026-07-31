from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from fu_gm.config import ImageGenerationConfig
from fu_gm.components.map_renderer import MapRenderer
from fu_gm.components.semantic_map_manager import SemanticMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.image_client import ImageGenerationClient, ImageGenerationResult
from fu_gm.models import MemoryVisibility, WorldCreationProfile


class WorldMapImageManager:
    """Generates a human-facing world map visual for a campaign.

    The generated image is deliberately a visual artifact only. The route graph
    in WorldState remains the single source of truth for travel days, threats,
    and route choices.
    """

    MEMORY_KIND = "world_map_visual"

    def __init__(
        self,
        client: ImageGenerationClient | None = None,
        config: ImageGenerationConfig | None = None,
        *,
        renderer: MapRenderer | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.renderer = renderer
        self.semantic_maps = SemanticMapManager()

    def generate_if_ready(self, world_state: WorldState, *, campaign_id: str = "") -> ImageGenerationResult | None:
        world = world_state.world_profile
        if not world.completed:
            return None
        return self._generate(world_state, campaign_id=campaign_id)

    def generate_for_adventure(
        self,
        world_state: WorldState,
        *,
        campaign_id: str = "",
        force: bool = False,
    ) -> ImageGenerationResult | None:
        """Ensure the map exists before play, even when Session 0 was left incomplete."""

        return self._generate(world_state, campaign_id=campaign_id, force=force)

    def _generate(
        self,
        world_state: WorldState,
        *,
        campaign_id: str,
        force: bool = False,
    ) -> ImageGenerationResult | None:
        world = world_state.world_profile
        if not force and self._already_generated(world_state):
            return None
        campaign_slug = campaign_id or self._campaign_slug(world)
        if self.renderer is not None:
            map_result = self.renderer.render(world_state, campaign_id=campaign_slug)
            manifest_error = ""
            if map_result.manifest_path:
                try:
                    manifest = json.loads(
                        Path(map_result.manifest_path)
                        .expanduser()
                        .read_text(encoding="utf-8")
                    )
                    if not isinstance(manifest, dict):
                        raise ValueError("地图布局清单不是JSON对象。")
                    self.semantic_maps.apply_manifest(
                        world_state,
                        manifest,
                        manifest_path=map_result.manifest_path,
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    manifest_error = str(exc)
            world_signature = self.world_signature(world_state)
            result = ImageGenerationResult(
                model=map_result.renderer,
                prompt="Nortantis topology map render from WorldState.map_locations/map_routes.",
                output_path=map_result.output_path,
                raw_keys=[
                    "brief_path",
                    "settings_path",
                    "manifest_path",
                    "stdout",
                    "stderr",
                ],
            )
            renderer_payload = {
                "world_signature": world_signature,
                "renderer": map_result.renderer,
                "brief_path": map_result.brief_path,
                "settings_path": map_result.settings_path,
                "manifest_path": map_result.manifest_path,
                "command": list(map_result.command),
                "stdout": map_result.stdout[-2000:],
                "stderr": map_result.stderr[-2000:],
            }
            if manifest_error:
                renderer_payload["manifest_error"] = manifest_error
            if map_result.seed > 0:
                renderer_payload["map_seed"] = map_result.seed
            if map_result.terrain_seed:
                renderer_payload["terrain_seed"] = map_result.terrain_seed
            self._record_result(
                world_state,
                result,
                extra_payload=renderer_payload,
            )
            return result
        if self.client is None or self.config is None or not self.config.usable():
            return None
        world_signature = self.world_signature(world_state)
        prompt = self.build_prompt(world)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = f"world_map_{campaign_slug}_{timestamp}"
        result = self.client.create_image(
            prompt,
            output_dir=Path(self.config.output_dir),
            filename_prefix=prefix,
        )
        self._record_result(world_state, result, extra_payload={"world_signature": world_signature})
        return result

    def world_signature(self, world_state: WorldState) -> str:
        world = world_state.world_profile
        # "自定义地图" is derived workflow metadata. Older maps generated
        # before that metadata was persisted remain visually equivalent.
        signature_map_card = "" if world.map_card == "自定义地图" else world.map_card
        payload = {
            "continent_name": world.continent_name,
            "world_shape": world.world_shape,
            "map_card": signature_map_card,
            "starting_region": world.starting_region,
            "major_locations": world.major_locations,
            "kingdoms": world.kingdoms,
            "factions": world.factions,
            "map_locations": {
                name: asdict(location)
                for name, location in sorted(world_state.map_locations.items())
            },
            "map_routes": {
                name: asdict(route)
                for name, route in sorted(world_state.map_routes.items())
            },
            "semantic_map": {
                "version": world_state.semantic_map.version,
                "grid_width": world_state.semantic_map.grid_width,
                "grid_height": world_state.semantic_map.grid_height,
                "terrain_rows": list(world_state.semantic_map.terrain_rows),
                "location_cells": dict(
                    sorted(world_state.semantic_map.location_cells.items())
                ),
                "location_points": dict(
                    sorted(world_state.semantic_map.location_points.items())
                ),
                "revision": world_state.semantic_map.revision,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def has_current_map(self, world_state: WorldState) -> bool:
        event = next(
            (event for event in reversed(world_state.memory_events) if event.kind == self.MEMORY_KIND),
            None,
        )
        if event is None:
            return False
        recorded = str(event.payload.get("world_signature") or "")
        # Older saves predate signatures; retain their existing map until the
        # user explicitly regenerates it rather than surprising them on load.
        return not recorded or recorded == self.world_signature(world_state)

    def build_prompt(self, world: WorldCreationProfile) -> str:
        sections = [
            "为一场日式桌面角色扮演游戏生成一张世界地图原画。",
            "用途：给人类玩家提供沉浸感；不要让画面承担规则计算。",
            "地图前提：当前项目使用 Nortantis/类地球大陆地图；请表现大陆、海岸、内海、山脉、河流和近海岛屿，不要绘制平面世界、环形世界、云海浮岛宇宙或非大陆拓扑。",
            "强制要求：不要绘制方格、坐标轴、比例尺、旅行日数字、测距线、六角格或战棋网格。",
            "构图：复古羊皮纸 / JRPG 世界地图 / 手绘地貌图标 / 适合群聊展示。",
            "文字：尽量不要生成大段可读文字；地点可用图标、旗帜、废墟、森林、山脉、飞艇航线等视觉元素表达。",
        ]
        facts = self._world_facts(world)
        if facts:
            sections.append("世界设定要素：\n" + "\n".join(f"- {fact}" for fact in facts))
        sections.append(
            "请突出世界第一印象和地貌关系。路线在后台 Graph 中以角色徒步一天可走的距离（一个旅行日）为单位；不要从图片像素反推路线距离或威胁等级。"
        )
        return "\n".join(sections)

    def _world_facts(self, world: WorldCreationProfile) -> list[str]:
        facts: list[str] = []
        map_card = "" if world.map_card == "自定义地图" else world.map_card
        for label, value in (
            ("战役标题", world.campaign_title),
            ("世界风格", world.world_style),
            ("地图形式", map_card),
            ("魔法与科技的地位", world.magic_tech_role),
            ("小队原型", world.group_concept),
            ("起始地区", world.starting_region),
        ):
            if value:
                facts.append(f"{label}：{value}")
        facts.extend(self._dict_facts("主要地点", world.major_locations))
        facts.extend(self._dict_facts("王国/国家", world.kingdoms))
        facts.extend(self._dict_facts("阵营", world.factions))
        facts.extend(f"重大历史事件：{value}" for value in world.historical_events[:6])
        facts.extend(f"世界谜团：{value}" for value in world.mysteries[:6])
        facts.extend(f"世界威胁：{value}" for value in world.world_threats[:6])
        if world.core_themes:
            facts.append("核心主题：" + "、".join(world.core_themes[:6]))
        return facts[:32]

    def _dict_facts(self, label: str, values: dict[str, str]) -> list[str]:
        return [f"{label}【{name}】：{detail}" for name, detail in list(values.items())[:8]]

    def _already_generated(self, world_state: WorldState) -> bool:
        # A failed or stale renderer attempt must not permanently suppress recovery.
        return self.has_current_map(world_state)

    def _record_result(
        self,
        world_state: WorldState,
        result: ImageGenerationResult,
        *,
        extra_payload: dict | None = None,
    ) -> None:
        target = result.output_path or result.remote_url
        summary = f"世界地图原画已生成：{target}"
        payload = {
            "model": result.model,
            "output_path": result.output_path,
            "remote_url": result.remote_url,
            "revised_prompt": result.revised_prompt,
        }
        thumbnail_path = self._derive_thumbnail(result.output_path)
        if thumbnail_path:
            payload["thumbnail_path"] = thumbnail_path
        payload.update(extra_payload or {})
        world_state.record_memory_event(
            summary,
            kind=self.MEMORY_KIND,
            visibility=MemoryVisibility.PUBLIC,
            tags=["map", "visual", "image_generation"],
            source="WorldMapImageManager",
            payload=payload,
        )
        if world_state.world_sheet is not None and summary not in world_state.world_sheet.created_assets:
            world_state.world_sheet.created_assets.append(summary)

    def _campaign_slug(self, world: WorldCreationProfile) -> str:
        title = world.campaign_title or world.world_style or "campaign"
        keep = []
        for char in title:
            if char.isascii() and (char.isalnum() or char in "-_"):
                keep.append(char)
            elif "\u4e00" <= char <= "\u9fff":
                keep.append(char)
            else:
                keep.append("_")
        slug = "".join(keep).strip("_")
        return slug[:40] or "campaign"

    def _derive_thumbnail(self, output_path: str, *, max_width: int = 1280) -> str:
        if not output_path:
            return ""
        source = Path(output_path).expanduser()
        if not source.exists() or not source.is_file():
            return ""
        try:
            from PIL import Image
        except Exception:
            return ""
        try:
            with Image.open(source) as image:
                width, height = image.size
                if width <= max_width:
                    return str(source)
                ratio = max_width / float(width)
                target = source.with_name(f"{source.stem}_thumb{source.suffix}")
                resized = image.resize((max_width, max(1, int(height * ratio))), Image.Resampling.LANCZOS)
                resized.save(target)
                return str(target)
        except Exception:
            return ""
