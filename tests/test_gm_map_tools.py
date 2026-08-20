from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fu_gm.components.map_renderer import MapRenderResult
from fu_gm.components.world_map_image_manager import WorldMapImageManager
from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService


class FakeMapRenderer:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[str] = []

    def render(self, _world_state, *, campaign_id: str = "default") -> MapRenderResult:
        self.calls.append(campaign_id)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        index = len(self.calls)
        output_path = self.output_dir / f"{campaign_id}-{index}.png"
        brief_path = self.output_dir / f"{campaign_id}-{index}.brief.json"
        settings_path = self.output_dir / f"{campaign_id}-{index}.nort"
        output_path.write_bytes(b"fake-png")
        brief_path.write_text("{}", encoding="utf-8")
        settings_path.write_text("{}", encoding="utf-8")
        return MapRenderResult(
            renderer="fake-map",
            brief_path=str(brief_path),
            output_path=str(output_path),
            settings_path=str(settings_path),
            command=["fake-map"],
            stdout="ok",
            stderr="",
            seed=8675309,
            terrain_seed=24680,
        )


def context(campaign_id: str = "地图工具团") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id=campaign_id,
        session_id="s0",
        channel_id="group-1",
        speaker="白河",
        gate_status="session_zero",
        directly_addressed=True,
        metadata={"current_message": "时悠，现在画一张地图。"},
    )


class GMMapToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime("地图工具团")
        self.renderer = FakeMapRenderer(Path(self.tempdir.name) / "maps")
        self.runtime.app.world_map_image_manager = WorldMapImageManager(
            renderer=self.renderer
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_map_tools_are_registered_and_available_during_session_zero(self) -> None:
        schemas = {
            item["name"]: item
            for item in self.service.gm_tool_registry.schemas()
        }
        names = set(schemas)
        self.assertIn("generate_world_map_preview", names)
        self.assertIn("get_world_map_status", names)
        self.assertIn("edit_world_map", names)
        self.assertIn("inspect_semantic_map", names)
        self.assertIn("suggest_route_travel_days", names)
        self.assertIn("find_map_location_candidates", names)
        self.assertIn("place_world_map_locations", names)
        self.assertEqual(
            schemas["find_map_location_candidates"]["side_effect"],
            "write_pending",
        )

    def test_failed_map_write_rolls_back_campaign_and_ephemeral_map_state(self) -> None:
        app = self.runtime.app
        app.world_state.world_profile.continent_name = "星藤大陆"
        app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
            position_hint="north",
            draw_icon=True,
        )
        self.service.gm_map_tools._placement_contexts = {
            "existing": {"campaign_id": "其他团", "revision": 1}
        }
        self.service.gm_map_tools._pending_redraw = {
            ("其他团", "s0", "白河"): False
        }
        app._world_map_generation_status = {
            "status": "ready",
            "attempts": 1,
        }

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk full"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "edit_world_map",
                {
                    "location_name": "托伦王国",
                    "position_hint": "south",
                    "redraw": False,
                },
                context(),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        restored = app.world_state.map_locations["托伦王国"]
        self.assertEqual(restored.position_hint, "north")
        self.assertEqual(
            self.service.gm_map_tools._placement_contexts,
            {"existing": {"campaign_id": "其他团", "revision": 1}},
        )
        self.assertEqual(
            self.service.gm_map_tools._pending_redraw,
            {("其他团", "s0", "白河"): False},
        )
        self.assertEqual(
            app._world_map_generation_status,
            {"status": "ready", "attempts": 1},
        )

    def test_edit_existing_location_to_relative_position_and_redraw(self) -> None:
        world = self.runtime.app.world_state
        world.world_profile.continent_name = "星藤大陆"
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
            draw_icon=True,
        )
        self.runtime.app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
            position_hint="north",
            draw_icon=True,
        )

        receipt = self.service.gm_map_tools.edit_world_map(
            context(),
            {
                "location_name": "托伦王国",
                "relative_to": "赤砂帝国",
                "relative_position": "west",
            },
        )

        location = world.map_locations["托伦王国"]
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(location.position_hint, "")
        self.assertEqual(location.relative_to, "赤砂帝国")
        self.assertEqual(location.relative_position, "west")
        self.assertEqual(receipt.result["status"], "needs_placement")
        self.assertEqual(
            receipt.result["required_followup_tools"],
            ["find_map_location_candidates"],
        )
        self.assertEqual(self.renderer.calls, [])

        inspected = self.service.gm_map_tools.find_map_location_candidates(
            context(),
            {},
        )
        self.assertTrue(inspected.state_changed)
        target = inspected.result["current_location"]
        cell = inspected.result["candidates"][target][0]["cell"]
        placed = self.service.gm_map_tools.place_world_map_locations(
            context(),
            {
                "placement_context_id": inspected.result[
                    "placement_context_id"
                ],
                "placements": [
                    {"location_name": target, "grid_cell": cell}
                ],
            },
        )

        self.assertTrue(placed.ok)
        self.assertIn(
            placed.result["status"],
            {"needs_placement", "generated"},
        )

    def test_edit_unknown_location_returns_exact_available_names(self) -> None:
        world = self.runtime.app.world_state
        world.world_profile.continent_name = "星藤大陆"
        self.runtime.app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
        )

        receipt = self.service.gm_map_tools.edit_world_map(
            context(),
            {
                "location_name": "托伦",
                "position_hint": "west",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MAP_LOCATION_NOT_FOUND")
        self.assertEqual(receipt.result["available_locations"], ["托伦王国"])
        self.assertEqual(self.renderer.calls, [])

    def test_edit_unnamed_map_saves_position_but_waits_for_name(self) -> None:
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
        )
        self.runtime.app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
            position_hint="north",
        )

        receipt = self.service.gm_map_tools.edit_world_map(
            context(),
            {
                "location_name": "托伦王国",
                "relative_to": "赤砂帝国",
                "relative_position": "west",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.state_changed)
        self.assertEqual(receipt.result["status"], "needs_name")
        self.assertEqual(self.renderer.calls, [])
        self.assertIn("还没有名字", receipt.public_fallback_reply)

    def test_non_rendering_edit_does_not_force_an_unnamed_map_question(self) -> None:
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
        )
        self.runtime.app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
            position_hint="north",
        )

        receipt = self.service.gm_map_tools.edit_world_map(
            context(),
            {
                "location_name": "托伦王国",
                "relative_to": "赤砂帝国",
                "relative_position": "west",
                "redraw": False,
            },
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result["status"], "needs_placement")
        self.assertNotIn("required_field", receipt.result)
        self.assertNotIn("还没有名字", receipt.public_fallback_reply)
        self.assertFalse(receipt.lock_public_reply)

    def test_edit_tool_can_name_map_outside_session_zero(self) -> None:
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
        )
        adventure_context = context()
        adventure_context.gate_status = "adventure"

        receipt = self.service.gm_tool_registry.execute(
            "edit_world_map",
            {"map_name": "星藤大陆"},
            adventure_context,
        )

        self.assertTrue(receipt.ok)
        self.assertEqual(
            self.runtime.app.world_state.world_profile.continent_name,
            "星藤大陆",
        )
        self.assertEqual(receipt.result["status"], "needs_placement")
        self.assertEqual(self.renderer.calls, [])

    def test_map_state_summary_exposes_exact_location_relationships(self) -> None:
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
        )
        self.runtime.app.world_map_manager.add_location(
            "托伦王国",
            feature_type="country",
            relative_to="赤砂帝国",
            relative_position="west",
        )

        summary = self.service.gm_map_tools.state_summary(context())

        locations = {
            item["name"]: item
            for item in summary["map_locations"]
        }
        self.assertEqual(locations["赤砂帝国"]["position_hint"], "east")
        self.assertEqual(locations["托伦王国"]["relative_to"], "赤砂帝国")
        self.assertEqual(locations["托伦王国"]["relative_position"], "west")
        self.assertEqual(
            summary["semantic_layout"]["unplaced_locations"],
            ["托伦王国", "赤砂帝国"],
        )

    def test_route_suggestion_is_read_only_until_route_is_registered(self) -> None:
        app = self.runtime.app
        app.world_map_manager.add_location(
            "西港",
            feature_type="settlement",
            semantic_cell="D06",
        )
        app.world_map_manager.add_location(
            "东港",
            feature_type="settlement",
            semantic_cell="P06",
        )

        receipt = self.service.gm_map_tools.suggest_route_travel_days(
            context(),
            {
                "origin": "西港",
                "destination": "东港",
                "travel_mode": "mixed",
            },
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertTrue(receipt.result["advisory_only"])
        self.assertFalse(receipt.result["authoritative"])
        self.assertEqual(app.world_state.map_routes, {})

    def test_candidate_receipt_shows_grid_and_requires_validated_placement(
        self,
    ) -> None:
        world = self.runtime.app.world_state
        world.world_profile.continent_name = "星藤大陆"
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
        )

        inspected = self.service.gm_map_tools.find_map_location_candidates(
            context(),
            {},
        )

        self.assertTrue(inspected.ok)
        self.assertIn("A B C D", inspected.result["grid"])
        self.assertIn("赤砂帝国", inspected.result["candidates"])
        self.assertEqual(
            inspected.result["required_followup_tools"],
            ["place_world_map_locations"],
        )

        rejected = self.service.gm_map_tools.place_world_map_locations(
            context(),
            {
                "placement_context_id": inspected.result[
                    "placement_context_id"
                ],
                "placements": [
                    {
                        "location_name": "赤砂帝国",
                        "grid_cell": "A01",
                    }
                ],
            },
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, "INVALID_MAP_PLACEMENT")

    def test_place_without_reading_map_is_rejected(self) -> None:
        receipt = self.service.gm_map_tools.place_world_map_locations(
            context(),
            {
                "placement_context_id": "not-a-real-context",
                "placements": [
                    {"location_name": "不存在", "grid_cell": "A01"}
                ],
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "MAP_NOT_INSPECTED")

    def test_failed_placement_autosave_restores_grid_and_retry_context(self) -> None:
        world = self.runtime.app.world_state
        world.world_profile.continent_name = "星藤大陆"
        self.runtime.app.world_map_manager.add_location(
            "赤砂帝国",
            feature_type="country",
            position_hint="east",
        )
        inspected = self.service.gm_map_tools.find_map_location_candidates(
            context(),
            {},
        )
        placement_context_id = str(
            inspected.result["placement_context_id"]
        )
        target = str(inspected.result["current_location"])
        cell = str(inspected.result["candidates"][target][0]["cell"])

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk full during placement"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "place_world_map_locations",
                {
                    "placement_context_id": placement_context_id,
                    "placements": [
                        {
                            "location_name": target,
                            "grid_cell": cell,
                        }
                    ],
                },
                context(),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertIn(
            target,
            self.service.gm_map_tools.state_summary(context())[
                "semantic_layout"
            ]["unplaced_locations"],
        )
        self.assertIn(
            placement_context_id,
            self.service.gm_map_tools._placement_contexts,
        )

    def test_generate_preview_returns_player_media_and_status_can_show_it(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "白钟大陆"
        world.kingdoms["钟鸣公国"] = "钟塔林立的国家。"

        generated = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )
        status = self.service.gm_map_tools.get_status(context(), {})

        self.assertTrue(generated.ok)
        self.assertTrue(generated.state_changed)
        self.assertEqual(generated.result["status"], "generated")
        self.assertEqual(len(generated.result["reply_media"]), 1)
        media = generated.result["reply_media"][0]
        self.assertTrue(Path(str(media["path"])).is_file())
        self.assertEqual(self.renderer.calls, ["地图工具团"])
        event = self.runtime.app.world_state.memory_events[-1]
        self.assertEqual(event.payload["map_seed"], 8675309)
        self.assertEqual(event.payload["terrain_seed"], 24680)
        self.assertEqual(world.map_card, "自定义地图")
        self.assertTrue(status.ok)
        self.assertEqual(status.result["status"], "ready")
        self.assertEqual(status.result["reply_media"], generated.result["reply_media"])

    def test_generated_map_is_not_committed_when_final_autosave_fails(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "白钟大陆"
        world.kingdoms["钟鸣公国"] = "钟塔林立的国家。"
        memory_count = len(self.runtime.app.world_state.memory_events)
        self.renderer.output_dir.mkdir(parents=True, exist_ok=True)
        existing_artifact = self.renderer.output_dir / "existing-map.png"
        existing_artifact.write_bytes(b"existing-map")

        with patch.object(
            self.service,
            "_autosave_campaign",
            side_effect=RuntimeError("disk full after render"),
        ):
            receipt = self.service.gm_tool_registry.execute(
                "generate_world_map_preview",
                {"redraw": False},
                context(),
            )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(
            len(self.runtime.app.world_state.memory_events),
            memory_count,
        )
        self.assertEqual(
            self.runtime.app.world_state.world_profile.map_card,
            "",
        )
        status = self.service.gm_map_tools.get_status(context(), {})
        self.assertFalse(status.result["artifact"]["available"])
        self.assertEqual(status.result["reply_media"], [])
        self.assertEqual(
            sorted(path.name for path in self.renderer.output_dir.iterdir()),
            ["existing-map.png"],
        )
        self.assertEqual(existing_artifact.read_bytes(), b"existing-map")

        restarted = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
        )
        restored = restarted._runtime("地图工具团").app.world_state
        self.assertFalse(
            any(event.kind == "world_map_visual" for event in restored.memory_events)
        )

    def test_audit_treats_existing_visual_map_as_completed_world_map(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "宁姆格福"
        world.kingdoms["索朗帝国"] = "蒸汽帝国。"
        self.runtime.app.world_map_image_manager.generate_for_adventure(
            self.runtime.app.world_state,
            campaign_id="地图工具团",
        )

        self.assertEqual(world.map_card, "")
        audit = self.service._setup_audit_payload(
            self.runtime.app,
            [],
            limit=20,
        )
        map_row = next(
            row for row in audit["checklist"] if row["name"] == "世界地图"
        )

        self.assertTrue(map_row["ready"])
        self.assertEqual(map_row["value"], "自定义地图")
        self.assertEqual(world.map_card, "自定义地图")

    def test_explicit_redraw_bypasses_current_map_cache(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "赤砂大陆"
        world.kingdoms["赤砂帝国"] = "沙海帝国。"

        first = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )
        cached = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )
        redrawn = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": True},
        )

        self.assertEqual(first.result["status"], "generated")
        self.assertEqual(cached.result["status"], "ready")
        self.assertFalse(cached.state_changed)
        self.assertEqual(redrawn.result["status"], "generated")
        self.assertTrue(redrawn.state_changed)
        self.assertEqual(
            self.renderer.calls,
            ["地图工具团", "地图工具团"],
        )

    def test_generation_without_geographic_foundation_is_truthfully_deferred(self) -> None:
        receipt = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertEqual(receipt.result["status"], "deferred")
        self.assertEqual(receipt.result["reply_media"], [])
        self.assertIn("还没有足够", receipt.public_fallback_reply)
        self.assertEqual(self.renderer.calls, [])

    def test_generation_requires_map_name_before_rendering(self) -> None:
        self.runtime.app.world_state.world_profile.kingdoms["钟鸣公国"] = "钟塔林立的国家。"

        receipt = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )

        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.state_changed)
        self.assertEqual(receipt.result["status"], "needs_name")
        self.assertEqual(receipt.result["required_field"], "continent_name")
        self.assertEqual(receipt.result["reply_media"], [])
        self.assertEqual(receipt.public_fallback_reply, "这张地图还没有名字。你想叫它什么？")
        self.assertEqual(self.renderer.calls, [])

    def test_status_does_not_send_an_unnamed_existing_map(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "白钟大陆"
        world.kingdoms["钟鸣公国"] = "钟塔林立的国家。"
        generated = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )
        self.assertEqual(generated.result["status"], "generated")
        world.continent_name = ""

        receipt = self.service.gm_map_tools.get_status(context(), {})

        self.assertEqual(receipt.result["status"], "needs_name")
        self.assertEqual(receipt.result["reply_media"], [])
        self.assertEqual(receipt.public_fallback_reply, "这张地图还没有名字。你想叫它什么？")

    def test_coordinator_collects_only_successful_image_receipts(self) -> None:
        world = self.runtime.app.world_state.world_profile
        world.continent_name = "托伦大陆"
        world.kingdoms["托伦王国"] = "沿海王国。"
        receipt = self.service.gm_map_tools.generate_preview(
            context(),
            {"redraw": False},
        )

        media = self.service.gm_agent_message_coordinator._reply_media([receipt])

        self.assertEqual(media, receipt.result["reply_media"])


if __name__ == "__main__":
    unittest.main()
