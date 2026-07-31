import unittest

from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary
from fu_gm.components.npc_continuity_policy import NPCContinuityPolicy
from fu_gm.models import Action, ActionType, Clock


class ClockNarrativeBoundaryTests(unittest.TestCase):
    def test_incomplete_arrival_clock_rejects_arrival_but_allows_distant_signs(self) -> None:
        boundaries = ClockNarrativeBoundary.packet(
            [
                Clock(
                    name="财团巡逻队逼近",
                    max_segments=8,
                    current=1,
                    clock_type="threat",
                    stakes="填满后巡逻队包围驿站。",
                )
            ]
        )

        violation = ClockNarrativeBoundary.violation(
            "几道人影已经停在檐下。白花婆婆说：‘他们到了。’",
            boundaries,
        )

        self.assertIn("仍为 1/8", violation)
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "林口的车灯又亮了一盏，远处车轮声变得更清楚。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "再拖下去，他们就会包围驿站！",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "巡逻队即将包围驿站，必须立刻行动。",
                boundaries,
            ),
            "",
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "监察官艾蕾娜终于到了，她把权杖横在门前。",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "白栀婆压低声音：‘辉钢的人到了。’",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "两名财团巡逻员踏上石阶，抬头看向风铃廊。",
                boundaries,
            ),
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "守望会的白栀婆从祭铃室走进风铃廊。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "队伍已经抵达【白花碑驿站·旧路闸门】，会长停在门边。",
                boundaries,
            ),
            "",
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "辉钢财团巡逻队已经在驿站外侧落下了临时检查线。",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "霍阙压低声音：‘巡逻队已经在外缘立停。’",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "前院外，艾蕾娜的巡逻队停步了，灯火在门栓上来回晃动。",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "监察官艾蕾娜停在可听见门内声响的位置，抬手让后面的巡逻者散开封住外沿。",
                boundaries,
            ),
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "巡逻者向两侧散开，驿站外的封锁线当场拉直。",
                boundaries,
            ),
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "巡逻者正在沿外缘展开，封锁线还未合拢。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "再拖下去，外面的封锁线就会合拢。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "监察官当场解除一次临检封锁，让队伍一小时内不受拦截。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "洛岚把旧闸门压回原位，出口从外侧封死，巡逻队无法循通道追来。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "监察官示意巡逻队暂缓撞门，远处的撞门声随即停了。",
                boundaries,
            ),
            "",
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "巡逻队现在退出驿站一小时，暂不封锁白花碑周边。",
                boundaries,
            ),
            "",
        )
        self.assertIn(
            "仍为 1/8",
            ClockNarrativeBoundary.violation(
                "巡逻队已经进入能看见候车厅的位置，但尚未封锁出口。",
                boundaries,
            ),
        )
        self.assertEqual(
            ClockNarrativeBoundary.violation(
                "艾蕾娜说：‘我只能确认动静还在靠近，不能说他们已经到了。’",
                boundaries,
            ),
            "",
        )

    def test_filled_clock_no_longer_blocks_its_consequence(self) -> None:
        boundaries = ClockNarrativeBoundary.packet(
            [
                Clock(
                    name="财团巡逻队逼近",
                    max_segments=8,
                    current=8,
                    clock_type="threat",
                    stakes="填满后巡逻队包围驿站。",
                )
            ]
        )

        self.assertEqual(boundaries, [])
        self.assertEqual(ClockNarrativeBoundary.violation("巡逻队已经包围驿站。", boundaries), "")

    def test_clock_repair_preserves_the_answer_while_dropping_early_arrival(self) -> None:
        boundaries = ClockNarrativeBoundary.packet(
            [
                Clock(
                    name="财团巡逻队逼近",
                    max_segments=8,
                    current=1,
                    clock_type="threat",
                    stakes="填满后巡逻队包围驿站。",
                )
            ]
        )
        action = Action(
            ActionType.NARRATE,
            {
                "npc_speech_plan": {
                    "speech_act": "answer",
                    "direct_answer": "巡逻队已经到了。先把旅人带进登记小室，我会让巡守开旧路。",
                    "facts_to_share": [],
                },
                "resolved_world_response_contract": ["会长说明安全离开的要求"],
            },
        )

        repaired = NPCContinuityPolicy.prune_clock_boundary_action(action, boundaries)

        self.assertIsNotNone(repaired)
        direct = repaired.parameters["npc_speech_plan"]["direct_answer"]
        self.assertNotIn("已经到了", direct)
        self.assertIn("登记小室", direct)
        self.assertEqual(
            repaired.parameters["resolved_world_response_contract"],
            ["会长说明安全离开的要求"],
        )


if __name__ == "__main__":
    unittest.main()
