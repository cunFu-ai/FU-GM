import unittest

from fu_gm.expressor import Expressor, LLMExpressor
from fu_gm.models import Action, ActionResolution, ActionType, ClockChange, ResourceChange, RollOutcome


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.content


class ExpressorTests(unittest.TestCase):
    def test_roll_render_includes_dice_subtotal_modifier_and_target(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.REQUEST_ROLL, parameters={}),
            rules_text="洛岚进行检定。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["DEX", "INS"],
                    dice=[(8, 5), (10, 7)],
                    total=14,
                    modifier=2,
                    high_roll=7,
                    target_number=10,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="古代门锁",
                    reason="开锁",
                )
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("属性【DEX+INS】", rendered)
        self.assertIn("掷骰 d8=5 + d10=7 = 12", rendered)
        self.assertIn("修正值 +2", rendered)
        self.assertIn("结算值 14 vs DL 10", rendered)

    def test_static_heal_render_includes_fixed_base_and_actual_recovery(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.SPELL, parameters={}),
            rules_text="伊莉雅 受到【治愈术】影响，规则恢复量 40 点 HP；HP 63->80，实际恢复 17 点。",
            payload={
                "resource_change": ResourceChange("赛璃", "mp", -10, 40, 30, "施放【治愈术】消耗 MP。"),
                "spell_name": "治愈术",
                "healing_change": ResourceChange("伊莉雅", "hp", 17, 63, 80, "【治愈术】恢复生命值。"),
                "spell_fixed_effect": {"kind": "heal", "base_amount": 40, "actual_amount": 17, "targets": ["伊莉雅"]},
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("规则恢复量 40", rendered)
        self.assertIn("实际恢复 17", rendered)
        self.assertIn("赛璃 消耗 10 点 MP", rendered)

    def test_llm_expressor_preserves_canonical_rules_panel_and_drops_math_hallucination(self) -> None:
        client = FakeClient("掷骰结果：1d10 = 10，1d8 = 8。最终结算值：6。\n钟声沿着盾面扩散开。")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.REQUEST_ROLL, parameters={}),
            rules_text="洛岚进行检定。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "DEX"],
                    dice=[(10, 1), (8, 5)],
                    total=6,
                    modifier=0,
                    high_roll=5,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="仪式：风铃回声",
                    reason="推进仪式",
                )
            },
        )

        rendered = expressor.render(resolution)

        self.assertIn("掷骰 d10=1 + d8=5 = 6", rendered)
        self.assertNotIn("1d10 = 10", rendered)
        self.assertIn("钟声沿着盾面扩散开。", rendered)
        prompt_message = client.calls[0]["messages"][-1]
        if hasattr(prompt_message, "content"):
            prompt_content = prompt_message.content
        else:
            prompt_content = prompt_message["content"]
        self.assertIn("【规则面板】", prompt_content)

    def test_ritual_critical_success_is_visibly_highlighted(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.CONTRIBUTE_RITUAL, parameters={}),
            rules_text="赛璃推进仪式。",
            payload={
                "roll": RollOutcome(
                    actor="赛璃",
                    attributes=["INS", "WLP"],
                    dice=[(10, 7), (8, 7)],
                    total=14,
                    modifier=0,
                    high_roll=7,
                    target_number=10,
                    success=True,
                    critical_success=True,
                    fumble=False,
                    opportunity_count=1,
                    target="仪式：风铃回声",
                    reason="推进仪式",
                ),
                "clock_change": ClockChange(
                    clock_name="仪式：风铃回声",
                    before=0,
                    after=2,
                    delta=2,
                    max_segments=4,
                    reason="大成功推进仪式。",
                ),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("大成功", rendered)
        self.assertIn("叙事高光", rendered)
        self.assertIn("产生 1 次机会", rendered)

    def test_llm_expressor_deduplicates_repeated_narrative_line(self) -> None:
        line = "赛璃抬手引来安静的魂息，把疗愈的光轻轻落在伊莉雅肩头。"
        client = FakeClient(line)
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.SPELL, parameters={"in_mind_reply": line}),
            rules_text="伊莉雅 受到【治愈术】影响，规则恢复量 40 点 HP；HP 43->60，实际恢复 17 点。",
            payload={
                "resource_change": ResourceChange("赛璃", "mp", -10, 50, 40, "施放【治愈术】消耗 MP。"),
                "spell_name": "治愈术",
                "healing_change": ResourceChange("伊莉雅", "hp", 17, 43, 60, "【治愈术】恢复生命值。"),
                "spell_fixed_effect": {"kind": "heal", "base_amount": 40, "actual_amount": 17, "targets": ["伊莉雅"]},
            },
        )

        rendered = expressor.render(resolution)

        self.assertEqual(rendered.count(line), 1)

    def test_ritual_waiting_render_does_not_require_ritual_result(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.CAST_RITUAL, parameters={}),
            rules_text="仪式【风铃回声】还不能完成：命刻【仪式：风铃回声】当前 3/4，还差 1 格。这不是行动失败；需要继续推进仪式命刻。",
            payload={"ritual_waiting": True},
        )

        rendered = Expressor().render(resolution)

        self.assertIn("【仪式等待】", rendered)
        self.assertIn("还差 1 格", rendered)
        self.assertNotIn("KeyError", rendered)


if __name__ == "__main__":
    unittest.main()
