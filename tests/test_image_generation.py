from __future__ import annotations

from pathlib import Path

from fu_gm.components.world_map_image_manager import WorldMapImageManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import ImageGenerationConfig
from fu_gm.image_client import ImageGenerationClient, ImageGenerationResult
from fu_gm.components.map_renderer import MapRenderResult, NortantisMapRendererConfig


ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {"data": [{"b64_json": ONE_PIXEL_PNG, "revised_prompt": "revised"}]}


class FakeImageClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_image(self, prompt: str, *, output_dir: str | Path | None = None, filename_prefix: str = "world_map"):
        self.calls += 1
        return ImageGenerationResult(
            model="gpt-image-2",
            prompt=prompt,
            output_path=str(Path(output_dir or ".") / f"{filename_prefix}.png"),
        )


class FakeMapRenderer:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.calls: list[str] = []

    def build_brief(self, world_state: WorldState, *, output_path: str | Path, settings_path: str | Path | None = None) -> dict:
        return {}

    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> MapRenderResult:
        self.calls.append(campaign_id)
        self.output_path.write_bytes(b"fake png")
        return MapRenderResult(
            renderer="nortantis",
            brief_path=str(self.output_path.with_suffix(".brief.json")),
            output_path=str(self.output_path),
            settings_path=str(self.output_path.with_suffix(".nort")),
            command=["java", "-jar", "Nortantis.jar"],
        )


class ValidPngMapRenderer(FakeMapRenderer):
    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> MapRenderResult:
        from PIL import Image

        self.calls.append(campaign_id)
        image = Image.new("RGB", (1600, 900), (220, 210, 180))
        image.save(self.output_path)
        return MapRenderResult(
            renderer="nortantis",
            brief_path=str(self.output_path.with_suffix(".brief.json")),
            output_path=str(self.output_path),
            settings_path=str(self.output_path.with_suffix(".nort")),
            command=["java", "-jar", "Nortantis.jar"],
        )


class FlakyMapRenderer(FakeMapRenderer):
    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> MapRenderResult:
        self.calls.append(campaign_id)
        if len(self.calls) == 1:
            raise RuntimeError("temporary renderer failure")
        self.output_path.write_bytes(b"fake png")
        return MapRenderResult(
            renderer="nortantis",
            brief_path=str(self.output_path.with_suffix(".brief.json")),
            output_path=str(self.output_path),
            settings_path=str(self.output_path.with_suffix(".nort")),
            command=["java", "-jar", "Nortantis.jar"],
        )


class AlwaysFailMapRenderer(FakeMapRenderer):
    def render(self, world_state: WorldState, *, campaign_id: str = "default") -> MapRenderResult:
        self.calls.append(campaign_id)
        raise RuntimeError("java missing")


def image_config(**overrides) -> ImageGenerationConfig:
    values = {
        "api_base_url": "https://example.test",
        "api_key": "sk-test",
        "model": "gpt-image-2",
        "enabled": True,
        "output_dir": "data/generated_images",
    }
    values.update(overrides)
    return ImageGenerationConfig(**values)


def test_image_generation_url_defaults_to_v1_images_endpoint() -> None:
    assert image_config(api_base_url="https://ai-pixel.online").image_generations_url() == (
        "https://ai-pixel.online/v1/images/generations"
    )
    assert image_config(api_base_url="https://ai-pixel.online/v1").image_generations_url() == (
        "https://ai-pixel.online/v1/images/generations"
    )


def test_client_saves_b64_png_response(tmp_path: Path) -> None:
    transport = FakeTransport()
    client = ImageGenerationClient(image_config(output_dir=str(tmp_path)), transport=transport)

    result = client.create_image("画一张测试地图", filename_prefix="map_test")

    assert Path(result.output_path).exists()
    assert Path(result.output_path).read_bytes().startswith(b"\x89PNG")
    assert transport.calls[0]["url"] == "https://example.test/v1/images/generations"
    assert transport.calls[0]["payload"]["model"] == "gpt-image-2"
    assert transport.calls[0]["payload"]["response_format"] == "b64_json"


