import unittest

from fu_gm.models import SceneRecord, SceneType
from fu_gm.play_process_guidance import build_play_process_guidance, summarize_play_process_for_prompt


class PlayProcessGuidanceTests(unittest.TestCase):
    def test_free_scene_recommends_framing_next_scene(self) -> None:
        guidance = build_play_process_guidance(None)

        self.assertIn("没有明确场景", guidance.current_focus)
        self.assertTrue(any("时间" in item and "地点" in item for item in guidance.scene_type_guidance))
        self.assertTrue(any("场景" in item for item in guidance.scene_flow))

    def test_interlude_guidance_keeps_slow_passages_summary_based(self) -> None:
        scene = SceneRecord(
            name="穿越苍蓝森林",
            scene_type=SceneType.INTERLUDE,
            location="苍蓝森林",
            objective="抵达旧王国水道",
        )

        guidance = summarize_play_process_for_prompt(scene)

        self.assertIn("幕间", guidance["current_focus"])
        self.assertTrue(any("每名玩家" in item for item in guidance["scene_type_guidance"]))
        self.assertTrue(any("放大成标准场景" in item for item in guidance["scene_type_guidance"]))

    def test_conflict_guidance_prioritizes_turns_goals_and_tactical_information(self) -> None:
        scene = SceneRecord(name="王庭审判", scene_type=SceneType.CONFLICT, objective="说服女王停战")

        guidance = build_play_process_guidance(scene, conflict_active=True)

        self.assertIn("冲突场景", guidance.current_focus)
        self.assertTrue(any("交替行动" in item for item in guidance.scene_type_guidance))
        self.assertTrue(any("战术信息" in item for item in guidance.scene_type_guidance))

    def test_gm_scene_guidance_warns_against_replacing_player_agency(self) -> None:
        scene = SceneRecord(name="反派的黑塔", scene_type=SceneType.GM)

        guidance = build_play_process_guidance(scene)

        self.assertTrue(any("过场动画" in item for item in guidance.scene_type_guidance))
        self.assertTrue(any("玩家选择" in item for item in guidance.scene_type_guidance))


if __name__ == "__main__":
    unittest.main()
