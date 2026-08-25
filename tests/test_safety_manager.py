import unittest

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


class SafetyManagerTests(unittest.TestCase):
    def test_explicit_line_and_veil_keep_each_labelled_scope_intact(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations(
            "界限：不要出现真实酷刑和性暴力细节。帷幕：亲密内容淡出处理，儿童受害只作为远景背景不要描写过程。"
        )

        self.assertEqual(
            declarations,
            [
                ("line", "真实酷刑和性暴力细节"),
                (
                    "veil",
                    "亲密内容淡出处理，儿童受害只作为远景背景不要描写过程",
                ),
            ],
        )

    def test_explicit_veil_treatment_is_not_reparsed_as_an_extra_line(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations(
            "我这边加一条帷幕：严重或残酷的身体伤害可以作为结果存在，"
            "但不要具体描写过程和伤口。"
        )

        self.assertEqual(
            declarations,
            [
                (
                    "veil",
                    "严重或残酷的身体伤害可以作为结果存在，但不要具体描写过程和伤口",
                )
            ],
        )

    def test_explicit_chinese_enumerations_are_split_without_intro_text(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations(
            "我先把底线说清：性暴力、酷刑和现实仇恨煽动是界限；"
            "儿童遇险、身体病变和亲密内容请帷幕淡出。"
        )

        self.assertEqual(
            declarations,
            [
                ("line", "性暴力"),
                ("line", "酷刑"),
                ("line", "现实仇恨煽动"),
                ("veil", "儿童遇险"),
                ("veil", "身体病变"),
                ("veil", "亲密内容"),
            ],
        )

    def test_natural_veil_does_not_keep_unfinished_preference_connector(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations("太血腥、虐待、过度绝望的内容我希望少一点或者淡出。")

        self.assertEqual(declarations, [("veil", "太血腥、虐待、过度绝望的内容")])

    def test_veil_treatment_continuation_is_not_recorded_as_a_new_line(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations(
            "我不希望出现蜘蛛；身体恐怖请淡出处理，不要细讲。"
        )

        self.assertEqual(
            declarations,
            [("line", "蜘蛛"), ("veil", "身体恐怖")],
        )

    def test_npc_consent_question_is_not_a_safety_declaration(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations("我想先确认他愿不愿意让我帮他稳一稳呼吸。")

        self.assertEqual(declarations, [])

    def test_opening_pacing_preference_is_not_a_safety_declaration(self) -> None:
        from fu_gm.safety_parser import extract_safety_declarations

        declarations = extract_safety_declarations(
            "我希望整体有史诗奇幻的希望感，但别一上来就是拯救世界。"
            "先从边境小事开始，真相到中期再掀开。"
        )

        self.assertEqual(declarations, [])

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

    def test_parse_explicit_colloquial_safety_declarations(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare(
            "阿凛",
            "加个界限，蜘蛛。补个帷幕，儿童遇险。亲密内容作为帷幕。把血腥细节设为界限。",
        )

        self.assertEqual([result.declaration_type for result in results], ["line", "veil", "veil", "line"])
        self.assertIn("蜘蛛", world_state.world_profile.safety_lines)
        self.assertIn("血腥细节", world_state.world_profile.safety_lines)
        self.assertIn("儿童遇险", world_state.world_profile.safety_veils)
        self.assertIn("亲密内容", world_state.world_profile.safety_veils)

    def test_negative_safety_label_statement_is_not_declaration(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "蜘蛛不是我的界限，只是角色害怕。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])

    def test_map_shape_preference_is_not_safety_declaration(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "我不想要奇怪的环形世界，地图就正常大陆吧。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])
        self.assertEqual(world_state.world_profile.safety_veils, [])

    def test_tone_preference_is_not_safety_declaration(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("时雨", "我希望保留明亮冒险感，不要全程压抑。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])
        self.assertEqual(world_state.world_profile.safety_veils, [])

    def test_party_tone_language_is_not_misrecorded_as_safety_lines(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare(
            "白河",
            "王道热血可以有，但别只剩爽点；队伍里可以争论，关键是别互相拆台。",
        )

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])

        followup = manager.parse_and_declare(
            "阿凛",
            "画面可以燃一点，但别太浮夸。雷点的话我暂时没有特别强的，不太想全程压抑。",
        )
        concise = manager.parse_and_declare("白河", "战斗可以燃、像传说但别飘。")
        self.assertEqual(followup, [])
        self.assertEqual(concise, [])

        difficulty = manager.parse_and_declare(
            "白河",
            "王道热血可以有，但最好别一路开无双，过程里保留一些现实代价和选择难题。",
        )
        self.assertEqual(difficulty, [])
        self.assertNotIn("一路开无双", world_state.world_profile.safety_lines)

    def test_mixed_explicit_line_and_natural_veil_do_not_duplicate(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare(
            "南星",
            "我补一条界限：不出现性暴力和针对儿童的残酷虐待；身体病变与亲密场景放在帷幕后淡出。",
        )

        self.assertEqual(
            [(result.declaration_type, result.item) for result in results],
            [
                ("line", "性暴力和针对儿童的残酷虐待"),
                ("veil", "身体病变与亲密场景"),
            ],
        )

    def test_explicit_veil_stops_before_following_tone_and_campaign_pacing(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare(
            "阿凛",
            "界限：不详细描写性暴力、酷刑、现实仇恨煽动。"
            "帷幕：儿童遇险、身体病变、亲密内容淡出处理。"
            "我希望故事有史诗奇幻的希望感，中期能揭开颠覆力量平衡的真相；但主线从边境驿站开始。",
        )

        self.assertEqual(
            [(result.declaration_type, result.item) for result in results],
            [
                ("line", "性暴力、酷刑、现实仇恨煽动"),
                ("veil", "儿童遇险、身体病变、亲密内容"),
            ],
        )
        self.assertFalse(any("希望感" in item or "中期" in item for item in world_state.world_profile.safety_veils))

    def test_typed_safety_tool_content_removes_treatment_suffix_as_a_unit(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        line = manager.declare(
            "line",
            "性暴力、酷刑、现实仇恨煽动不作详细描写",
            speaker="阿凛",
        )
        veil = manager.declare(
            "veil",
            "儿童遇险、身体病变、亲密内容都淡出处理",
            speaker="阿凛",
        )

        self.assertEqual(line.item, "性暴力、酷刑、现实仇恨煽动")
        self.assertEqual(veil.item, "儿童遇险、身体病变、亲密内容")

    def test_table_coordination_is_not_safety_declaration(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "我刚泡好茶，大家今天慢慢来，别急。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])
        self.assertEqual(world_state.world_profile.safety_veils, [])

    def test_non_safety_word_bieren_is_not_split_as_line(self) -> None:
        world_state = WorldState()
        manager = SafetyManager(world_state)

        results = manager.parse_and_declare("阿凛", "她不会把别人交给财团换安全。")

        self.assertEqual(results, [])
        self.assertEqual(world_state.world_profile.safety_lines, [])

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

    def test_orchestrator_exposes_safety_guidance_to_game_panel(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=0)
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=SessionZeroManager(world_state),
        )

        app.declare_safety_line("蜘蛛", speaker="阿凛")
        panel = app.build_panel("继续")

        self.assertIn("蜘蛛", panel.safety_guidance)
        self.assertIn("界限", app.safety_guidance())


if __name__ == "__main__":
    unittest.main()
