import unittest

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.safety_manager import SafetyManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import WorldSheet
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator


class SafetyManagerTests(unittest.TestCase):
    def test_declares_lines_and_veils_without_asking_why(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        line = manager.declare_line("蜘蛛", speaker="阿凛")
        veil = manager.declare_veil("儿童遇险", speaker="白河")

        self.assertTrue(line.accepted)
        self.assertTrue(veil.accepted)
        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)
        self.assertIn("不会出现在游戏中", line.message)
        self.assertIn("幕后", veil.message)
        self.assertNotIn("为什么", line.message + veil.message)

    def test_safety_declarations_sync_to_world_sheet_and_guidance(self) -> None:
        world_state = WorldState()
        world_state.apply_world_sheet(WorldSheet(campaign_title="永雨之下"))
        manager = SafetyManager(world_state)

        manager.declare_line("详细酷刑")
        manager.declare_veil("不健康关系")
        guidance = manager.render_guidance()

        self.assertIn("详细酷刑", world_state.world_sheet.safety_lines)
        self.assertIn("不健康关系", world_state.world_sheet.safety_veils)
        self.assertIn("绝不出现", guidance)
        self.assertIn("不得明确描写", guidance)
        self.assertIn("不要追问", guidance)

    def test_parse_and_review_safety_content(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "界限：蜘蛛。帷幕：儿童遇险。")
        review = manager.review_content("蜘蛛和儿童遇险都被提到了。")

        self.assertEqual([result.declaration_type for result in results], ["line", "veil"])
        self.assertEqual(review["line_conflicts"], ["蜘蛛"])
        self.assertEqual(review["veil_matches"], ["儿童遇险"])

    def test_parse_natural_language_safety_declarations(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare(
            "阿凛",
            "我不希望出现蜘蛛这种内容。儿童遇险能不能淡出处理？不要详细描写不健康关系。",
        )

        self.assertEqual([result.declaration_type for result in results], ["line", "veil", "veil"])
        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)
        self.assertIn("不健康关系", world_state.world_profile.safety_veils)

    def test_map_shape_preference_is_not_safety_declaration(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "我不想要奇怪的环形世界，地图就正常大陆吧。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])
        self.assertEqual(world_state.world_profile.safety_veils, [])

    def test_parse_natural_language_discomfort_and_fade_to_black(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("白河", "蜘蛛我接受不了，亲密场景一笔带过。")

        self.assertEqual([result.declaration_type for result in results], ["line", "veil"])
        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("亲密场景", world_state.world_profile.safety_veils)

    def test_anonymous_safety_declaration_does_not_store_speaker_name(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "我不希望出现蜘蛛，儿童遇险请带过。", anonymous=True)

        self.assertEqual([result.declaration_type for result in results], ["line", "veil"])
        self.assertTrue(all(result.anonymous for result in results))
        self.assertTrue(all(result.speaker == "" for result in results))
        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)
        self.assertFalse(any("阿凛" in memory for memory in world_state.memories))
        self.assertTrue(any("匿名玩家声明界限：蜘蛛" in memory for memory in world_state.memories))

    def test_parse_natural_language_decline_without_labels_in_session_zero(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=SessionZeroManager(world_state),
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        app.start_session_zero(participants=["阿凛"])
        app.discuss_session_zero("阿凛", "我不希望出现蜘蛛，儿童遇险请带过。")

        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)
        self.assertNotIn("我不希望出现蜘蛛，儿童遇险请带过。", world_state.world_profile.safety_veils)

    def test_orchestrator_exposes_safety_guidance_to_game_panel(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=SessionZeroManager(world_state),
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        app.declare_safety_line("蜘蛛", speaker="阿凛")
        panel = app.build_panel("继续")

        self.assertIn("蜘蛛", panel.safety_guidance)
        self.assertIn("界限", app.safety_guidance())

    def test_session_zero_discussion_auto_records_explicit_safety_declarations(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=SessionZeroManager(world_state),
            session_zero_facilitator=HeuristicSessionZeroFacilitator(),
        )

        app.start_session_zero(participants=["阿凛"])
        app.discuss_session_zero("阿凛", "界限：蜘蛛。帷幕：儿童遇险。")

        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)


if __name__ == "__main__":
    unittest.main()
