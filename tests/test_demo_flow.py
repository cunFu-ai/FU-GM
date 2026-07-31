import unittest

from fu_gm.main import build_demo_app
from fu_gm.models import Action, ActionType


class DemoFlowTests(unittest.TestCase):
    def test_demo_attack_flow_produces_combat_text(self) -> None:
        app = build_demo_app(use_llm=False)
        provisional = app.run_structured_turn(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "瓦莉亚",
                    "target": "帝国机甲",
                    "attributes": ["DEX", "MIG"],
                    "damage_type": "lightning",
                },
            ),
            "玩家[瓦莉亚]: 我要用雷电魔法攻击机甲！",
        )
        self.assertIn("骰面先停在这里", provisional)
        window = app.decision_window_manager.pending(owner="瓦莉亚", blocking_only=True)[0]
        text = app.run_structured_turn(
            Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": "瓦莉亚",
                    "window_id": window.window_id,
                    "choice": "accept_result",
                    "selected_option": {"choice": "accept_result"},
                    "post_check_acceptance": True,
                },
            ),
            "瓦莉亚: 我接受这次结果，不重掷。",
        )
        self.assertIn("瓦莉亚 对 帝国机甲 的检定", text)
        self.assertIn("帝国机甲", text)
        self.assertIn("伤害", text)
        self.assertNotIn("【战斗结算】", text)

    def test_story_change_consumes_fabula_point(self) -> None:
        app = build_demo_app(use_llm=False)
        text = app.run_structured_turn(
            Action(
                ActionType.ACCEPT_STORY_CHANGE,
                {
                    "actor": "瓦莉亚",
                    "target": "瓦莉亚",
                    "fabula_cost": 1,
                    "fact": "断桥旁有一条可供队伍使用的密道。",
                },
            ),
            "我要消耗 1 点物语点，设定这里有一条密道。",
        )
        self.assertIn("密道", text)
        self.assertNotIn("我要消耗", text)
        self.assertNotIn("【物语改写】", text)


if __name__ == "__main__":
    unittest.main()
