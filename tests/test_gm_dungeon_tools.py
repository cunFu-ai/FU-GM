import tempfile
import unittest

from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import (
    Character,
    SceneType,
    TravelDayResult,
    TravelEventType,
    TravelThreatLevel,
)


def context(message: str) -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="dungeon-tools",
        session_id="s1",
        channel_id="group-1",
        speaker="阿凛",
        gate_status="adventure",
        directly_addressed=True,
        metadata={
            "current_message": message,
            "recent_public_context": "伊莉雅站在镜之水道入口。",
        },
    )


class GMDungeonToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        self.runtime = self.service._runtime("dungeon-tools")
        self.app = self.runtime.app
        self.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=45,
                max_mp=35,
                mp=35,
                traits=["pc"],
            )
        )
        self.app.scene_manager.start_scene(
            "镜之水道入口",
            SceneType.STANDARD,
            location="旧王国地下",
            participants=["伊莉雅"],
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_start_persists_a_playable_dungeon_with_real_participants(self) -> None:
        message = "伊莉雅推开水门，正式进入镜之水道。"

        receipt = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context(message),
            {
                "name": "镜之水道",
                "location": "旧王国地下",
                "importance": "major",
                "preparation": "prepared",
                "purpose": "找到失踪的守钟人",
                "concept": "水道网络",
                "focus": "失踪的守钟人",
                "inhabitants": "古代构装体",
                "peculiarity": "会倒映未来的水面",
                "evidence": "正式进入镜之水道",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(self.app.dungeon_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅"],
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_type,
            SceneType.DUNGEON,
        )
        self.assertTrue(self.app.dungeon_manager.state.areas)
        self.assertEqual(
            self.app.dungeon_manager.state.current_area,
            "入口",
        )
        for clock_name in self.app.dungeon_manager.state.danger_clocks:
            clock = self.app.clock_manager.get(clock_name)
            self.assertEqual(
                clock.scene_id,
                self.app.scene_manager.current_scene.scene_id,
            )

        reloaded = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )._runtime("dungeon-tools")
        self.assertTrue(reloaded.app.dungeon_manager.state.active)
        self.assertEqual(
            reloaded.app.dungeon_manager.state.name,
            "镜之水道",
        )
        self.assertEqual(
            reloaded.app.scene_manager.current_scene.participants,
            ["伊莉雅"],
        )

    def test_start_cannot_overwrite_an_active_journey(self) -> None:
        self.app.travel_manager.begin_journey(
            journey_id="journey-active",
            origin="旧王国地下",
            destination="镜之水道入口",
            threat_levels=[TravelThreatLevel.LOW],
            party_names=["伊莉雅"],
        )
        scene_id = self.app.scene_manager.current_scene.scene_id

        receipt = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context("伊莉雅在旅途中直接进入镜之水道。"),
            {
                "name": "镜之水道",
                "location": "旧王国地下",
                "purpose": "找到失踪的守钟人",
                "evidence": "在旅途中直接进入镜之水道",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTIVE_JOURNEY_REQUIRES_TRAVEL_TOOL",
        )
        self.assertIsNotNone(self.app.travel_manager.active_journey)
        self.assertFalse(self.app.dungeon_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_id,
        )

    def test_travel_dungeon_discovery_can_be_explored_then_resume_journey(self) -> None:
        self.app.scene_manager.start_scene(
            "旧王国地下至镜之水道入口",
            SceneType.TRAVEL,
            location="旧王国地下",
            participants=["伊莉雅"],
        )
        journey = self.app.travel_manager.begin_journey(
            journey_id="journey-discovery",
            origin="旧王国地下",
            destination="镜之水道入口",
            threat_levels=[TravelThreatLevel.LOW],
            party_names=["伊莉雅"],
        )
        discovery = TravelDayResult(
            day=1,
            region="旧王国地下",
            threat_level=TravelThreatLevel.LOW,
            die_size=8,
            roll=1,
            event_type=TravelEventType.DISCOVERY,
            summary="队伍发现镜之水道入口。",
            event_detail="古代废墟入口：镜之水道的水门露出地面。",
            discovered_location="旧王国地下的镜之水道入口",
            danger_tags=["dungeon"],
        )
        journey.current_day = 1
        journey.day_results = [discovery]
        journey.pending_event_day = 1
        journey.status = "event_pending"

        started = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context("伊莉雅推开途中发现的水门，正式进入镜之水道。"),
            {
                "name": "镜之水道",
                "location": "旧王国地下的镜之水道入口",
                "purpose": "查明水门后的遗迹",
                "participants": ["伊莉雅"],
                "evidence": "正式进入镜之水道",
            },
        )

        self.assertTrue(started.ok, started.message)
        self.assertTrue(started.result["journey_suspended"])
        self.assertTrue(self.app.dungeon_manager.state.active)
        self.assertIs(self.app.travel_manager.active_journey, journey)
        self.assertEqual(journey.status, "event_pending")

        restarted_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        restarted_runtime = restarted_service._runtime("dungeon-tools")
        restarted_app = restarted_runtime.app
        self.assertTrue(restarted_app.dungeon_manager.state.active)
        self.assertIsNotNone(restarted_app.travel_manager.active_journey)
        self.assertEqual(
            restarted_app.travel_manager.active_journey.status,
            "event_pending",
        )

        blocked = restarted_service.gm_adventure_tools.continue_travel(
            context("伊莉雅还在镜之水道里，但准备继续原来的旅程。"),
            {
                "event_resolution": "镜之水道已经处理完毕。",
                "evidence": "准备继续原来的旅程",
            },
        )

        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "DUNGEON_ACTIVE")
        self.assertTrue(restarted_app.dungeon_manager.state.active)
        self.assertEqual(
            restarted_app.travel_manager.active_journey.status,
            "event_pending",
        )
        self.assertEqual(
            restarted_app.scene_manager.current_scene.scene_type,
            SceneType.DUNGEON,
        )

        finished = restarted_service.gm_dungeon_tools.finish_dungeon_exploration(
            context("伊莉雅查明遗迹后离开水道，回到途中发现的入口。"),
            {
                "outcome": "completed",
                "completion_reason": "镜之水道已经查明，伊莉雅回到原路线。",
                "exit_location": "旧王国地下的镜之水道入口",
                "evidence": "离开水道",
            },
        )

        self.assertTrue(finished.ok, finished.message)
        self.assertTrue(finished.result["journey_event_still_pending"])
        self.assertEqual(
            restarted_app.travel_manager.active_journey.status,
            "event_pending",
        )
        continued = restarted_service.gm_adventure_tools.continue_travel(
            context("镜之水道已经探索完毕，伊莉雅返回路线继续赶路。"),
            {
                "event_resolution": "队伍探索镜之水道后回到原路线。",
                "evidence": "返回路线继续赶路",
            },
        )
        self.assertTrue(continued.ok, continued.message)
        self.assertIsNone(restarted_app.travel_manager.active_journey)
        self.assertEqual(
            restarted_app.scene_manager.current_scene.location,
            "镜之水道入口",
        )

    def test_finish_archives_dungeon_clocks_and_keeps_party_at_exit(self) -> None:
        start_message = "伊莉雅推开水门，正式进入镜之水道。"
        started = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context(start_message),
            {
                "name": "镜之水道",
                "location": "旧王国地下",
                "importance": "minor",
                "preparation": "improvised",
                "purpose": "找到失踪的守钟人",
                "evidence": "正式进入镜之水道",
            },
        )
        self.assertTrue(started.ok, started.message)
        clock_names = list(self.app.dungeon_manager.state.danger_clocks)
        finish_message = "守钟人已经获救，伊莉雅带着他离开水道，回到旧王国地下入口。"

        receipt = self.service.gm_dungeon_tools.finish_dungeon_exploration(
            context(finish_message),
            {
                "outcome": "completed",
                "completion_reason": "守钟人获救，队伍平安离开镜之水道。",
                "exit_location": "旧王国地下入口",
                "evidence": "带着他离开水道",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertFalse(self.app.dungeon_manager.state.active)
        self.assertEqual(
            self.app.scene_manager.current_scene.location,
            "旧王国地下入口",
        )
        self.assertEqual(
            self.app.scene_manager.current_scene.participants,
            ["伊莉雅"],
        )
        for clock_name in clock_names:
            self.assertFalse(self.app.clock_manager.exists(clock_name))
            self.assertIsNotNone(
                self.app.clock_manager.archived_match(clock_name)
            )
        self.assertEqual(
            self.app.dungeon_manager.history[-1].completion_status,
            "completed",
        )

    def test_retreat_does_not_record_dungeon_as_resolved(self) -> None:
        started = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context("伊莉雅推开水门，正式进入镜之水道。"),
            {
                "name": "镜之水道",
                "location": "旧王国地下",
                "purpose": "找到失踪的守钟人",
                "evidence": "正式进入镜之水道",
            },
        )
        self.assertTrue(started.ok, started.message)

        receipt = self.service.gm_dungeon_tools.finish_dungeon_exploration(
            context("水势已经无法控制，伊莉雅决定撤回入口。"),
            {
                "outcome": "retreated",
                "completion_reason": "水势失控，队伍撤回入口；守钟人仍然下落不明。",
                "exit_location": "旧王国地下入口",
                "evidence": "决定撤回入口",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["outcome"], "retreated")
        ended = self.app.dungeon_manager.history[-1]
        self.assertEqual(ended.completion_status, "retreated")
        self.assertIn("守钟人仍然下落不明", ended.completion_summary)

    def test_start_rejects_a_remote_participant(self) -> None:
        self.app.character_manager.add(
            Character(
                name="洛岚",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=40,
                hp=40,
                max_mp=50,
                mp=50,
                traits=["pc"],
            )
        )
        message = "伊莉雅推开水门，正式进入镜之水道。"

        receipt = self.service.gm_dungeon_tools.start_dungeon_exploration(
            context(message),
            {
                "name": "镜之水道",
                "location": "旧王国地下",
                "participants": ["伊莉雅", "洛岚"],
                "evidence": "正式进入镜之水道",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "DUNGEON_PARTICIPANT_NOT_PRESENT",
        )


if __name__ == "__main__":
    unittest.main()
