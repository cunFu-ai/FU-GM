import json
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


class SequenceFakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.contents.pop(0)


class ExpressorTests(unittest.TestCase):
    def test_llm_expressor_never_narrates_a_provisional_check_outcome(self) -> None:
        client = FakeClient("巡逻灯影没有发现登记小室里的人影。")
        expressor = LLMExpressor(
            client=client,
            model="fake-model",
            allow_fallback=False,
        )
        resolution = ActionResolution(
            action=Action(
                ActionType.HINDER,
                {
                    "actor": "洛岚",
                    "success_observation": "巡逻灯影没有发现登记小室里的人影。",
                    "failure_consequence": "巡逻队发现门缝里的人影。",
                },
            ),
            rules_text="",
            payload={
                "check_result_provisional": True,
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["敏捷", "洞察"],
                    dice=[(8, 7), (10, 7)],
                    modifier=0,
                    total=14,
                    high_roll=7,
                    target_number=7,
                    success=True,
                    critical_success=True,
                    fumble=False,
                ),
            },
        )

        rendered = expressor.render(resolution)

        self.assertIn("结算值 14", rendered)
        self.assertNotIn("没有发现", rendered)
        self.assertNotIn("发现门缝", rendered)
        self.assertEqual(client.calls, [])

    def test_llm_expressor_includes_gm_personality_without_overriding_rules(self) -> None:
        client = FakeClient("窗外的风铃轻响了一声。")
        expressor = LLMExpressor(
            client=client,
            model="fake-model",
            allow_fallback=False,
            gm_personality_prompt="语气温和，偶尔做一句简短点评；普通人不说谜语。",
        )
        resolution = ActionResolution(
            action=Action(ActionType.GUARD, {"actor": "伊莉雅"}),
            rules_text="伊莉雅执行防御行动。",
            payload={},
        )

        rendered = expressor.render(resolution)

        self.assertIn("伊莉雅执行防御行动", rendered)
        system_prompt = str(client.calls[0]["messages"][0].content)
        self.assertIn("普通人不说谜语", system_prompt)
        self.assertIn("不覆盖规则与事实", system_prompt)

    def test_llm_expressor_serializes_dataclasses_nested_in_payload_lists(self) -> None:
        change = ClockChange(
            clock_name="财团巡逻队逼近",
            before=2,
            after=3,
            delta=1,
            max_segments=6,
            reason="时间流逝",
            clock_type="threat",
        )
        client = FakeClient("")
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "风铃回应。"}),
            rules_text="",
            payload={"auto_clock_changes": [change]},
        )

        rendered = expressor.render(resolution)

        self.assertIn("风铃回应", rendered)
        prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[0]["messages"]
        )
        self.assertIn('"clock_name": "财团巡逻队逼近"', prompt)

    def test_auto_clock_board_is_compact_and_not_duplicated(self) -> None:
        change = ClockChange(
            clock_name="财团巡逻队逼近",
            before=2,
            after=3,
            delta=1,
            max_segments=6,
            reason="自动推进",
            clock_type="threat",
        )
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "巡守终于作答。"}),
            rules_text="",
            payload={
                "turn_auto_advanced": True,
                "auto_clock_changes": [change],
                "clock_progress": ["【财团巡逻队逼近】3/6"],
            },
        )

        rendered = Expressor().render(resolution)

        self.assertEqual(rendered.count("【财团巡逻队逼近】3/6"), 1)
        self.assertNotIn("命刻进度：", rendered)

    def test_auto_clock_completion_is_not_hidden_by_another_active_clock(self) -> None:
        threat_change = ClockChange(
            clock_name="财团巡逻队逼近",
            before=5,
            after=6,
            delta=1,
            max_segments=6,
            reason="时间流逝",
            clock_type="threat",
            stakes="填满后财团巡逻队包围现场。",
        )
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "仪式继续回响。"}),
            rules_text="",
            payload={
                "turn_auto_advanced": True,
                "auto_clock_changes": [threat_change],
                "clock_progress": ["【仪式：风铃回声】3/4。只差最后一点回响。"],
            },
        )

        rendered = Expressor().render(resolution)

        self.assertEqual(rendered.count("【财团巡逻队逼近】6/6"), 1)
        self.assertIn("财团巡逻队包围现场", rendered)
        self.assertEqual(rendered.count("【仪式：风铃回声】3/4"), 1)

    def test_auto_clock_board_does_not_repeat_directly_rendered_clock(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "【仪式：风铃回声】3/4。"}),
            rules_text="",
            payload={
                "turn_auto_advanced": True,
                "clock_progress": ["【仪式：风铃回声】3/4。只差最后一点回响。"],
            },
        )

        rendered = Expressor().render(resolution)

        self.assertEqual(rendered.count("【仪式：风铃回声】3/4"), 1)

    def test_investigation_omits_stale_zero_delta_state_before_auto_tick(self) -> None:
        zero_change = ClockChange(
            clock_name="财团巡逻队逼近",
            before=2,
            after=2,
            delta=0,
            max_segments=6,
            reason="观察不改变距离",
            clock_type="threat",
        )
        auto_change = ClockChange(
            clock_name="财团巡逻队逼近",
            before=2,
            after=3,
            delta=1,
            max_segments=6,
            reason="时间流逝",
            clock_type="threat",
        )
        resolution = ActionResolution(
            action=Action(ActionType.INVESTIGATE, {"target": "财团车辙"}),
            rules_text="洛岚调查车辙。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "INS"],
                    dice=[(10, 1), (10, 5)],
                    total=6,
                    modifier=0,
                    high_roll=5,
                    target_number=7,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="财团车辙",
                    reason="调查",
                ),
                "clock_change": zero_change,
                "turn_auto_advanced": True,
                "auto_clock_changes": [auto_change],
                "clock_progress": ["【财团巡逻队逼近】3/6"],
            },
        )

        rendered = Expressor().render(resolution)

        self.assertNotIn("【财团巡逻队逼近】2/6", rendered)
        self.assertEqual(rendered.count("【财团巡逻队逼近】3/6"), 1)

    def test_sanitizer_fixes_common_hero_name_duplication(self) -> None:
        rendered = Expressor()._sanitize_player_text("伊莉莉雅踏入风铃廊。")

        self.assertIn("伊莉雅踏入风铃廊", rendered)
        self.assertNotIn("伊莉莉雅", rendered)

    def test_sanitizer_fixes_repeated_traveler_typo(self) -> None:
        rendered = Expressor()._sanitize_player_text("失忆的旅旅人抱紧木匣。")

        self.assertEqual(rendered, "失忆的旅人抱紧木匣。")

    def test_sanitizer_removes_explanatory_player_intent_commentary(self) -> None:
        rendered = Expressor().render(
            ActionResolution(
                action=Action(
                    ActionType.NARRATE,
                    parameters={
                        "summary": (
                            "苍祈把注意力转向失忆旅人和旁观者。"
                            "她没有急着替任何人做决定，只是先看一眼旅人的脸色、呼吸和站姿；"
                            "这一步的重点，是确认再拖下去会先伤到谁、谁还能继续撑住。"
                            "旅人攥紧白绳，旁观者的声音低了下去。"
                        )
                    },
                ),
                rules_text="",
                payload={},
            )
        )

        self.assertIn("旅人的脸色", rendered)
        self.assertIn("旅人攥紧白绳", rendered)
        self.assertNotIn("这一步的重点", rendered)
        self.assertNotIn("替任何人做决定", rendered)

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

        self.assertIn("属性【敏捷+洞察】", rendered)
        self.assertIn("掷骰 d8=5 + d10=7 = 12", rendered)
        self.assertIn("修正值 +2", rendered)
        self.assertIn("结算值 14 对抗难度等级 10", rendered)
        self.assertNotIn("【检定】", rendered)
        self.assertNotIn("造成 0 点", rendered)

    def test_objective_failure_renders_clock_state(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.OBJECTIVE,
                parameters={
                    "clock_name": "旧路闸门开启",
                    "failure_consequence": "锈死的机关咬住工具，旧门仍纹丝不动",
                },
            ),
            rules_text="洛岚目标行动失败。命刻 [旧路闸门开启] 仍为 0/6。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["DEX", "INS"],
                    dice=[(8, 1), (10, 5)],
                    total=6,
                    modifier=0,
                    high_roll=5,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="旧路闸门开启",
                    reason="推进目标",
                ),
                "clock_state": {
                    "clock_name": "旧路闸门开启",
                    "current": 0,
                    "max_segments": 6,
                    "clock_type": "objective",
                },
            },
        )

        rendered = Expressor().render(resolution)

        self.assertNotIn("进度未变化", rendered)
        self.assertNotIn("【旧路闸门开启】0/6", rendered)
        self.assertNotIn("若失败", rendered)
        self.assertLess(rendered.index("失败！"), rendered.index("锈死的机关咬住工具"))
        self.assertIn("旧门仍纹丝不动", rendered)

    def test_scene_opening_fallback_uses_visible_situation_instead_of_title_echo(self) -> None:
        rendered = Expressor().render_scene_moment(
            {
                "location": "白花碑驿站",
                "premise": "第一章从白花碑驿站开场：迟响",
                "visible_elements": [
                    "地点：白花碑驿站",
                    "现场：南岸驿站的白花风铃保存着失去的名字。",
                    "现场人物：被护送的失名旅人正在这里等待去路。",
                ],
                "current_pressure": "守门人尚未决定是否开放旧路。",
            }
        )

        self.assertIn("白花风铃", rendered)
        self.assertIn("失名旅人", rendered)
        self.assertNotIn("旧路钥匙", rendered)
        self.assertNotIn("准备把他带到哪里", rendered)
        self.assertNotIn("第一章从", rendered)

    def test_scene_opening_rewrites_until_a_prepared_npc_is_named(self) -> None:
        client = SequenceFakeClient(
            [
                "白花风铃下，收发棚看守白栎按着门闩看向众人。",
                "白花风铃内侧的墨名在暮色里一闪，失忆旅人也被那声迟响惊得抬头。收发棚看守白栎按着门闩，抬眼看向众人。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_image": "白花风铃内侧的墨名",
                "required_opening_elements": ["白花风铃", "失忆旅人"],
                "required_opening_npc_names": ["白栎"],
                "prepared_npcs": [{"name": "白栎", "public_role": "收发棚看守"}],
            }
        )

        self.assertIn("白栎", rendered)
        self.assertIn("失忆旅人", rendered)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(expressor.last_scene_candidates[0].count("失忆旅人"), 0)

    def test_scene_beat_rewrites_player_option_menu_from_speech_intent(self) -> None:
        client = SequenceFakeClient(
            [
                (
                    "黄铜片贴地亮起冷白纹路。若打断它，风铃会把巡逻队引得更近；"
                    "若任它继续，旅人会失去刚说出的方向感。"
                ),
                (
                    "财团使者把黄铜片推进门槛，冷白纹路立刻缠住旅人的影子；"
                    "旅人刚说出的方向感正从记忆里被抽走。"
                ),
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "财团使者仍在门外等答复。",
                "speech_intent": {"avoid": ["替玩家行动", "列出两三个选项"]},
            },
            instruction="让财团使者采取一项已经发生的行动。",
            beat=True,
        )

        self.assertIn("把黄铜片推进门槛", rendered)
        self.assertNotIn("若打断", rendered)
        # Two prose candidates plus the existing continuity-quality audit.
        self.assertEqual(len(client.calls), 3)
        self.assertIn("选项菜单", expressor.last_scene_candidate_diagnostics[0]["boundary_violation"])

    def test_optional_scene_beat_can_disable_internal_rewrites(self) -> None:
        client = SequenceFakeClient(
            [
                (
                    "黄铜片贴地亮起冷白纹路。若打断它，风铃会把巡逻队引得更近；"
                    "若任它继续，旅人会失去刚说出的方向感。"
                ),
                "这一候选不应被请求。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "财团使者仍在门外等答复。",
                "speech_intent": {"avoid": ["替玩家行动", "列出两三个选项"]},
            },
            instruction="让财团使者采取一项已经发生的行动。",
            beat=True,
            max_attempts=1,
        )

        self.assertEqual(rendered, "")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(expressor.last_scene_candidate_diagnostics), 1)

    def test_scene_opening_reports_exact_requirements_when_model_ignores_rewrite(self) -> None:
        client = SequenceFakeClient(
            [
                "白花风铃下只有旅人站着。",
                "白花风铃又响了一声，廊下无人开口。",
                "白花风铃下仍只有旅人站着，廊下无人开口。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        with self.assertRaises(RuntimeError):
            expressor.render_scene_moment(
                {
                    "location": "白花碑驿站",
                    "required_opening_elements": ["白花风铃", "失忆旅人"],
                    "required_opening_npc_names": ["阿芒会长"],
                }
            )

        self.assertIn("NPC【阿芒会长】", expressor.last_error)
        self.assertIn("场景要素【失忆旅人】", expressor.last_error)
        self.assertEqual(len(expressor.last_scene_candidate_diagnostics), 3)
        self.assertIn(
            "NPC【阿芒会长】",
            expressor.last_scene_candidate_diagnostics[-1]["missing_requirements"],
        )
        retry_prompt = str(client.calls[1]["messages"][-1].content)
        self.assertIn("NPC【阿芒会长】", retry_prompt)
        self.assertIn("场景要素【失忆旅人】", retry_prompt)

    def test_scene_opening_name_check_ignores_formatting_spaces(self) -> None:
        client = FakeClient(
            "白花守望会会长林岚站在白花风铃下，失忆旅人阿渡正听着铃声。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花风铃"],
                "required_opening_npc_names": ["白花守望会会长 林岚", "失忆旅人 阿渡"],
            }
        )

        self.assertIn("林岚", rendered)
        self.assertIn("阿渡", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_accepts_unambiguous_short_names_from_prepared_npcs(self) -> None:
        client = FakeClient(
            "岑舟站在白花风铃下，听见迟响后脸色一白；罗绮按着旧路钥牌，转头望向远处灯火。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花风铃"],
                "required_opening_npc_names": ["失忆旅人岑舟", "白花守望会会长罗绮"],
                "prepared_npcs": [
                    {"name": "失忆旅人岑舟", "public_role": "失忆旅人"},
                    {"name": "白花守望会会长罗绮", "public_role": "白花守望会会长"},
                ],
            }
        )

        self.assertIn("岑舟", rendered)
        self.assertIn("罗绮", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_does_not_receive_offstage_npc_library_or_later_arc_secrets(self) -> None:
        client = FakeClient(
            "白花风铃在廊下迟响，失忆旅人岑舟抬起头；白棘会长按住钥牌，远处只有模糊的金属回声。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花风铃"],
                "required_opening_npc_names": ["失忆旅人岑舟", "白棘会长"],
                "prepared_npcs": [
                    {"name": "失忆旅人岑舟", "public_role": "失忆旅人"},
                    {"name": "白棘会长", "public_role": "白花守望会会长"},
                    {"name": "监察官艾蕾娜", "public_role": "财团监察官"},
                ],
                "opening_prepared_npcs": [
                    {"name": "失忆旅人岑舟", "public_role": "失忆旅人"},
                    {"name": "白棘会长", "public_role": "白花守望会会长"},
                ],
                "npc_functions": [
                    "失忆旅人岑舟：记忆受损的护送对象",
                    "白棘会长：决定是否开放旧路",
                    "监察官艾蕾娜：试图截住旅人",
                ],
                "opposition_goal": "监察官艾蕾娜要在巡逻队抵达后扣押旅人。",
                "reversal": "艾蕾娜持有被改写的旧名册。",
                "selected_scene_situation": "旅人听见风铃中的名字，会长仍不愿开放旧路。",
            }
        )

        prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[0]["messages"]
        )
        self.assertIn("白棘会长", rendered)
        self.assertNotIn("监察官艾蕾娜", prompt)
        self.assertNotIn("被改写的旧名册", prompt)
        self.assertIn("required_opening_npc_names是本次开场的演员名单", prompt)

    def test_scene_beat_keeps_full_prepared_npc_reference_library(self) -> None:
        client = FakeClient("远处的金属回声忽然停了一拍，风铃廊里的人都抬起了头。")
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "prepared_npcs": [
                    {"name": "白棘会长", "public_role": "白花守望会会长"},
                    {"name": "监察官艾蕾娜", "public_role": "财团监察官"},
                ],
                "opposition_goal": "监察官艾蕾娜要截断旧路。",
            },
            beat=True,
        )

        prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[0]["messages"]
        )
        self.assertIn("监察官艾蕾娜", prompt)
        self.assertIn("截断旧路", prompt)

    def test_scene_opening_accepts_natural_title_name_order_for_prepared_npc(self) -> None:
        client = FakeClient(
            "白花守望会会长白栎站在风铃廊尽头，抬眼看向失忆旅人。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["失忆旅人"],
                "required_opening_npc_names": ["白栎会长"],
                "prepared_npcs": [
                    {
                        "name": "白栎会长",
                        "public_role": "白花守望会会长",
                        "aliases": [],
                    }
                ],
            }
        )

        self.assertIn("会长白栎", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_does_not_accept_unregistered_short_name(self) -> None:
        client = SequenceFakeClient(
            [
                "岑舟站在白花风铃下，罗绮按着钥牌。",
                "岑舟仍站在白花风铃下，罗绮没有离开。",
                "岑舟与罗绮都在风铃廊里。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        with self.assertRaises(RuntimeError):
            expressor.render_scene_moment(
                {
                    "location": "白花碑驿站",
                    "required_opening_elements": ["白花风铃"],
                    "required_opening_npc_names": ["失忆旅人岑舟", "白花守望会会长罗绮"],
                    "prepared_npcs": [],
                }
            )

        self.assertIn("NPC【失忆旅人岑舟】", expressor.last_error)
        self.assertIn("NPC【白花守望会会长罗绮】", expressor.last_error)

    def test_scene_opening_accepts_a_local_element_without_repeating_map_prefix(self) -> None:
        client = FakeClient("祭铃室里的旧路闸门震下一层灰，白花风铃也随之轻响。")
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花碑驿站旧路", "白花风铃"],
            }
        )

        self.assertIn("旧路闸门", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_accepts_natural_alias_for_prepared_object(self) -> None:
        client = FakeClient(
            "裂口的白花铜铃垂在廊下，失忆旅人阿渡听见铃音后猛地抬头。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花风铃", "失忆旅人"],
                "required_opening_npc_names": ["失忆旅人 阿渡"],
            }
        )

        self.assertIn("白花铜铃", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_accepts_possessive_particle_inside_prepared_element(self) -> None:
        client = FakeClient(
            "旧路闸门外的泥地上，一枚枚辉钢财团的巡逻印记被新雨冲得发亮。"
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花碑驿站旧路", "辉钢财团巡逻印记"],
            }
        )

        self.assertIn("辉钢财团的巡逻印记", rendered)
        self.assertEqual(len(client.calls), 1)

    def test_scene_opening_retry_only_repairs_shot_and_gets_boundary_retry(self) -> None:
        client = SequenceFakeClient(
            [
                "白花风铃在廊下轻响，霍阙按着旧路闸钥看向众人。",
                "白花风铃忽然急响，失忆旅人抬起头；霍阙低声说巡逻队已经在外缘立停。",
                "白花风铃在廊下轻响，失忆旅人抬起头；霍阙按着旧路闸钥，远处只有稀疏的金属回声。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "required_opening_elements": ["白花风铃", "失忆旅人"],
                "required_opening_npc_names": ["霍阙"],
                "clock_boundaries": [
                    {
                        "name": "财团巡逻队逼近",
                        "current": 1,
                        "maximum": 8,
                        "stakes": "填满后巡逻队包围驿站。",
                    }
                ],
            }
        )

        self.assertIn("稀疏的金属回声", rendered)
        self.assertEqual(len(client.calls), 3)
        first_retry = str(client.calls[1]["messages"][-1].content)
        self.assertIn("只补齐缺失对象", first_retry)
        self.assertNotIn("加入一项此前没有公开", first_retry)

    def test_llm_scene_opening_prompt_requires_confirmed_mission_anchors(self) -> None:
        client = FakeClient("守门人按住铜铃，抬眼看向门外的海雾。")
        expressor = LLMExpressor(client=client, model="fake-model")

        expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "mission_anchor": "护送失忆旅人与碎月遗物前往钟鸣公国",
                "visible_elements": ["现场人物：小队护送的失名旅人正在这里等待去路。"],
            }
        )

        prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[0]["messages"]
        )
        self.assertIn("mission_anchor", prompt)
        self.assertIn("护送对象、关键物件或目的地", prompt)
        self.assertIn("不得让该威胁已经停在门外", prompt)

    def test_llm_scene_opening_marks_title_like_response_as_fallback(self) -> None:
        client = FakeClient("钟鸣公国。")
        expressor = LLMExpressor(client=client, model="fake-model")

        rendered = expressor.render_scene_moment(
            {
                "location": "钟鸣公国",
                "visible_elements": ["正午大钟下正在举行公开听证。"],
                "current_pressure": "财团代理人拒绝出示文书。",
            }
        )

        self.assertNotEqual(rendered, "钟鸣公国。")
        self.assertTrue(expressor.last_used_fallback)
        self.assertIn("过短", expressor.last_error)

    def test_llm_scene_transition_rewrites_semantic_repetition(self) -> None:
        client = SequenceFakeClient(
            [
                "木牌边缘的赤羽纹样又在火光里露出来，失名旅人再次偏头看向北岸。",
                '{"adds_new_change":false,"reason":"只重复赤羽纹样和旅人的既有反应"}',
                "会长把封路记录交给一名见习者，命他从后窗送往钟鸣公国；院外同时响起第一声财团盘查哨。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"reason":"出现了送信与盘查哨"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "mission_anchor": "保护失名旅人与封路记录",
                "recent_public_context": (
                    "木牌边缘露出褪色的赤羽纹样。失名旅人听见风铃后偏头看向北岸。"
                ),
            },
            instruction="承接上一幕，让局势发生此前没有公开的新变化。",
            beat=True,
        )

        self.assertIn("后窗送往钟鸣公国", rendered)
        self.assertIn("第一声财团盘查哨", rendered)
        self.assertNotIn("再次偏头", rendered)
        self.assertEqual(len(client.calls), 4)

    def test_scene_opening_uses_continuity_change_audit_without_rejecting_a_new_shot(self) -> None:
        client = FakeClient("晨雾压在钟鸣公国的石桥上，听证钟刚敲过第一声，护送的失名旅人就在英雄们身边。")
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "钟鸣公国",
                "mission_anchor": "护送失名旅人参加公开听证",
                "recent_public_context": "上一幕结束时，众人已经抵达钟鸣公国城门。",
            },
        )

        self.assertIn("听证钟", rendered)
        self.assertEqual(len(client.calls), 2)

    def test_scene_transition_accepts_a_planned_new_situation_even_when_landmarks_repeat(self) -> None:
        candidate = (
            "白花碑驿站东侧的白色石碑前，裹着黑蜡的风铃仍没有声音，记忆罐外壁的霜纹却忽然一跳。"
            "隔离帘后，阿雾正把小布袋塞进木梁缝里；白穗转头按住她的手：‘现在就停。’"
        )
        client = SequenceFakeClient(
            [
                candidate,
                '{"adds_new_change":false,"honors_consequences":true,'
                '"fulfills_requested_change":true,"requested_change_already_public":false,'
                '"reason":"沿用了石碑、风铃和记忆罐，但实际演出了阿雾藏匿布袋的新局面"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站·旧候车廊与临时隔离帘之间",
                "recent_public_context": (
                    "众人已经见过东侧白色石碑、失声风铃和结霜的记忆罐。"
                    "阿雾先前一直避开众人的目光。"
                ),
                "selected_scene_situation": "阿雾正试图把一个小布袋藏进木梁缝里",
            }
        )

        self.assertEqual(rendered, candidate)
        self.assertEqual(len(client.calls), 2)
        audit_prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[1]["messages"]
        )
        self.assertIn("阿雾正试图把一个小布袋藏进木梁缝里", audit_prompt)
        self.assertIn('"planned_change_is_new": true', audit_prompt)

    def test_scene_transition_rewrites_when_the_planned_situation_is_already_public(self) -> None:
        client = SequenceFakeClient(
            [
                "阿雾仍把小布袋塞在木梁缝里，白穗仍按着她的手。",
                '{"adds_new_change":false,"honors_consequences":true,'
                '"fulfills_requested_change":true,"requested_change_already_public":true,'
                '"reason":"藏匿动作已经公开"}',
                "候车廊外忽然响起铅签落地声，岑铅俯身去捡时，阿雾把布袋递给了白穗。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"requested_change_already_public":false,'
                '"reason":"出现了新的交付动作"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站·旧候车廊",
                "recent_public_context": "阿雾已经把小布袋塞进木梁缝里，白穗按住了她的手。",
                "selected_scene_situation": "阿雾正试图把一个小布袋藏进木梁缝里",
            }
        )

        self.assertIn("把布袋递给了白穗", rendered)
        self.assertEqual(len(client.calls), 4)

    def test_scene_beat_rewrites_unrelated_change_until_requested_npc_move_happens(self) -> None:
        client = SequenceFakeClient(
            [
                "门内传来一声椅脚摩地声，风铃在梁下轻轻碰了一下。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":false,"reason":"没有提出担保条件"}',
                "会长推门走出来，把旧路钥匙按在桌上：‘可以开门，但你们要留下一件能让我追责的信物。’",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"reason":"会长提出了可执行的担保条件"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "会长仍未说明开放旧路的条件。",
            },
            instruction="会长提出具体担保条件。",
            beat=True,
        )

        self.assertIn("留下一件", rendered)
        self.assertNotIn("椅脚摩地", rendered)
        self.assertEqual(len(client.calls), 4)

    def test_scene_beat_rewrites_a_half_revealed_contrast_relation(self) -> None:
        client = SequenceFakeClient(
            [
                "铃舌转过来，露出被刮掉的名字：那不是旅人的名字。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"reason":"排除了旅人的名字"}',
                "铃舌再转半圈，残留的刻痕拼出伊莉雅熟悉的旧姓——被刮去的名字不属于旅人，"
                "而属于伊莉雅本应记得的那个人。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"reason":"否定旧归属并公开了新归属"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "众人已经发现铃舌内侧有一段被刮去的刻痕。",
            },
            instruction=(
                "只落实这一项尚未发生的变化："
                "被刮去的名字并非旅人的，而属于伊莉雅本应记得的人。"
            ),
            beat=True,
        )

        self.assertIn("属于伊莉雅", rendered)
        self.assertEqual(len(client.calls), 4)

    def test_scene_beat_quality_review_extracts_fiction_change_from_meta_instruction(self) -> None:
        instruction = (
            "桌面自然停顿后，判断是否需要由NPC、环境或对立方推进。"
            "若需要发言，只落实这一项尚未发生的变化：会长提出具体担保条件。"
            "已说过的内容不得换词重说；不替英雄决定。"
        )

        self.assertEqual(
            LLMExpressor._judgeable_requested_change(instruction),
            "会长提出具体担保条件",
        )

    def test_scene_beat_quality_review_drops_meta_only_instruction(self) -> None:
        instruction = "桌面自然停顿后，判断是否需要发言；不要复述，不替英雄决定。"

        self.assertEqual(LLMExpressor._judgeable_requested_change(instruction), "")

    def test_structured_scene_beat_reuses_quality_and_state_from_single_model_call(self) -> None:
        reply = "会长收起名册，把旧路门闩推到底：‘路开了，你们现在就走。’"
        client = FakeClient(
            json.dumps(
                {
                    "reply": reply,
                    "npc_conditions": [],
                    "state_change": {
                        "material_change": True,
                        "public_fact": "旧路门闩已经打开。",
                        "opposition_move": "",
                        "reveal": "",
                        "reversal": False,
                        "local_question_changed": True,
                        "local_question_resolved": True,
                        "signature_image_evolved": True,
                        "commitment_level": "consequence",
                        "irreversible_change": True,
                    },
                    "quality": {
                        "adds_new_change": True,
                        "honors_consequences": True,
                        "fulfills_requested_change": True,
                        "requested_change_already_public": False,
                    },
                },
                ensure_ascii=False,
            )
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "会长仍按着旧路门闩，没有答复。",
            },
            instruction="【高潮提交】让旧路是否开放得到明确答案。",
            beat=True,
        )

        self.assertEqual(rendered, reply)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(expressor.last_scene_moment_metadata["state_change"]["material_change"])
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})

    def test_scene_beat_rewrites_an_anonymous_speaking_npc_as_a_stable_role(self) -> None:
        vague_reply = "白花守望会的人从门后探出脸：‘旧路能借，但先别碰风铃。’"
        stable_reply = "白花守门人从门后探出脸：‘旧路能借，但先别碰风铃。’"
        client = SequenceFakeClient(
            [
                json.dumps(
                    {
                        "reply": vague_reply,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "白花守望会的人",
                                "speaker_evidence": vague_reply,
                                "public_statement": "旧路能借，但先别碰风铃。",
                            }
                        ],
                        "state_change": {"material_change": False, "commitment_level": "atmosphere"},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "reply": stable_reply,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "白花守门人",
                                "speaker_evidence": stable_reply,
                                "public_statement": "旧路能借，但先别碰风铃。",
                            }
                        ],
                        "state_change": {"material_change": False, "commitment_level": "atmosphere"},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {"location": "白花碑驿站"},
            instruction="让一名在场NPC表明旧路是否能借。",
            beat=True,
        )

        self.assertEqual(rendered, stable_reply)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(expressor.last_scene_moment_metadata["npc_speakers"][0]["npc"], "白花守门人")

    def test_scene_opening_rewrites_dialogue_borrowed_from_another_npc(self) -> None:
        wrong_reply = (
            "灰金短斗篷使者停在门外，说：“条件我已经说清了，要谈，"
            "就别让他再站回原来的入口。”"
        )
        corrected_reply = (
            "灰金短斗篷使者停在门外，说：“今天只认你们知道的那条去路，"
            "不换别的。”"
        )
        client = SequenceFakeClient(
            [
                json.dumps(
                    {
                        "reply": wrong_reply,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "灰金短斗篷使者",
                                "speaker_evidence": wrong_reply,
                                "public_statement": "条件我已经说清了，要谈，就别让他再站回原来的入口。",
                            }
                        ],
                        "state_change": {"material_change": False},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "reply": corrected_reply,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "灰金短斗篷使者",
                                "speaker_evidence": corrected_reply,
                                "public_statement": "今天只认你们知道的那条去路，不换别的。",
                            }
                        ],
                        "state_change": {"material_change": False},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "npc_statement_ledger": [
                    {
                        "npc": "失名旅人",
                        "statements": [
                            "真正能顶住门外使者的，只是路还在、但人不能再站在原来的入口上这一句。"
                        ],
                    },
                    {
                        "npc": "灰金短斗篷使者",
                        "statements": ["今天只认一项：你们知道的那条去路。"],
                    },
                ],
            },
            instruction="让门外使者回应已经公开的局面。",
            beat=False,
        )

        self.assertEqual(rendered, corrected_reply)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("台词归属冲突", expressor.last_scene_candidate_diagnostics[0]["boundary_violation"])

    def test_scene_beat_fallback_never_recites_or_invents_from_planning_fields(self) -> None:
        rendered = Expressor().render_scene_moment(
            {
                "location": "白花碑驿站",
                "current_pressure": "旧路仍未开放。",
                "npc_functions": ["守门者：白花守望会，能提供旧路、规矩或代价。"],
            },
            instruction="让驿站守门人作出新的反应。",
            beat=True,
        )

        self.assertEqual(rendered, "")

    def test_strict_optional_scene_beat_stays_silent_after_two_unusable_candidates(self) -> None:
        client = SequenceFakeClient(["钟鸣公国。", "仍是钟鸣公国。"])
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "钟鸣公国",
                "recent_public_context": "听证厅仍在等代理人回答。",
            },
            instruction="若有必要，让代理人作出一个具体决定。",
            beat=True,
        )

        self.assertEqual(rendered, "")
        self.assertEqual(len(client.calls), 2)

    def test_scene_beat_rewrites_a_threat_consequence_before_clock_is_full(self) -> None:
        client = SequenceFakeClient(
            [
                "几道人影停在檐下，白花婆婆抬头说：‘他们到了。’",
                "林口的第三盏车灯亮了起来，远处车轮声压过风铃一瞬；白花婆婆把名册折进抽屉。",
                '{"adds_new_change":true,"honors_consequences":true,'
                '"fulfills_requested_change":true,"reason":"远处征兆增强且NPC采取了行动"}',
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "recent_public_context": "远处已经能听见财团车轮声。",
                "active_clocks": ["【财团巡逻队逼近】1/8"],
                "clock_boundaries": [
                    {
                        "name": "财团巡逻队逼近",
                        "current": 1,
                        "maximum": 8,
                        "stakes": "填满后巡逻队包围驿站。",
                    }
                ],
            },
            instruction="让现场出现一个新的可回应变化，但不得替玩家行动。",
            beat=True,
        )

        self.assertIn("第三盏车灯", rendered)
        self.assertNotIn("他们到了", rendered)
        self.assertEqual(len(client.calls), 3)

    def test_scene_beat_rewrites_when_an_npc_skips_a_public_retreat_promise(self) -> None:
        broken = "财团使者收起验片，说：‘验到这里已经够了。’她没有退开，反而敲了敲门板。"
        honored = "财团使者收起验片，说：‘验到这里已经够了。’她随即向后退开两步，离开登记小室的门边。"
        client = SequenceFakeClient(
            [
                json.dumps(
                    {
                        "reply": broken,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "监察官艾蕾娜",
                                "speaker_evidence": "验到这里已经够了",
                                "public_statement": "验到这里已经够了。她没有退开，反而敲了敲门板。",
                            }
                        ],
                        "state_change": {"material_change": True},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "reply": honored,
                        "npc_conditions": [],
                        "npc_speakers": [
                            {
                                "npc": "监察官艾蕾娜",
                                "speaker_evidence": "验到这里已经够了",
                                "public_statement": "验到这里已经够了。她向后退开两步。",
                            }
                        ],
                        "state_change": {"material_change": True},
                        "quality": {"adds_new_change": True, "honors_consequences": True},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "npc_due_commitments": [
                    {
                        "npc": "财团使者",
                        "aliases": "财团使者|门外的使者",
                        "trigger": "核验完成",
                        "required_effect": "退开",
                    }
                ],
            },
            instruction="让财团使者结束本次核验。",
            beat=True,
        )

        self.assertEqual(rendered, honored)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("承诺核验完成后退开", expressor.last_scene_candidate_diagnostics[0]["boundary_violation"])

    def test_scene_beat_stays_silent_when_rewrite_still_crosses_clock_boundary(self) -> None:
        client = SequenceFakeClient(
            [
                "巡逻队已经到了门外。",
                "车队终于抵达，领头人站在檐下。",
            ]
        )
        expressor = LLMExpressor(client=client, model="fake-model", allow_fallback=False)

        rendered = expressor.render_scene_moment(
            {
                "location": "白花碑驿站",
                "clock_boundaries": [
                    {
                        "name": "财团巡逻队逼近",
                        "current": 1,
                        "maximum": 8,
                        "stakes": "填满后巡逻队包围驿站。",
                    }
                ],
            },
            instruction="让局势向前一拍。",
            beat=True,
        )

        self.assertEqual(rendered, "")
        self.assertEqual(len(client.calls), 2)

    def test_static_render_drops_pre_roll_in_mind_reply(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.REQUEST_ROLL, parameters={"in_mind_reply": "先把话说稳，盾比剑更适合开门。"}),
            rules_text="伊莉雅尝试说服守望会。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["MIG", "WLP"],
                    dice=[(10, 3), (6, 1)],
                    total=4,
                    modifier=0,
                    high_roll=3,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="白花守望会",
                    reason="推进信任",
                )
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("失败", rendered)
        self.assertNotIn("先把话说稳", rendered)
        self.assertNotIn("盾比剑", rendered)

    def test_request_roll_with_hp_after_is_rendered_as_damage_resolution(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.REQUEST_ROLL, parameters={}),
            rules_text="洛岚攻击。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["DEX", "MIG"],
                    dice=[(8, 5), (8, 4)],
                    total=9,
                    modifier=0,
                    high_roll=5,
                    target_number=8,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="财团机兵",
                    damage=11,
                    damage_type="physical",
                    hp_after=29,
                )
            },
        )

        rendered = Expressor().render(resolution)

        self.assertNotIn("【战斗结算】", rendered)
        self.assertIn("洛岚 对 财团机兵 的检定", rendered)
        self.assertIn("造成 11 点", rendered)

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

    def test_llm_expressor_drops_narration_that_invents_an_injury_for_zero_healing(self) -> None:
        client = FakeClient(
            "伊莉雅的脸色没有立刻回暖，魂光没能把她从原本的状态里拉出来。"
        )
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.SPELL, parameters={}),
            rules_text="伊莉雅受到【治愈术】影响，规则恢复量40点HP；HP 60->60，实际恢复0点。",
            payload={
                "resource_change": ResourceChange("赛璃", "mp", -10, 50, 40, "施放【治愈术】消耗MP。"),
                "spell_name": "治愈术",
                "healing_change": ResourceChange("伊莉雅", "hp", 0, 60, 60, "【治愈术】恢复生命值。"),
                "spell_fixed_effect": {
                    "kind": "heal",
                    "base_amount": 40,
                    "actual_amount": 0,
                    "targets": ["伊莉雅"],
                },
            },
        )

        rendered = expressor.render(resolution)
        prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in client.calls[0]["messages"]
        )

        self.assertIn("实际恢复 0", rendered)
        self.assertNotIn("脸色没有立刻回暖", rendered)
        self.assertIn("不得暗示其原本受伤", prompt)

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

    def test_rich_investigation_panel_is_not_semantically_repeated_by_second_prose_pass(self) -> None:
        client = FakeClient("盐痕再次证明门外的东西还在附近，阴影也仍然没有散去。")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.INVESTIGATE, parameters={}),
            rules_text="调查检定 14：成功。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["INS", "INS"],
                    dice=[(8, 7), (8, 7)],
                    total=14,
                    modifier=0,
                    high_roll=7,
                    target_number=7,
                    success=True,
                    critical_success=True,
                    fumble=False,
                    target="门口与柜台周边",
                    reason="观察",
                ),
                "scene_object": "门口与柜台周边",
                "information": [
                    "柜台到门口之间没有新脚印。",
                    "大成功线索：拖痕转向侧路。",
                ],
            },
        )

        rendered = expressor.render(resolution)

        self.assertIn("柜台到门口之间没有新脚印", rendered)
        self.assertIn("拖痕转向侧路", rendered)
        self.assertNotIn("盐痕再次证明", rendered)
        self.assertEqual(client.calls, [])

    def test_investigation_answers_the_question_before_bonus_clue_and_opportunity(self) -> None:
        primary = "贴潮沟的窄路脚印更少且石脊连续，比海岸侧旧车辙更适合避开巡逻；右侧浅水边的三块凸石可安全落脚。"
        bonus = "海岸侧车辙缝里嵌着与财团巡逻印记相同的蓝灰晶粉。"
        resolution = ActionResolution(
            action=Action(
                ActionType.INVESTIGATE,
                parameters={"scene_investigation_label": "比较两条路线"},
            ),
            rules_text="调查检定 14：成功。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "DEX"],
                    dice=[(10, 7), (8, 7)],
                    total=14,
                    modifier=0,
                    high_roll=7,
                    target_number=8,
                    success=True,
                    critical_success=True,
                    fumble=False,
                    opportunity_count=1,
                    target="潮沟入口的两条路线",
                    reason="比较安全路线",
                ),
                "information": [primary, bonus],
                "scene_object": "潮沟入口的两条路线",
            },
        )

        rendered = Expressor().render(resolution)

        self.assertLess(rendered.index(primary), rendered.index(bonus))
        self.assertLess(rendered.index(bonus), rendered.index("你获得 1 次机会"))

    def test_llm_expressor_receives_structured_speech_intent(self) -> None:
        client = FakeClient("守望者把钥匙压回掌心，没有让出门路。")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.INVESTIGATE, parameters={}),
            rules_text="伊莉雅调查失败。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["INS", "INS"],
                    dice=[(8, 2), (8, 3)],
                    total=5,
                    modifier=0,
                    high_roll=3,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="守望会旧路",
                    reason="调查",
                ),
                "speech_intent": {
                    "act": "investigation_resolution",
                    "tone": "具体、克制；让阻力或代价在剧情中发生",
                    "max_sentences": 2,
                    "avoid": ["替玩家角色行动"],
                }
            },
        )

        expressor.render(resolution)
        prompt_message = client.calls[0]["messages"][-1]
        prompt_content = prompt_message.content if hasattr(prompt_message, "content") else prompt_message["content"]

        self.assertIn("【表达意图】", prompt_content)
        self.assertIn("investigation_resolution", prompt_content)
        self.assertIn("替玩家角色行动", prompt_content)

    def test_llm_expressor_does_not_send_pre_roll_in_mind_reply_to_expression_model(self) -> None:
        stale_line = "先把话说稳，盾比剑更适合开门。"
        client = FakeClient("守望者的沉默把门缝重新压紧。")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.REQUEST_ROLL, parameters={"in_mind_reply": stale_line}),
            rules_text="伊莉雅尝试说服守望会。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["MIG", "WLP"],
                    dice=[(10, 3), (6, 1)],
                    total=4,
                    modifier=0,
                    high_roll=3,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="白花守望会",
                    reason="推进信任",
                )
            },
        )

        rendered = expressor.render(resolution)
        prompt_message = client.calls[0]["messages"][-1]
        prompt_content = prompt_message.content if hasattr(prompt_message, "content") else prompt_message["content"]

        self.assertIn("守望者的沉默把门缝重新压紧", rendered)
        self.assertNotIn(stale_line, prompt_content)
        self.assertNotIn(stale_line, rendered)

    def test_llm_expressor_drops_writer_room_nail_metaphor(self) -> None:
        client = FakeClient("这份提议本身也会像一粒冰冷的钉子，牢牢钉住他们必须作出的选择。\n风铃仍在旧路尽头轻响。")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "守望会仍在迟疑。"}),
            rules_text="守望会仍在迟疑。",
            payload={},
        )

        rendered = expressor.render(resolution)

        self.assertNotIn("钉子", rendered)
        self.assertIn("风铃仍在旧路尽头轻响", rendered)

    def test_llm_expressor_drops_literal_empty_string_placeholder(self) -> None:
        client = FakeClient("空字符串")
        expressor = LLMExpressor(client=client, model="fake-model")
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, {"summary": "潮生藤给出了方向。"}),
            rules_text="潮生藤给出了方向。",
            payload={},
        )

        rendered = expressor.render(resolution)

        self.assertEqual(rendered, "潮生藤给出了方向。")
        self.assertNotIn("空字符串", rendered)

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

    def test_clock_multi_segment_progress_explains_margin(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.CONTRIBUTE_RITUAL, parameters={}),
            rules_text="洛岚推进仪式。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "DEX"],
                    dice=[(10, 8), (8, 7)],
                    total=15,
                    modifier=0,
                    high_roll=8,
                    target_number=7,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="仪式：风铃回声",
                    reason="推进仪式",
                    margin=8,
                ),
                "clock_change": ClockChange(
                    clock_name="仪式：风铃回声",
                    before=0,
                    after=3,
                    delta=3,
                    max_segments=4,
                    reason="高出难度等级6点以上。",
                ),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("结算值高出难度等级 8 点", rendered)
        self.assertIn("填充 3 格", rendered)

    def test_npc_failed_objective_gets_intent_and_failure_narrative(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.NPCACT,
                parameters={
                    "npc_action_type": "Objective",
                    "clock_name": "灰飞烟灭",
                    "target": "灰飞烟灭",
                },
            ),
            rules_text="监察官艾蕾娜试图推进命刻。",
            payload={
                "roll": RollOutcome(
                    actor="监察官艾蕾娜",
                    attributes=["INS", "WLP"],
                    dice=[(8, 2), (10, 3)],
                    total=5,
                    modifier=0,
                    high_roll=3,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="灰飞烟灭",
                    reason="推进命刻",
                    margin=-5,
                ),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("监察官艾蕾娜试图加速命刻【灰飞烟灭】", rendered)
        self.assertIn("失败", rendered)
        self.assertIn("没有造成额外推进", rendered)

    def test_clock_completion_turns_stakes_into_the_immediate_consequence(self) -> None:
        change = ClockChange(
            "财团巡逻队逼近",
            before=5,
            after=6,
            delta=1,
            max_segments=6,
            reason="自动推进",
            clock_type="threat",
            stakes="填满后财团巡逻队包围现场。",
        )

        text = Expressor()._clock_change_text(change)

        self.assertIn("【财团巡逻队逼近】6/6", text)
        self.assertIn("财团巡逻队包围现场", text)
        self.assertNotIn("填满后", text)
        self.assertNotIn("赌注", text)

    def test_ritual_clock_completion_says_ritual_ready(self) -> None:
        change = ClockChange(
            "仪式：风铃回声",
            before=3,
            after=4,
            delta=1,
            max_segments=4,
            reason="推进仪式命刻",
            clock_type="ritual",
        )

        text = Expressor()._clock_change_text(change)

        self.assertIn("仪式准备完成", text)
        self.assertNotIn("目标达成", text)

    def test_npc_objective_direction_says_block_when_erasing_player_clock(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.NPCACT,
                parameters={
                    "npc_action_type": "Objective",
                    "clock_name": "[仪式：风铃回声]",
                    "target": "[仪式：风铃回声]",
                    "clock_direction": -1,
                },
            ),
            rules_text="财团狙击手试图破坏仪式命刻。",
            payload={
                "roll": RollOutcome(
                    actor="财团狙击手",
                    attributes=["INS", "WLP"],
                    dice=[(8, 7), (6, 6)],
                    total=13,
                    modifier=0,
                    high_roll=7,
                    target_number=7,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="仪式：风铃回声",
                    reason="破坏命刻",
                    margin=6,
                ),
                "clock_change": ClockChange("仪式：风铃回声", 2, 0, -3, 4),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("财团狙击手试图阻止命刻【仪式：风铃回声】", rendered)
        self.assertIn("擦除 3 格", rendered)

    def test_investigation_renders_established_threat_clock(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.INVESTIGATE,
                parameters={
                    "establish_threat_clock_stakes": "填满后财团巡逻队包围现场。",
                },
            ),
            rules_text="洛岚调查车辙。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "INS"],
                    dice=[(10, 1), (10, 5)],
                    total=6,
                    modifier=0,
                    high_roll=5,
                    target_number=7,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="财团车辙",
                    reason="调查迫近威胁",
                    margin=-1,
                ),
                "information": [],
                "clock_change": ClockChange(
                    clock_name="财团巡逻队逼近",
                    before=0,
                    after=0,
                    delta=0,
                    max_segments=6,
                    reason="GM 判断线索显示威胁正在逼近。",
                ),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("【财团巡逻队逼近】0/6", rendered)
        self.assertNotIn("公开威胁命刻", rendered)
        self.assertNotIn("赌注：", rendered)
        self.assertNotIn("财团巡逻队包围现场", rendered)
        self.assertNotIn("失败触发威胁进展", rendered)

    def test_environment_investigation_does_not_render_as_targeted_object_check(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.INVESTIGATE,
                parameters={
                    "scene_investigation_scope": "environment",
                    "scene_investigation_label": "巡夜观察周边环境",
                },
            ),
            rules_text="伊莉雅巡夜观察。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["INS", "INS"],
                    dice=[(8, 5), (8, 3)],
                    total=8,
                    modifier=0,
                    high_roll=5,
                    target_number=7,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="周边环境",
                    reason="巡夜观察",
                    margin=1,
                ),
                "information": ["巡夜观察确认了一条周边线索：远处火光不是偶然渔灯。"],
                "scene_object": "周边环境",
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("伊莉雅巡夜观察周边环境", rendered)
        self.assertNotIn("对 追兵火光 的检定", rendered)
        self.assertNotIn("对 周边环境 的检定", rendered)

    def test_investigation_reveals_failure_consequence_only_after_failed_roll(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.INVESTIGATE,
                parameters={
                    "failure_consequence": "巡守会察觉你在试探他们的戒备",
                },
            ),
            rules_text="伊莉雅观察门口巡守。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["INS", "WLP"],
                    dice=[(8, 4), (8, 3)],
                    total=7,
                    modifier=0,
                    high_roll=4,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                    target="门口巡守",
                    reason="观察戒备",
                    margin=-3,
                ),
                "information": [],
            },
        )

        rendered = Expressor().render(resolution)

        roll_at = rendered.index("掷骰")
        consequence_at = rendered.index("巡守会察觉")
        self.assertLess(roll_at, consequence_at)
        self.assertIn("【洞察+意志】", rendered)
        self.assertIn("难度等级 10", rendered)
        self.assertIn("巡守会察觉", rendered)
        self.assertNotIn("若失败", rendered)


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

        self.assertNotIn("【仪式等待】", rendered)
        self.assertIn("还差 1 格", rendered)
        self.assertNotIn("KeyError", rendered)

    def test_public_render_humanizes_internal_terms(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.NARRATE, parameters={"summary": "当前公开硬状态显示【旧路闸门开启】仍为 0/6；保持冲突继续。事后用 SellItem 处理。"}),
            rules_text="",
            payload={},
        )

        rendered = Expressor().render(resolution)

        self.assertIn("现在已经确认", rendered)
        self.assertIn("冲突还在继续", rendered)
        self.assertIn("出售物品", rendered)
        self.assertNotIn("硬状态", rendered)
        self.assertNotIn("SellItem", rendered)

        gm_leak = Expressor().render(
            ActionResolution(
                action=Action(ActionType.NARRATE, parameters={"summary": "GM应接住撤离意图，不做硬成本结算。"}),
                rules_text="",
                payload={},
            )
        )

        self.assertIn("先接住", gm_leak)
        self.assertNotIn("GM应", gm_leak)
        self.assertNotIn("硬成本", gm_leak)

    def test_replayed_investigation_renders_only_final_committed_result(self) -> None:
        source_action = Action(
            ActionType.INVESTIGATE,
            parameters={
                "actor": "洛岚",
                "scene_investigation_scope": "environment",
                "scene_investigation_label": "检查旧钟齿槽",
            },
        )
        resolution = ActionResolution(
            action=Action(ActionType.INVOKE_TRAIT, parameters={"actor": "洛岚"}),
            rules_text="旧结果已回滚并重新提交。 调查检定 13：成功。获取了场景线索。",
            payload={
                "check_transaction_replayed": True,
                "check_transaction_invocation_text": "洛岚援用身份【研究魔导技术的钟匠】重掷。",
                "committed_source_action": source_action,
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "INS"],
                    dice=[(10, 8), (10, 5)],
                    total=13,
                    modifier=0,
                    high_roll=8,
                    target_number=10,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="旧钟齿槽",
                    reason="检查复制钥匙留下的灰晶粉",
                    margin=3,
                ),
                "information": ["齿槽里的灰晶粉只会附着在财团制式复制钥匙上。"],
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("援用身份", rendered)
        self.assertIn("灰晶粉", rendered)
        self.assertIn("结算值 13", rendered)
        self.assertNotIn("旧结果已回滚", rendered)
        self.assertNotIn("调查检定 13", rendered)
        self.assertNotIn("获取了场景线索", rendered)

    def test_planned_object_check_uses_natural_label_and_reveals_success_fact(self) -> None:
        resolution = ActionResolution(
            action=Action(
                ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "洛岚",
                    "target": "旧钟齿槽里的灰晶粉",
                    "scene_check_planned": True,
                    "scene_investigation_scope": "object",
                    "scene_investigation_label": "检查旧钟齿槽里的灰晶粉",
                    "success_answer": "灰晶粉只附着在财团制式复制钥匙留下的擦痕上",
                },
            ),
            rules_text="",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "INS"],
                    dice=[(10, 8), (10, 5)],
                    total=13,
                    modifier=0,
                    high_roll=8,
                    target_number=10,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="旧钟齿槽里的灰晶粉",
                    reason="检查复制钥匙留下的痕迹",
                    margin=3,
                )
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("洛岚进行检查旧钟齿槽里的灰晶粉检定", rendered)
        self.assertIn("灰晶粉只附着在财团制式复制钥匙", rendered)
        self.assertNotIn("对 旧钟齿槽里的灰晶粉 的检定", rendered)


if __name__ == "__main__":
    unittest.main()
