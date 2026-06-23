import unittest

from fu_gm.main import build_demo_app


class DemoFlowTests(unittest.TestCase):
    def test_demo_attack_flow_produces_combat_text(self) -> None:
        app = build_demo_app(use_llm=False)
        text = app.run_turn("玩家[瓦莉亚]: 我要用雷电魔法攻击机甲！")
        self.assertIn("【战斗结算】", text)
        self.assertIn("帝国机甲", text)
        self.assertIn("伤害", text)

    def test_story_change_consumes_fabula_point(self) -> None:
        app = build_demo_app(use_llm=False)
        text = app.run_turn("我要消耗 1 点物语点，设定这里有一条密道。")
        self.assertIn("【物语改写】", text)
        self.assertIn("密道", text)


if __name__ == "__main__":
    unittest.main()