def test_world_map_image_manager_generates_once_and_records_memory(tmp_path: Path) -> None:
    world_state = WorldState()
    world_state.world_profile.completed = True
    world_state.world_profile.world_style = "科技奇幻"
    world_state.world_profile.map_card = "完整大陆与近海岛屿"
    world_state.world_profile.magic_tech_role = "科技与魔法对立"
    config = image_config(output_dir=str(tmp_path))
    fake_client = FakeImageClient()
    manager = WorldMapImageManager(fake_client, config)  # type: ignore[arg-type]

    result = manager.generate_if_ready(world_state)
    second = manager.generate_if_ready(world_state)

    assert result is not None
    assert second is None
    assert fake_client.calls == 1
    assert world_state.memory_events[-1].kind == "world_map_visual"
    assert "世界地图原画已生成" in world_state.memory_events[-1].summary


def test_world_map_image_manager_can_record_nortantis_renderer_result(tmp_path: Path) -> None:
    world_state = WorldState()
    world_state.world_profile.completed = True
    renderer = FakeMapRenderer(tmp_path / "map.png")
    manager = WorldMapImageManager(renderer=renderer)

    result = manager.generate_if_ready(world_state, campaign_id="demo_campaign")

    assert result is not None
    assert renderer.calls == ["demo_campaign"]
    event = world_state.memory_events[-1]
    assert event.kind == "world_map_visual"
    assert event.payload["renderer"] == "nortantis"
    assert event.payload["output_path"].endswith("map.png")
    assert event.payload["brief_path"].endswith(".brief.json")


def test_world_map_image_manager_redraws_after_map_relevant_world_change(tmp_path: Path) -> None:
    from fu_gm.models import MapLocation

    world_state = WorldState()
    world_state.world_profile.completed = True
    world_state.world_profile.map_card = "完整大陆与近海岛屿"
    renderer = FakeMapRenderer(tmp_path / "map.png")
    manager = WorldMapImageManager(renderer=renderer)

    first = manager.generate_if_ready(world_state, campaign_id="changing-world")
    first_signature = world_state.memory_events[-1].payload["world_signature"]
    world_state.map_locations["白花碑驿站"] = MapLocation(
        name="白花碑驿站",
        description="立在旧路与风铃廊交界处。",
        feature_type="settlement",
        position_hint="钟鸣公国西境",
    )
    second = manager.generate_if_ready(world_state, campaign_id="changing-world")
    second_signature = world_state.memory_events[-1].payload["world_signature"]

    assert first is not None
    assert second is not None
    assert renderer.calls == ["changing-world", "changing-world"]
    assert first_signature != second_signature
    assert manager.has_current_map(world_state)


def test_world_map_signature_includes_political_factions(tmp_path: Path) -> None:
    world_state = WorldState()
    world_state.world_profile.completed = True
    world_state.world_profile.map_card = "完整大陆"
    renderer = FakeMapRenderer(tmp_path / "faction-map.png")
    manager = WorldMapImageManager(renderer=renderer)

    first = manager.generate_if_ready(world_state, campaign_id="faction-world")
    world_state.world_profile.factions["辉钢财团"] = "控制第七采掘城的矿道与记忆炉。"
    second = manager.generate_if_ready(world_state, campaign_id="faction-world")

    assert first is not None
    assert second is not None
    assert renderer.calls == ["faction-world", "faction-world"]


def test_world_map_image_manager_derives_thumbnail_from_full_render(tmp_path: Path) -> None:
    from PIL import Image

    world_state = WorldState()
    world_state.world_profile.completed = True
    renderer = ValidPngMapRenderer(tmp_path / "map.png")
    manager = WorldMapImageManager(renderer=renderer)

    result = manager.generate_if_ready(world_state, campaign_id="demo_campaign")

    assert result is not None
    event = world_state.memory_events[-1]
    thumbnail_path = Path(event.payload["thumbnail_path"])
    assert thumbnail_path.name == "map_thumb.png"
    assert thumbnail_path.exists()
    with Image.open(thumbnail_path) as thumbnail:
        assert thumbnail.size == (1280, 720)


