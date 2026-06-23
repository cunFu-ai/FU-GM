import unittest

from fu_gm.message_arbiter import HeuristicMessageArbiter


class HeuristicMessageArbiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.arbiter = HeuristicMessageArbiter(gm_aliases=["时悠", "GM"])

    def test_natural_game_action_routes_to_fu_gm(self) -> None:
        decision = self.arbiter.decide("我攻击宝箱王", speaker="阿凛", is_group=True)
        self.assertEqual(decision.target, "fu_gm")
        self.assertEqual(decision.mode, "game")
        self.assertTrue(decision.stop_astrbot)

    def test_table_discussion_stays_silent(self) -> None:
        decision = self.arbiter.decide("我们要不要先调查宝箱？", speaker="白河", is_group=True)
        self.assertEqual(decision.target, "silent")
        self.assertTrue(decision.stop_astrbot)

    def test_direct_gm_address_routes_to_casual(self) -> None:
        decision = self.arbiter.decide("时悠，还记得宝箱王吗？", speaker="阿凛", is_group=True)
        self.assertEqual(decision.target, "fu_gm")
        self.assertEqual(decision.mode, "casual")

    def test_unrelated_message_goes_to_astrbot(self) -> None:
        decision = self.arbiter.decide("今天晚饭吃什么", speaker="阿凛", is_group=True)
        self.assertEqual(decision.target, "astrbot")
        self.assertFalse(decision.stop_astrbot)

    def test_natural_safety_declaration_routes_to_safety(self) -> None:
        decision = self.arbiter.decide("我不希望出现蜘蛛", speaker="阿凛", is_private=True, is_group=False)
        self.assertEqual(decision.target, "fu_gm")
        self.assertEqual(decision.mode, "safety")

    def test_session_zero_world_creation_routes_to_session_zero(self) -> None:
        decision = self.arbiter.decide("我的角色想做失国公主", speaker="阿凛", is_group=True)
        self.assertEqual(decision.target, "fu_gm")
        self.assertEqual(decision.mode, "session_zero")

    def test_class_options_question_routes_to_fu_gm(self) -> None:
        decision = self.arbiter.decide("有什么职业可以选择？", speaker="阿凛", is_group=True)
        self.assertEqual(decision.target, "fu_gm")
        self.assertEqual(decision.mode, "casual")
        self.assertTrue(decision.stop_astrbot)

    def test_open_session_zero_filters_chatter_but_accepts_substantive_contribution(self) -> None:
        self.assertFalse(self.arbiter.should_accept_open_session_zero_input("哈哈哈"))
        self.assertFalse(self.arbiter.should_accept_open_session_zero_input("我们要不要先等等白河？"))
        self.assertFalse(self.arbiter.should_accept_open_session_zero_input("@白河 你先说吧"))
        self.assertTrue(self.arbiter.should_accept_open_session_zero_input("我希望是个有地下城宝箱和奇遇的奇幻故事"))
        self.assertTrue(self.arbiter.should_accept_open_session_zero_input("地下城宝箱奇遇挺好，就这个方向吧"))
        self.assertTrue(self.arbiter.should_accept_open_session_zero_input("失国公主，想找回被夺走的王国"))
        self.assertTrue(self.arbiter.should_accept_open_session_zero_input("这个开场可以从港口酒馆开始吗？"))

    def test_at_other_player_does_not_count_as_gm_address(self) -> None:
        decision = self.arbiter.decide("@白河 你来决定职业吧", speaker="阿凛", is_group=True)
        self.assertNotEqual(decision.target, "fu_gm")


if __name__ == "__main__":
    unittest.main()
