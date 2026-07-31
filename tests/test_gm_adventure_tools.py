import tempfile
import unittest

from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, SceneType, TravelEventType, TravelThreatLevel


def context(message: str, *, speaker: str = "阿凛") -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="adventure-tools",
        session_id="s1",
        channel_id="group-1",
        speaker=speaker,
        gate_status="adventure",
        directly_addressed=True,
        metadata={"current_message": message, "recent_public_context": "队伍正在白花碑驿站整装。"},
    )


class GMAdventureToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tmpdir.name, use_llm=False)
        self.runtime = self.service._runtime("adventure-tools")
        self.app = self.runtime.app
        self.app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 6},
                max_hp=45,
                hp=45,
                max_mp=35,
                mp=35,
                zenit=500,
                traits=["pc"],
            )
        )
        self.app.world_map_manager.add_location("白花碑驿站", terrain="村庄")
        self.app.world_map_manager.add_location("钟鸣公国", terrain="城市")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_travel_uses_existing_rules_pays_service_and_is_idempotent(self) -> None:
        rolls = iter([3, 4])
        self.app.interceptor.rules_engine.roll_die = lambda _sides: next(rolls)
        message = "伊莉雅决定从白花碑驿站出发，雇陆地旅行服务前往钟鸣公国，由她付款。"
        arguments = {
            "origin": "白花碑驿站",
            "destination": "钟鸣公国",
            "transport": "陆地旅行服务",
            "payer": "伊莉雅",
            "explicit_distance": 2,
            "route_type": "land",
            "default_threat_level": "low",
            "evidence": "从白花碑驿站出发，雇陆地旅行服务前往钟鸣公国",
        }

        first = self.service.gm_adventure_tools.travel_party(context(message), arguments)
        second = self.service.gm_adventure_tools.travel_party(context(message), arguments)

        self.assertTrue(first.ok, first.message)
        self.assertTrue(first.state_changed)
        self.assertEqual(first.result["days"], 2)
        self.assertEqual(first.result["service_transaction"]["total_cost"], 20)
        self.assertEqual(self.app.character_manager.get("伊莉雅").zenit, 480)
        self.assertEqual(len(self.app.travel_manager.history), 1)
        self.assertTrue(second.ok)
        self.assertFalse(second.state_changed)
        self.assertEqual(self.app.character_manager.get("伊莉雅").zenit, 480)
        self.assertEqual(self.app.scene_manager.current_scene.location, "钟鸣公国")
        self.assertEqual(self.app.scene_manager.current_scene.participants, ["伊莉雅"])

    def test_travel_cannot_overwrite_an_active_dungeon(self) -> None:
        self.app.scene_manager.start_scene(
            "镜之水道深处",
            SceneType.DUNGEON,
            location="镜之水道",
            participants=["伊莉雅"],
        )
        self.app.dungeon_manager.state.active = True
        self.app.dungeon_manager.state.name = "镜之水道"
        scene_id = self.app.scene_manager.current_scene.scene_id

        receipt = self.service.gm_adventure_tools.travel_party(
            context("伊莉雅直接离开镜之水道，前往钟鸣公国。"),
            {
                "origin": "镜之水道",
                "destination": "钟鸣公国",
                "explicit_distance": 1,
                "evidence": "直接离开镜之水道，前往钟鸣公国",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "ACTIVE_DUNGEON_REQUIRES_DUNGEON_TOOL",
        )
        self.assertTrue(self.app.dungeon_manager.state.active)
        self.assertIsNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_id,
            scene_id,
        )

    def test_danger_pauses_journey_survives_reload_and_requires_resolution(self) -> None:
        rolls = iter([8])
        self.app.interceptor.rules_engine.roll_die = lambda _sides: next(rolls)
        message = "伊莉雅从白花碑驿站出发，徒步前往钟鸣公国。"
        first = self.service.gm_adventure_tools.travel_party(
            context(message),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "explicit_distance": 2,
                "default_threat_level": "low",
                "evidence": "从白花碑驿站出发，徒步前往钟鸣公国",
            },
        )

        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.result["status"], "event_pending")
        self.assertEqual(first.result["pending_event"]["event_type"], "danger")
        self.assertEqual(self.app.travel_manager.history, [])
        self.assertEqual(
            self.app.scene_manager.current_scene.scene_type,
            SceneType.TRAVEL,
        )
        self.assertIn("途中", self.app.scene_manager.location_of("伊莉雅"))

        reloaded_service = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )
        reloaded_runtime = reloaded_service._runtime("adventure-tools")
        reloaded = reloaded_runtime.app
        active = reloaded.travel_manager.active_journey
        self.assertIsNotNone(active)
        self.assertEqual(active.status, "event_pending")
        self.assertEqual(active.threat_levels[0], TravelThreatLevel.LOW)
        self.assertEqual(
            reloaded.travel_manager.pending_travel_event().event_type,
            TravelEventType.DANGER,
        )

        more_rolls = iter([4])
        reloaded.interceptor.rules_engine.roll_die = lambda _sides: next(more_rolls)
        continue_message = "塌桥已经用绳索加固，伊莉雅确认全员通过后继续赶路。"
        continued = reloaded_service.gm_adventure_tools.continue_travel(
            context(continue_message),
            {
                "event_resolution": "塌桥已经用绳索加固，全员安全通过。",
                "evidence": "塌桥已经用绳索加固",
            },
        )

        self.assertTrue(continued.ok, continued.message)
        self.assertEqual(continued.result["status"], "arrived")
        self.assertIsNone(reloaded.travel_manager.active_journey)
        self.assertEqual(len(reloaded.travel_manager.history), 1)
        self.assertEqual(reloaded.scene_manager.current_scene.location, "钟鸣公国")

    def test_continue_travel_rejects_unresolved_or_missing_event(self) -> None:
        message = "伊莉雅准备继续赶路。"
        receipt = self.service.gm_adventure_tools.continue_travel(
            context(message),
            {
                "event_resolution": "准备处理。",
                "evidence": "准备继续赶路",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "NO_ACTIVE_JOURNEY")

    def test_party_can_abort_a_paused_journey_and_reload_the_record(self) -> None:
        self.app.interceptor.rules_engine.roll_die = lambda _sides: 8
        started = self.service.gm_adventure_tools.travel_party(
            context("伊莉雅从白花碑驿站出发，徒步前往钟鸣公国。"),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "explicit_distance": 2,
                "default_threat_level": "low",
                "evidence": "从白花碑驿站出发，徒步前往钟鸣公国",
            },
        )
        self.assertTrue(started.ok, started.message)
        self.assertEqual(started.result["status"], "event_pending")

        message = "塌桥无法安全通过，伊莉雅决定放弃这次行程，返回白花碑驿站。"
        receipt = self.service.gm_adventure_tools.abort_travel(
            context(message),
            {
                "reason": "塌桥无法安全通过，队伍放弃前往钟鸣公国。",
                "end_location": "白花碑驿站",
                "evidence": "决定放弃这次行程，返回白花碑驿站",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(receipt.result["status"], "interrupted")
        self.assertIsNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.scene_manager.current_scene.location,
            "白花碑驿站",
        )
        interrupted = self.app.travel_manager.interrupted_journeys[-1]
        self.assertEqual(interrupted.status, "interrupted")
        self.assertEqual(interrupted.end_location, "白花碑驿站")
        self.assertIn("塌桥", interrupted.interruption_reason)

        reloaded = FUGMHttpService(
            data_root=self.tmpdir.name,
            use_llm=False,
        )._runtime("adventure-tools").app
        self.assertIsNone(reloaded.travel_manager.active_journey)
        self.assertEqual(
            reloaded.travel_manager.interrupted_journeys[-1].end_location,
            "白花碑驿站",
        )

    def test_split_party_travel_does_not_move_remote_hero(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        self.app.scene_manager.start_scene(
            "驿站启程",
            SceneType.STANDARD,
            location="白花碑驿站",
            participants=["伊莉雅"],
        )
        self.app.scene_manager.actor_locations["艾薇娅"] = "星落尖塔"
        rolls = iter([4])
        self.app.interceptor.rules_engine.roll_die = lambda _sides: next(rolls)
        message = "伊莉雅独自从白花碑驿站前往钟鸣公国。"

        receipt = self.service.gm_adventure_tools.travel_party(
            context(message),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "participants": ["伊莉雅"],
                "explicit_distance": 1,
                "evidence": "伊莉雅独自从白花碑驿站前往钟鸣公国",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(self.app.scene_manager.location_of("伊莉雅"), "钟鸣公国")
        self.assertEqual(self.app.scene_manager.location_of("艾薇娅"), "星落尖塔")
        self.assertNotIn("艾薇娅", self.app.scene_manager.current_scene.participants)

    def test_remote_travel_participant_is_rejected_without_teleport(self) -> None:
        self.app.character_manager.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 8, "INS": 8, "MIG": 6, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=55,
                mp=55,
                traits=["pc"],
            )
        )
        self.app.scene_manager.start_scene(
            "驿站启程",
            SceneType.STANDARD,
            location="白花碑驿站",
            participants=["伊莉雅"],
        )
        self.app.scene_manager.actor_locations["艾薇娅"] = "星落尖塔"
        message = "伊莉雅和艾薇娅从白花碑驿站前往钟鸣公国。"

        receipt = self.service.gm_adventure_tools.travel_party(
            context(message),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "participants": ["伊莉雅", "艾薇娅"],
                "explicit_distance": 1,
                "evidence": "伊莉雅和艾薇娅从白花碑驿站前往钟鸣公国",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TRAVEL_PARTICIPANT_NOT_PRESENT")
        self.assertEqual(self.app.travel_manager.history, [])

    def test_paid_travel_without_payer_does_not_roll_or_mutate(self) -> None:
        message = "我们从白花碑驿站雇车去钟鸣公国。"
        before_events = len(self.app.world_state.memory_events)

        receipt = self.service.gm_adventure_tools.travel_party(
            context(message),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "transport": "陆地旅行服务",
                "explicit_distance": 2,
                "evidence": "从白花碑驿站雇车去钟鸣公国",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TRAVEL_PAYER_REQUIRED")
        self.assertEqual(len(self.app.world_state.memory_events), before_events)
        self.assertEqual(self.app.travel_manager.history, [])

    def test_impossible_transport_route_is_rejected_before_travel_starts(self) -> None:
        self.app.scene_manager.start_scene(
            "驿站码头",
            SceneType.STANDARD,
            location="白花碑驿站",
            participants=["伊莉雅"],
        )
        message = "伊莉雅打算徒步横渡水面，直接前往钟鸣公国。"

        receipt = self.service.gm_adventure_tools.travel_party(
            context(message),
            {
                "origin": "白花碑驿站",
                "destination": "钟鸣公国",
                "transport": "徒步",
                "route_type": "water",
                "explicit_distance": 2,
                "evidence": "徒步横渡水面，直接前往钟鸣公国",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "TRAVEL_REJECTED")
        self.assertIn("不能用于water路线", receipt.message)
        self.assertIsNone(self.app.travel_manager.active_journey)
        self.assertEqual(
            self.app.scene_manager.current_scene.location,
            "白花碑驿站",
        )

    def test_reward_derives_party_level_and_cannot_be_granted_twice(self) -> None:
        message = "机兵已经投降，我们收下守望会答应的报酬。"
        arguments = {
            "recipients": ["伊莉雅"],
            "difficulty": "normal",
            "source": "守望会护送委托",
            "evidence": "机兵已经投降，我们收下守望会答应的报酬",
        }
        before = self.app.character_manager.get("伊莉雅").zenit

        first = self.service.gm_adventure_tools.award_stage_reward(context(message), arguments)
        after_first = self.app.character_manager.get("伊莉雅").zenit
        second = self.service.gm_adventure_tools.award_stage_reward(context(message), arguments)

        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.result["party_level"], 5)
        self.assertGreater(after_first, before)
        self.assertTrue(second.ok)
        self.assertFalse(second.state_changed)
        self.assertEqual(self.app.character_manager.get("伊莉雅").zenit, after_first)

    def test_reward_defaults_to_pcs_in_the_current_scene_not_remote_pcs(self) -> None:
        self.app.character_manager.add(
            Character(
                name="洛岚",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                zenit=500,
                traits=["pc"],
            )
        )
        self.app.start_scene(
            "守望会委托结算",
            SceneType.STANDARD,
            participants=["伊莉雅"],
        )
        ilya_before = self.app.character_manager.get("伊莉雅").zenit
        loran_before = self.app.character_manager.get("洛岚").zenit
        message = "伊莉雅收下守望会答应的报酬。"

        receipt = self.service.gm_adventure_tools.award_stage_reward(
            context(message),
            {
                "difficulty": "normal",
                "source": "守望会护送委托",
                "evidence": "伊莉雅收下守望会答应的报酬",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertGreater(
            self.app.character_manager.get("伊莉雅").zenit,
            ilya_before,
        )
        self.assertEqual(
            self.app.character_manager.get("洛岚").zenit,
            loran_before,
        )

    def test_unknown_rare_reward_is_rejected_before_mutation(self) -> None:
        message = "首领已经倒下，我们取得不存在的神剑。"
        before = self.app.character_manager.get("伊莉雅").zenit

        receipt = self.service.gm_adventure_tools.award_stage_reward(
            context(message),
            {
                "recipients": ["伊莉雅"],
                "difficulty": "boss",
                "rare_item": "不存在的神剑",
                "source": "钟塔首领",
                "evidence": "首领已经倒下，我们取得不存在的神剑",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "UNKNOWN_REWARD_ITEM")
        self.assertEqual(self.app.character_manager.get("伊莉雅").zenit, before)

    def test_progression_state_and_level_up_are_available_after_session(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.experience_points = 10
        hero.classes = {"武器大师": 1}
        hero.skills = {"碎骨": 1}
        message = "伊莉雅把这一级投入武器大师，学习近战武器精通。"

        before = self.service.gm_adventure_tools.get_progression_state(
            context(message),
            {},
        )
        receipt = self.service.gm_adventure_tools.level_up_character(
            context(message),
            {
                "character_name": "伊莉雅",
                "class_name": "武器大师",
                "skill_name": "近战武器精通",
                "evidence": "伊莉雅把这一级投入武器大师，学习近战武器精通",
            },
        )

        self.assertTrue(before.ok)
        self.assertTrue(before.result["characters"][0]["can_level_up"])
        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(hero.level, 6)
        self.assertEqual(hero.experience_points, 0)
        self.assertEqual(hero.classes["武器大师"], 2)
        self.assertEqual(hero.skills["近战武器精通"], 1)

    def test_level_up_is_rejected_while_an_adventure_session_is_active(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.experience_points = 10
        hero.classes = {"武器大师": 1}
        hero.skills = {"碎骨": 1}
        self.app.start_session_tracking("s1", participating_pcs=["伊莉雅"])
        message = "伊莉雅现在把这一级投入武器大师。"

        receipt = self.service.gm_adventure_tools.level_up_character(
            context(message),
            {
                "character_name": "伊莉雅",
                "class_name": "武器大师",
                "skill_name": "近战武器精通",
                "evidence": "伊莉雅现在把这一级投入武器大师",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.error_code, "LEVEL_UP_DURING_ACTIVE_SESSION")
        self.assertEqual(hero.level, 5)
        self.assertEqual(hero.experience_points, 10)

    def test_level_up_cannot_control_another_players_character(self) -> None:
        hero = self.app.character_manager.get("伊莉雅")
        hero.experience_points = 10
        hero.classes = {"武器大师": 1}
        hero.skills = {"碎骨": 1}
        self.app.world_state.world_profile.hero_drafts["owner"] = type(
            "Draft",
            (),
            {"player_name": "白河", "hero_name": "伊莉雅"},
        )()
        message = "阿凛替伊莉雅选择武器大师的近战武器精通。"

        receipt = self.service.gm_adventure_tools.level_up_character(
            context(message, speaker="阿凛"),
            {
                "character_name": "伊莉雅",
                "class_name": "武器大师",
                "skill_name": "近战武器精通",
                "evidence": "阿凛替伊莉雅选择武器大师的近战武器精通",
            },
        )

        self.assertFalse(receipt.ok)
        self.assertEqual(
            receipt.error_code,
            "CHARACTER_NOT_CONTROLLED_BY_SPEAKER",
        )
        self.assertEqual(hero.level, 5)


if __name__ == "__main__":
    unittest.main()