def test_world_map_prompt_forbids_grid_and_distance_calculation() -> None:
    world_state = WorldState()
    world_state.world_profile.completed = True
    world_state.world_profile.major_locations["索朗旧都"] = "齿轮与藤蔓缠绕的废墟"
    manager = WorldMapImageManager(FakeImageClient(), image_config())  # type: ignore[arg-type]

    prompt = manager.build_prompt(world_state.world_profile)

    assert "不要绘制方格" in prompt
    assert "角色徒步一天可走的距离（一个旅行日）" in prompt
    assert "不要从图片像素反推路线距离" in prompt
    assert "索朗旧都" in prompt


def test_world_map_can_generate_when_adventure_starts_before_session_zero_completion(tmp_path: Path) -> None:
    world_state = WorldState()
    world_state.world_profile.completed = False
    world_state.world_profile.map_card = "沿海大陆与近海岛屿"
    renderer = FakeMapRenderer(tmp_path / "early-adventure.png")
    manager = WorldMapImageManager(renderer=renderer)

    result = manager.generate_for_adventure(world_state, campaign_id="early-adventure")

    assert result is not None
    assert renderer.calls == ["early-adventure"]
    assert world_state.world_profile.completed is False


def test_adventure_map_generation_retries_before_reporting_failure(tmp_path: Path) -> None:
    from fu_gm.app_factory import build_app

    app = build_app(use_llm=False)
    app.set_campaign_id("retry-map")
    app.world_state.world_profile.map_card = "沿海大陆"
    renderer = FlakyMapRenderer(tmp_path / "retry-map.png")
    app.world_map_image_manager = WorldMapImageManager(renderer=renderer)

    status = app.ensure_world_map_for_adventure(max_attempts=2)

    assert status["status"] == "generated"
    assert status["attempts"] == 2
    assert renderer.calls == ["retry-map", "retry-map"]
    assert any(event.kind == "world_map_visual_error" for event in app.world_state.memory_events)
    assert any(event.kind == "world_map_visual" for event in app.world_state.memory_events)


def test_nortantis_config_discovers_project_runtime_jdk(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    java_bin = project_dir / ".runtime" / "jdks" / "jdk-21" / "Contents" / "Home" / "bin" / "java"
    java_bin.parent.mkdir(parents=True)
    java_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.delenv("FU_GM_JAVA_EXE", raising=False)
    monkeypatch.delenv("FU_GM_JAVA_BIN", raising=False)
    monkeypatch.delenv("FU_GM_JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv("FU_GM_PROJECT_DIR", str(project_dir))

    config = NortantisMapRendererConfig.from_env()

    assert config.java_exe == str(java_bin)


def test_failed_adventure_map_generation_is_not_retried_without_force(tmp_path: Path) -> None:
    from fu_gm.app_factory import build_app

    app = build_app(use_llm=False)
    app.set_campaign_id("failed-map")
    app.world_state.world_profile.map_card = "沿海大陆"
    renderer = AlwaysFailMapRenderer(tmp_path / "failed-map.png")
    app.world_map_image_manager = WorldMapImageManager(renderer=renderer)

    first = app.ensure_world_map_for_adventure(max_attempts=1)
    second = app.ensure_world_map_for_adventure(max_attempts=1)
    forced = app.ensure_world_map_for_adventure(max_attempts=1, force=True)

    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert forced["status"] == "failed"
    assert renderer.calls == ["failed-map", "failed-map"]


def test_force_redraw_regenerates_a_current_map(tmp_path: Path) -> None:
    from fu_gm.app_factory import build_app

    app = build_app(use_llm=False)
    app.set_campaign_id("force-redraw-map")
    app.world_state.world_profile.map_card = "沿海大陆"
    renderer = FakeMapRenderer(tmp_path / "force-redraw-map.png")
    app.world_map_image_manager = WorldMapImageManager(renderer=renderer)

    first = app.ensure_world_map_for_adventure(max_attempts=1)
    cached = app.ensure_world_map_for_adventure(max_attempts=1)
    forced = app.ensure_world_map_for_adventure(max_attempts=1, force=True)

    assert first["status"] == "generated"
    assert cached["status"] == "generated"
    assert forced["status"] == "generated"
    assert renderer.calls == [
        "force-redraw-map",
        "force-redraw-map",
    ]
