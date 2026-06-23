from __future__ import annotations

import unittest

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.models import ActionType, GamePanel


def panel(message: str) -> GamePanel:
    return GamePanel(
        game_phase="标准场景",
        active_clocks=[],
        pc_status=["阿凛: HP 40/40, MP 40/40"],
        enemy_status=["帝国机甲: HP 80/80"],
        recent_chat=f"阿凛: {message}",
    )


class HeuristicActionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain = HeuristicActionBrain()

    def test_observing_chest_stays_narrative(self) -> None:
        action = self.brain.decide(panel("我先观察一下这个宝箱，有没有什么特别的纹路？"))

        self.assertEqual(action.action_type, ActionType.NARRATE)
        self.assertIn("不触发硬规则结算", action.parameters["summary"])

    def test_opening_chest_requires_explicit_action(self) -> None:
        action = self.brain.decide(panel("我打开宝箱。"))

        self.assertEqual(action.action_type, ActionType.OPEN_CHEST)

    def test_looking_around_shop_stays_narrative(self) -> None:
        action = self.brain.decide(panel("我去商店看看货架上都有什么。"))

        self.assertEqual(action.action_type, ActionType.NARRATE)

    def test_buying_item_still_uses_shop_action(self) -> None:
        action = self.brain.decide(panel("我购买一把钢匕首。"))

        self.assertEqual(action.action_type, ActionType.SHOP)
        self.assertEqual(action.parameters["item_name"], "钢匕首")

    def test_investigating_scene_detail_stays_narrative(self) -> None:
        action = self.brain.decide(panel("我调查一下走廊墙画上的线索。"))

        self.assertEqual(action.action_type, ActionType.NARRATE)

    def test_investigating_enemy_still_uses_investigate_action(self) -> None:
        action = self.brain.decide(panel("我调查帝国机甲的弱点。"))

        self.assertEqual(action.action_type, ActionType.INVESTIGATE)

    def test_player_added_world_detail_uses_story_change(self) -> None:
        action = self.brain.decide(panel("我补充一个世界细节：白花碑驿站由白花守望会管理，他们知道避开财团关卡的旧路。"))

        self.assertEqual(action.action_type, ActionType.ACCEPT_STORY_CHANGE)
        self.assertEqual(action.parameters["fabula_cost"], 1)
        self.assertIn("白花碑驿站", action.parameters["fact"])


if __name__ == "__main__":
    unittest.main()
