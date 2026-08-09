import json
import tempfile
import unittest
from pathlib import Path

from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.player_simulator import (
    PLAYER_ACTION_PROGRESS_REVIEW_PROMPT,
    ConstrainedPlayerSimulator,
)
from fu_gm.testing.replay_models import LegalActionContext, ReplayScenario, ReplayStep
from fu_gm.testing.replay_runner import HumanLikeReplayRunner
from fu_gm.testing.rule_glossary import FINAL_FABULA_GLOSSARY


class HumanLikeReplayFrameworkTests(unittest.TestCase):
    def test_adventure_action_uses_character_name_not_table_player_name(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="player-character-boundary",
            kind="game_turn",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["艾薇娅", "苍祈"],
        )

        invalid = simulator.validate(
            "苍祈站到时雨身旁，确认核验期间时雨是否受保护。",
            step=step,
            legal_context=context,
        )
        valid = simulator.validate(
            "艾薇娅站到苍祈身旁，确认核验期间艾薇娅是否受保护。",
            step=step,
            legal_context=context,
        )

        self.assertIn("player_name_used_as_fictional_character", invalid)
        self.assertNotIn("player_name_used_as_fictional_character", valid)

    def test_adventure_action_slot_rejects_session_zero_meta_discussion(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="stay-in-scene",
            kind="player_message",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="只根据上一条GM公开内容回应当前场景。这是行动槽：必须提交明确行动。",
        )

        errors = simulator.validate(
            "苍祈把话接住，先不急着往前推剧情。我想这次偏严肃正剧一点，但也别太压抑。",
            step=step,
            legal_context=LegalActionContext(stage_goal=step.stage_goal, known_pcs=["苍祈"]),
            recent_public_context="时悠：艾蕾娜举着临检令，等你们回答。",
        )

        self.assertIn("action_slot_leaves_adventure_for_setup_discussion", errors)

    def test_optional_reroll_after_success_is_not_a_required_player_response(self) -> None:
        self.assertFalse(
            LegalActionLayer._requires_player_response(
                {"kind": "trait_invocation", "blocking": False}
            )
        )
        self.assertTrue(
            LegalActionLayer._requires_player_response(
                {"kind": "critical_opportunity", "blocking": True}
            )
        )

    def test_distinct_decision_window_may_require_the_same_standard_reply(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="second-trait-window",
            kind="decision_window",
            speaker="白河",
            actor="洛岚",
            stage_goal="回应当前属于洛岚的待决窗口。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            pending_decisions=[
                {
                    "window_id": "trait-window-2",
                    "kind": "trait_invocation",
                    "blocking": True,
                    "options": [{"trait": "敏锐"}],
                }
            ],
        )
        text = "我花 1 点物语点，援用【敏锐】重掷两枚骰。"

        errors = simulator.validate(
            text,
            step=step,
            legal_context=context,
            recent_public_context=(
                "阿凛：我花 1 点物语点，援用【敏锐】重掷两枚骰。\n"
                "时悠：上一项检定已经完成；现在是新的检定窗口。"
            ),
        )

        self.assertNotIn("near_duplicate_recent_player_utterance", errors)
        self.assertNotIn("does_not_answer_pending_decision", errors)

    def test_critical_opportunity_fallback_supplies_required_target(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        window = {
            "window_id": "critical-1",
            "kind": "critical_opportunity",
            "owner": "洛岚",
            "blocking": True,
            "options": [
                {
                    "effect": "优势",
                    "requires": ["target"],
                }
            ],
        }
        context = LegalActionContext(
            stage_goal="回答大成功机会窗口。",
            known_pcs=["洛岚", "伊莉雅"],
            pending_decisions=[window],
        )

        fallback = simulator._decision_window_fallback(window, context)
        errors = simulator.validate(
            fallback,
            step=ReplayStep(
                id="critical-target",
                kind="decision_window",
                speaker="白河",
                actor="洛岚",
                stage_goal="回答大成功机会窗口。",
            ),
            legal_context=context,
            recent_public_context="时悠：这次大成功带来一个机会。",
        )

        self.assertIn("【优势】", fallback)
        self.assertIn("【洛岚】", fallback)
        self.assertNotIn("does_not_answer_pending_decision", errors)

    def test_trust_fallback_supplies_choice_trait_and_target_together(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        window = {
            "window_id": "trust-1",
            "kind": "skill_parameter",
            "owner": "艾薇娅",
            "label": "予以信任",
            "blocking": True,
            "options": [
                {
                    "choice": "assist_trait",
                    "trait": "辉钢财团出逃的魔导工匠",
                    "target": "洛岚",
                },
                {"choice": "decline"},
            ],
        }
        context = LegalActionContext(
            stage_goal="回答予以信任窗口。",
            known_pcs=["艾薇娅", "洛岚"],
            pending_decisions=[window],
        )

        fallback = simulator._decision_window_fallback(window, context)
        errors = simulator.validate(
            fallback,
            step=ReplayStep(
                id="trust-choice",
                kind="decision_window",
                speaker="时雨",
                actor="艾薇娅",
                stage_goal="回答予以信任窗口。",
            ),
            legal_context=context,
            recent_public_context="时悠：艾薇娅要发动予以信任吗？",
        )

        self.assertIn("【予以信任】", fallback)
        self.assertIn("【洛岚】", fallback)
        self.assertIn("【辉钢财团出逃的魔导工匠】", fallback)
        self.assertNotIn("does_not_answer_pending_decision", errors)

    def test_player_cannot_cross_a_route_that_has_not_been_opened(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="closed-route",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        recent = (
            "时悠：白栎按住门闩，没有让开。‘给我一份带姓名的保证，我才会开门。’\n"
            "阿凛：伊莉雅还没有递出保证。"
        )

        errors = simulator.validate(
            "南星：赛璃沿旧路走到转折处，查看前方是否安全。",
            step=step,
            legal_context=LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"]),
            recent_public_context=recent,
        )

        self.assertIn("crosses_unopened_route", errors)

    def test_player_can_inspect_an_unopened_route_from_the_threshold(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="inspect-threshold-without-crossing",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="风铃廊问路",
            scene_location="白花碑驿站·风铃廊",
            known_pcs=["赛璃"],
            known_npcs=["白花守望会会长"],
        )
        recent = (
            "时悠：一条路线要满足三点：眼前一段能当场查明没有新鲜的拦截或伏击迹象；"
            "失名旅人全程留在你们彼此可信的视线内；遇到异常时，你们能说明如何立即退回。"
            "做到这些，我会放行。"
        )
        action = (
            "赛璃让失名旅人留在同伴之间，停在尚未开启的旧路闸门内侧，"
            "查看旧路起段可见范围内的脚印、遮挡和异常动静；一旦发现异常，就立刻退回门内。"
        )

        errors = simulator.validate(
            action,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("leaves_current_scene_without_transition", errors)
        self.assertNotIn("crosses_unopened_route", errors)
        self.assertNotIn("ignores_explicit_gm_affordance", errors)

    def test_condition_fallback_performs_visible_route_safety_check(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="route-safety-condition-fallback",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="风铃廊问路",
            scene_location="白花碑驿站·风铃廊",
            known_pcs=["赛璃"],
            known_npcs=["白花守望会会长"],
            open_npc_conditions=[
                {
                    "npc": "白花守望会会长",
                    "condition": "当场查明旧路起段的即时风险，保持失名旅人在可信视线内，并说明遭遇异常时的退路与处置办法。",
                    "promised_result": "放行进入旧路。",
                }
            ],
        )
        recent = (
            "时悠：一条路线要满足三点：眼前一段能当场查明没有新鲜的拦截或伏击迹象；"
            "失名旅人全程留在你们彼此可信的视线内；遇到异常时，你们能说明如何立即退回。"
            "做到这些，我会放行。"
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "南星：赛璃现在当场查明旧路起段的即时风险。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
            last_gm_reply=recent,
        )

        self.assertEqual(errors, [])
        self.assertIn("闸门内侧", fallback)
        self.assertIn("脚印、遮挡和异常动静", fallback)
        self.assertIn("失名旅人", fallback)
        self.assertIn("退回门内", fallback)

    def test_structured_scene_gate_blocks_crossing_without_repeating_closed_door_in_chat(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="structured-closed-route",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            blocked_routes=["旧路"],
        )

        errors = simulator.validate(
            "南星：赛璃沿旧路走到转折处，查看前方是否安全。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：白花风铃停止了，柜台旁的封蜡仍有余温。",
        )

        self.assertIn("crosses_unopened_route", errors)

    def test_player_repair_instruction_explains_saturated_npc_lane_in_plain_language(self) -> None:
        instructions = ConstrainedPlayerSimulator._repair_instructions(
            ["repeats_saturated_npc_question_lane", "near_duplicate_recent_player_utterance"],
            LegalActionContext(stage_goal="测试修复提示"),
        )

        rendered = " ".join(instructions)
        self.assertIn("不要向任何NPC提问", rendered)
        self.assertIn("换一个对象、手段和目的", rendered)
        self.assertNotIn("repeats_saturated", rendered)

    def test_player_repair_instruction_points_to_the_open_npc_condition(self) -> None:
        instructions = ConstrainedPlayerSimulator._repair_instructions(
            ["repeats_saturated_npc_question_lane"],
            LegalActionContext(
                stage_goal="测试修复提示",
                open_npc_conditions=[
                    {
                        "npc": "梅鸥会长",
                        "condition": "当众承诺由守望会保管碎月遗物到日落前",
                        "promised_result": "打开旧路外闸",
                    }
                ],
            ),
        )

        rendered = " ".join(instructions)
        self.assertIn("不要再确认条件", rendered)
        self.assertIn("保管碎月遗物", rendered)

    def test_open_condition_fallback_chooses_an_immediately_executable_branch(self) -> None:
        action = ConstrainedPlayerSimulator._open_condition_action_fallback(
            "洛岚",
            [
                {
                    "npc": "梅鸥会长",
                    "condition": (
                        "要玩家交出一项能证明旅人身份或记忆异状的具体佐证，"
                        "或者当众承诺由守望会保管碎月遗物到日落前；"
                        "满足标准：收到公开宣读的保管承诺"
                    ),
                    "promised_result": "打开旧路外闸",
                }
            ],
        )

        self.assertIn("当众承诺", action)
        self.assertIn("碎月遗物", action)
        self.assertNotIn("一项能证明", action)
        self.assertNotIn("逐项落实", action)

    def test_open_condition_fallback_does_not_repeat_the_current_character_name(self) -> None:
        action = ConstrainedPlayerSimulator._open_condition_action_fallback(
            "赛璃",
            [
                {
                    "npc": "白花守望会会长",
                    "condition": "赛璃当场明确承担失名旅人的去向与护送责任。",
                    "promised_result": "临时开放旧路闸门。",
                }
            ],
            known_pcs=["伊莉雅", "赛璃", "洛岚"],
        )

        self.assertEqual(action, "赛璃现在明确承担失名旅人的去向与护送责任。")

    def test_open_condition_fallback_collects_a_ready_disclosure_promise(self) -> None:
        action = ConstrainedPlayerSimulator._open_condition_action_fallback(
            "赛璃",
            [
                {
                    "npc": "失名旅人",
                    "condition": "先把我带到门外看不见的位置。",
                    "promised_result": "我会当场说出还能说的那一小段去路。",
                }
            ],
            public_context=(
                "阿凛：伊莉雅把失名旅人带到屋内更深处。\n"
                "时悠：你已经把我带到更里侧了；现在我会说那一小段方向感。"
            ),
        )

        self.assertIn("转向失名旅人", action)
        self.assertIn("一小段去路", action)
        self.assertIn("现在请说出来", action)
        self.assertNotIn("带到门外看不见", action)

    def test_saturated_npc_lane_fallback_switches_to_a_concrete_world_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="saturated-lane-17",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal=(
                "这是行动槽：必须提交明确行动。"
                "本行动不得继续向NPC追问，必须操作现场物件或应对威胁。"
            ),
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            active_clocks=["【财团巡逻队逼近】3/6"],
            legal_actions=["调查", "普通叙事行动"],
        )

        utterance = simulator.compose(step=step, legal_context=context)

        self.assertTrue(utterance.used_fallback)
        self.assertNotRegex(utterance.text, r"[？?]|询问|追问|回答")
        self.assertIn("洛岚", utterance.text)

    def test_saturated_npc_lane_allows_explicitly_leaving_question_for_physical_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="leave-question-lane",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal=(
                "这是行动槽：必须提交明确行动。"
                "本行动不得继续向NPC追问，必须操作现场物件或应对威胁。"
            ),
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["白穗"],
            visible_scene_elements=["白穗脚边的小布袋"],
            legal_actions=["调查"],
        )

        errors = simulator.validate(
            "洛岚不再追问白穗，转身捻开她刚放下的小布袋，查看袋口的铅封痕。",
            step=step,
            legal_context=context,
        )

        self.assertNotIn("repeats_saturated_npc_question_lane", errors)

    def test_saturated_npc_lane_still_rejects_switching_to_another_npc_question(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="switch-question-target",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal=(
                "这是行动槽：必须提交明确行动。"
                "本行动不得继续向NPC追问，必须操作现场物件或应对威胁。"
            ),
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["洛岚"])

        errors = simulator.validate(
            "洛岚不再追问白穗，转向岑铅问：这只布袋是谁送来的？",
            step=step,
            legal_context=context,
        )

        self.assertIn("repeats_saturated_npc_question_lane", errors)

    def test_fallback_is_revalidated_instead_of_repeating_a_saturated_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="validated-fallback",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal=(
                "这是行动槽：必须提交明确行动。"
                "本行动不得继续向NPC追问，必须操作现场物件或应对威胁。"
            ),
        )
        legal = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            legal_actions=["调查", "普通叙事行动"],
        )
        recent = (
            "时悠：门外的巡逻队撞得门轴掉下灰粉，受伤的旅人靠在后墙。\n"
            "阿凛：伊莉雅把木柜推到门边挡住缺口。\n"
            "时悠：门板暂时稳住了。\n"
            "南星：赛璃又用粗麻布封住门缝。"
        )

        utterance = simulator.compose(
            step=step,
            legal_context=legal,
            recent_public_context=recent,
        )

        self.assertEqual(utterance.validation_errors, [])
        self.assertNotIn("当前压力", utterance.text)
        self.assertNotIn("已经公开的痕迹", utterance.text)
        self.assertNotIn("对方刚要求的那一步", utterance.text)

    def test_generic_fallback_uses_latest_public_scene_instead_of_current_target(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="contextual-fallback",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            legal_actions=["调查", "普通叙事行动"],
        )
        recent = (
            "时悠：旧闸门的门轴裂开一道口子，门外的财团脚步正在逼近。\n"
            "南星：赛璃把受伤的旅人扶到墙后。"
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("当前目标", utterance.text)
        self.assertNotIn("眼前装置", utterance.text)
        self.assertNotIn("伤势最明显的人", utterance.text)
        self.assertNotIn("视野内仍可通行的路线", utterance.text)
        self.assertRegex(utterance.text, r"入口|缺口|门|装置|痕迹|伤势|旅人|遮挡")

    def test_contextual_fallback_names_the_public_mechanism_it_uses(self) -> None:
        candidates = ConstrainedPlayerSimulator._contextual_action_candidates(
            "赛璃",
            "时悠：旧闸门的木栓从门槽里弹出半寸，门外巡逻队的铁靴声更近了。",
            ReplayStep(id="named-object", kind="game_turn", actor="赛璃"),
        )

        rendered = "\n".join(text for _, text in candidates)
        self.assertRegex(rendered, r"旧闸门|木栓")
        self.assertNotIn("眼前装置", rendered)
        self.assertNotIn("能移动的障碍物", rendered)
        self.assertNotIn("最容易被冲开的缺口", rendered)

    def test_contextual_fallback_does_not_walk_an_old_route_absent_from_latest_gm_reply(self) -> None:
        candidates = ConstrainedPlayerSimulator._contextual_action_candidates(
            "洛岚",
            (
                "时悠：驿站外的海岸路在雾里折向北边。\n"
                "南星：赛璃继续守在门边。\n"
                "时悠：门外脚步已经贴近，旧闸门仍没有打开。"
            ),
            ReplayStep(id="stale-route", kind="game_turn", actor="洛岚"),
        )

        rendered = "\n".join(text for _, text in candidates)
        self.assertNotIn("沿海岸走到转折处", rendered)

    def test_player_simulator_rejects_teleport_to_stale_location_outside_current_scene(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="no-stale-coast",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室里的白光",
            scene_location="白花碑驿站",
            known_pcs=["洛岚"],
            visible_scene_elements=["登记小室门口", "白花风铃", "碎月遗物"],
        )

        errors = simulator.validate(
            "洛岚沿海岸走到转折处，停下来查看这段路能否安全通过。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：白花风铃和碎月遗物已经并排放在登记小室桌边。",
        )

        self.assertIn("leaves_current_scene_without_transition", errors)

    def test_player_simulator_allows_movement_to_place_opened_in_latest_gm_reply(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="opened-coast",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="潮湿转角",
            scene_location="驿站外海岸路",
            known_pcs=["洛岚"],
            visible_scene_elements=["海岸转折处"],
        )

        errors = simulator.validate(
            "洛岚沿海岸走到转折处，停下来查看这段路能否安全通过。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：海岸路已经开放，转折处就在你眼前。",
        )

        self.assertNotIn("leaves_current_scene_without_transition", errors)

    def test_player_simulator_rejects_rechecking_an_established_evidence_relation(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="no-recheck",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室里的白光",
            scene_location="白花碑驿站",
            known_pcs=["洛岚"],
            established_scene_facts=[
                "白花风铃内侧与碎月遗物的浅痕确实相互对应，属于同一段被处理过的痕迹。"
            ],
        )

        errors = simulator.validate(
            "洛岚把白花风铃和碎月遗物并排比对，确认它们是不是同一段痕迹。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：两处浅痕已经确认相互对应。",
        )

        self.assertIn("rechecks_established_scene_fact", errors)

    def test_player_simulator_may_ask_a_new_aspect_about_established_evidence(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="new-aspect",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室里的白光",
            scene_location="白花碑驿站",
            known_pcs=["洛岚"],
            established_scene_facts=[
                "白花风铃内侧与碎月遗物的浅痕确实相互对应，属于同一段被处理过的痕迹。"
            ],
        )

        errors = simulator.validate(
            "洛岚检查两件东西残留的灰膜，追查这种处理最初来自谁。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：两处浅痕已经确认相互对应。",
        )

        self.assertNotIn("rechecks_established_scene_fact", errors)

    def test_player_simulator_rejects_same_action_repeated_by_another_hero(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        repeated = (
            "艾薇娅在岔路留下反向足迹并熄掉最显眼的路标，"
            "想把追踪者引离同伴撤退的方向；如果这需要检定，我接受 GM 指定合适属性。"
        )
        recent = (
            "南星：赛璃在岔路留下反向足迹并熄掉最显眼的路标，"
            "想把追踪者引离同伴撤退的方向；如果这需要检定，我接受 GM 指定合适属性。"
        )

        self.assertTrue(simulator._near_duplicate_player_utterance(repeated, recent))

    def test_player_simulator_rejects_immediate_paraphrase_of_complex_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        prior = (
            "我不再盯阿缇娅那边了，先把注意力转到岑烛的收据和柜台上，站近一点看清他递笔、收纸的动作，"
            "顺手挡在白栀和柜台之间，免得惊动阿缇娅。"
        )
        repeated = (
            "我先不碰阿缇娅那边，去盯岑烛的手势和柜台上的收据，站到能看清他递笔、收纸的位置，"
            "同时把自己挡在白栀和柜台中间，免得惊动阿缇娅。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, f"白河：{prior}"))

    def test_affordance_fallback_never_invents_former_and_latter_options(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        context = (
            "时悠：会长的明确答复是：黑色辉钢印记不具封路效力，"
            "它不是路权凭证，还是由你们决定之后的去向。"
        )

        fallback = simulator._affordance_response_fallback(
            "伊莉雅",
            context,
            known_npcs=["守望会巡守"],
        )

        self.assertEqual(fallback, "")
        self.assertNotIn("前一种", fallback)

    def test_player_simulator_rejects_second_hero_repeating_same_concealed_watch_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        prior = (
            "伊莉雅把注意力转到白碑后方那片尘雾上，往驿站门边退半步，"
            "先找一处能遮挡视线的位置，把失名旅人护在一起，同时留意号角是不是更近了。"
        )
        repeated = (
            "赛璃侧身到门边檐柱和半掩的木门框后面，招呼失名旅人往麻袋和木箱内侧靠拢，"
            "自己盯住白碑后的尘雾和南岸远路传来的号角声，先守住驿站门口。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, f"阿凛：{prior}"))

    def test_player_simulator_negated_opening_phrase_does_not_hide_repeated_action_family(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        prior = (
            "赛璃侧身到门边檐柱和木门框后面，借麻袋和木箱压低身形，"
            "盯住白碑后的尘雾与南岸号角，同时护住失名旅人。"
        )
        repeated = (
            "洛岚不再等别人先开口，他贴到檐柱和木门框内侧，借麻袋和木箱压低身形，"
            "抬头盯住白碑后的尘雾，把耳朵对准南岸号角，也护着失名旅人。"
        )

        self.assertEqual(simulator._action_family(repeated), "investigate")
        self.assertTrue(simulator._repeats_recent_action_lane(repeated, f"南星：{prior}"))

    def test_player_simulator_rejects_repeated_wind_chime_guard_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        prior = (
            "洛岚继续把那串白花风铃死死按在檐柱和木门框内侧，手掌压住铃身和门缝，"
            "先不让它再响；我就守在门边，把这处动静压稳。"
        )
        repeated = (
            "艾薇娅继续把门边那串白花风铃按在檐柱和木门框内侧，手掌压稳铃身和门缝，"
            "不让它再发出一点响动；我就守着这处门边，先把这份安静维持住。"
        )

        self.assertEqual(simulator._action_family(prior), "guard")
        self.assertEqual(simulator._action_family(repeated), "guard")
        self.assertTrue(simulator._repeats_recent_action_lane(repeated, f"白河：{prior}"))

    def test_player_simulator_rejects_reinforcing_the_same_signal_cylinder_brace(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        prior = "苍祈把碎石推拢到铜筒下方，垒出止滚矮坎，限制铜筒继续滑动。"
        repeated = "伊莉雅把碎石压紧在矮坎外侧，进一步稳住铜筒底部。"

        self.assertEqual(simulator._action_family(prior), "manipulate")
        self.assertEqual(simulator._action_family(repeated), "manipulate")
        self.assertTrue(simulator._repeats_recent_action_lane(repeated, f"澄砚：{prior}"))

    def test_player_simulator_rejects_conditional_backup_action_after_probe(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)

        assert simulator._conditional_future_action(
            "洛岚查看灰晶薄片；如果看不出来，我就把它收好，转而留意门外的号角。",
            actor="洛岚",
        )
        assert simulator._conditional_future_action(
            "洛岚立刻去看旧门；如果巡逻队真在逼近，我也想把会暴露位置的东西先收一收。",
            actor="洛岚",
        )

    def test_action_slot_rejects_repeated_disclosure_and_deferred_real_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="repeat-disclosure-then-act-later",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            known_npcs=["失名旅人", "灰金短斗篷的财团使者"],
            legal_actions=["交谈", "互动"],
        )
        recent = (
            "时悠：失名旅人说，往驿站南侧、顺着离风铃廊最远的旧通道走。\n"
            "时悠：财团使者说，他只接受这一小段方向，不接受别的交换。"
        )
        message = (
            "苍祈转向失名旅人，听你把那一小段再说一遍，"
            "然后我会把它原样拿去顶门外的财团使者。"
        )

        errors = simulator.validate(
            message,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_disclosed_information", errors)
        self.assertIn("action_slot_contains_deferred_future_action", errors)

        payout_then_future = simulator.validate(
            (
                "赛璃点头，我现在就听你那一小段方向感；说完以后，"
                "我会立刻拿这段话去回门外的财团使者。"
            ),
            step=step,
            legal_context=context,
            recent_public_context="时悠：我会在你听完后说出这一小段方向感。",
        )
        self.assertIn("action_slot_contains_deferred_future_action", payout_then_future)

        assumes_answer_then_addresses_second_npc = simulator.validate(
            (
                "赛璃等失名旅人说完那一小段方向感，便朝门外的灰金短斗篷使者开口："
                "‘这就是我们愿意让你听见的极限。’"
            ),
            step=step,
            legal_context=context,
            recent_public_context="时悠：现在我会说那一小段方向感。",
        )
        self.assertIn(
            "action_slot_contains_deferred_future_action",
            assumes_answer_then_addresses_second_npc,
        )

        single_continuous_action = simulator.validate(
            "赛璃走到门槛边，用靴底压住同一枚发亮的黄铜片。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：门槛上的黄铜片正亮起冷白纹路。",
        )
        self.assertNotIn("action_slot_contains_deferred_future_action", single_continuous_action)

    def test_action_slot_rejects_claim_that_open_npc_payout_already_happened(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="unfulfilled-route-payout",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        condition = {
            "condition_id": "traveler-hidden-place",
            "npc": "失名旅人",
            "condition": "先把我带到门外看不见的位置。",
            "promised_result": "我会当场说出还能说的那一小段去路。",
        }
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=["失名旅人", "灰金短斗篷的财团使者"],
            legal_actions=["交谈", "互动"],
            open_npc_conditions=[condition],
        )
        recent = "时悠：你已经把我带到更里侧了；现在我会说那一小段方向感。"
        false_payout = (
            "赛璃不再盯着失名旅人；既然他已经给出那一小段方向感，"
            "我现在直接把这段话转向门外的财团使者。"
        )

        errors = simulator.validate(
            false_payout,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("claims_unfulfilled_npc_payout", errors)
        willing_only = simulator.validate(
            "赛璃确认失名旅人已经愿意说出一小段方向感，现在请他当场说完。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        self.assertNotIn("claims_unfulfilled_npc_payout", willing_only)
        settled_context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=context.known_npcs,
            legal_actions=["交谈", "互动"],
        )
        settled = simulator.validate(
            false_payout,
            step=step,
            legal_context=settled_context,
            recent_public_context="时悠：往南岸旧堤，过三根断柱。",
        )
        self.assertNotIn("claims_unfulfilled_npc_payout", settled)

    def test_table_talk_cannot_treat_an_open_npc_access_condition_as_fulfilled(self) -> None:
        """A short role title must not bypass an NPC's still-open condition."""

        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="table-talk-open-access-condition",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="你正在和其他玩家短暂商量。只说意见，不替角色声明行动。",
        )
        condition = {
            "condition_id": "watch-chief-release",
            "npc": "白花守望会会长",
            "condition": "说清失忆旅人的去向和接手者。",
            "promised_result": "给出放行答复。",
        }
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=["白花守望会会长"],
            open_npc_conditions=[condition],
        )

        false_premise = simulator.validate(
            "南星: 我觉得先别散开，既然会长已经放行了，我们就先想想下一站。",
            step=step,
            legal_context=context,
            recent_public_context=(
                "时悠：会长说，先把确切去向和接手的人告诉我；我确认后，才给放行答复。"
            ),
        )
        honest_discussion = simulator.validate(
            "南星: 会长还没给放行答复。我们得先决定要不要把去向和接手的人告诉她。",
            step=step,
            legal_context=context,
            recent_public_context=(
                "时悠：会长说，先把确切去向和接手的人告诉我；我确认后，才给放行答复。"
            ),
        )

        self.assertIn("claims_unfulfilled_npc_payout", false_premise)
        self.assertNotIn("claims_unfulfilled_npc_payout", honest_discussion)

    def test_table_talk_fallback_acknowledges_an_open_npc_condition(self) -> None:
        utterance = ConstrainedPlayerSimulator._table_discussion_fallback(
            "南星",
            "时悠：门外的巡守把灯提起来，等你们说话。",
            open_conditions=[
                {
                    "npc": "白花守望会会长",
                    "condition": "说清失忆旅人的去向和接手者。",
                    "promised_result": "给出放行答复。",
                }
            ],
        )

        self.assertIn("还没兑现", utterance)
        self.assertNotIn("已经开", utterance)

    def test_player_cannot_turn_a_pending_npc_decision_into_a_result(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="pending-gatekeeper-decision",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["白花守望会会长"],
        )
        errors = simulator.validate(
            "伊莉雅对会长说：既然你已经当场作决定，那我们就照你的意思走旧路。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：我现在就决定是否让你们走旧路。",
        )
        waiting_is_legal = simulator.validate(
            "伊莉雅稳住旅人，等会长把允许或拒绝的话说清。",
            step=step,
            legal_context=context,
            recent_public_context="时悠：我现在就决定是否让你们走旧路。",
        )

        self.assertIn("claims_pending_npc_decision_result", errors)
        self.assertNotIn("claims_pending_npc_decision_result", waiting_is_legal)

    def test_action_slot_allows_collecting_an_open_npc_promise(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="collect-open-route-payout",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal=(
                "这是行动槽：必须提交当前角色的一个明确行动。"
                "本行动不得继续向NPC追问同一个话题。"
            ),
        )
        condition = {
            "condition_id": "traveler-hidden-place",
            "npc": "失名旅人",
            "condition": "先把我带到门外看不见的位置。",
            "promised_result": "我会当场说出还能说的那一小段去路。",
        }
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=["失名旅人"],
            legal_actions=["交谈", "互动"],
            open_npc_conditions=[condition],
        )
        recent = (
            "澄砚：苍祈把失名旅人带到屋内更深处，想听他说出方向。\n"
            "时悠：你把我带到更里侧后，我就说那一小段方向感。\n"
            "阿凛：伊莉雅现在直接听他把那一小段说完。\n"
            "时悠：你已经把我带到更里侧了；现在我会说那一小段方向感。"
        )
        request = "赛璃转向失名旅人：\u201c你答应的那一小段去路，现在请说出来。\u201d"

        errors = simulator.validate(
            request,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertTrue(
            simulator._requests_open_npc_payout(
                request,
                open_conditions=[condition],
            )
        )
        self.assertNotIn("repeats_disclosed_information", errors)
        self.assertNotIn("repeats_recent_action_lane", errors)
        self.assertNotIn("repeats_saturated_npc_question_lane", errors)
        self.assertNotIn("claims_unfulfilled_npc_payout", errors)

    def test_safe_spell_fallback_never_targets_a_narrative_only_npc(self) -> None:
        context = LegalActionContext(
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            known_pcs=["赛璃", "伊莉雅"],
            pc_resources={
                "赛璃": {"hp": 45, "max_hp": 45, "mp": 50, "max_mp": 50, "statuses": []},
                "伊莉雅": {"hp": 31, "max_hp": 60, "mp": 40, "max_mp": 40, "statuses": []},
            },
            known_npcs=["失名旅人"],
            legal_spells=["治愈术"],
        )

        candidates = ConstrainedPlayerSimulator._safe_spell_action_candidates(
            "赛璃",
            context,
            "失名旅人靠在墙边，伊莉雅守在门口。",
        )

        self.assertIn("赛璃对伊莉雅施放已掌握的【治愈术】。", candidates)
        self.assertTrue(all("失名旅人" not in item for item in candidates))

    def test_player_prompt_does_not_guess_unknown_character_gender(self) -> None:
        prompt = ConstrainedPlayerSimulator(use_llm=False)._system_prompt()

        self.assertIn("直接重复角色名", prompt)
        self.assertIn("不要擅自用‘他’或‘她’", prompt)

    def test_safe_spell_fallback_omits_healing_when_every_pc_is_at_full_hp(self) -> None:
        context = LegalActionContext(
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            known_pcs=["赛璃", "伊莉雅"],
            pc_resources={
                "赛璃": {"hp": 45, "max_hp": 45, "mp": 50, "max_mp": 50, "statuses": []},
                "伊莉雅": {"hp": 60, "max_hp": 60, "mp": 40, "max_mp": 40, "statuses": []},
            },
            legal_spells=["治愈术", "屏障"],
        )

        candidates = ConstrainedPlayerSimulator._safe_spell_action_candidates(
            "赛璃",
            context,
            "伊莉雅守在门口。",
        )

        self.assertTrue(all("治愈术" not in item for item in candidates))
        self.assertTrue(any("屏障" in item for item in candidates))

    def test_player_simulator_rejects_healing_a_full_health_target(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="full_health_heal",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃", "伊莉雅"],
            pc_resources={
                "赛璃": {"hp": 45, "max_hp": 45, "mp": 50, "max_mp": 50, "statuses": []},
                "伊莉雅": {"hp": 60, "max_hp": 60, "mp": 40, "max_mp": 40, "statuses": []},
            },
            legal_actions=["施放已掌握法术"],
            legal_spells=["治愈术"],
            legal_spell_rules=[
                {
                    "name": "治愈术",
                    "effect_type": "heal",
                    "selectable_damage_types": [],
                    "selectable_statuses": [],
                    "selectable_attributes": [],
                }
            ],
        )

        errors = simulator.validate(
            "赛璃对伊莉雅施放已掌握的【治愈术】。",
            step=step,
            legal_context=context,
        )

        self.assertIn("healing_spell_has_no_wounded_target", errors)

    def test_action_slot_rejects_a_second_action_disguised_as_incidental(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="incidental_second_action",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            legal_actions=["调查", "互动"],
        )
        message = "洛岚去看白碑后的尘雾和号角来源，同时顺手把檐下的白花风铃摘近些检查。"

        errors = simulator.validate(message, step=step, legal_context=context)

        self.assertIn("action_slot_contains_multiple_actions", errors)

    def test_action_slot_allows_speech_alongside_one_physical_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="speech_with_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            legal_actions=["调查", "互动"],
        )
        message = "赛璃检查旅人手腕上的刻痕，同时问他这里疼不疼。"

        errors = simulator.validate(message, step=step, legal_context=context)

        self.assertNotIn("action_slot_contains_multiple_actions", errors)

    def test_player_simulator_rejects_rephrased_traveller_memory_question(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "阿凛：伊莉雅问失名旅人：你还记得从哪条路来，或那行字最后的地点吗？\n"
            "时悠：他想不出从哪条路来，也认不出那行字里的地点。"
        )
        repeated = (
            "赛璃看着失名旅人问：那段地点信息里有没有具体字眼或地标，"
            "你还能辨认出来吗？"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, recent))

    def test_player_simulator_allows_new_traveller_question_topic(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "阿凛：伊莉雅问失名旅人：你还记得从哪条路来，或那行字最后的地点吗？\n"
            "时悠：他想不出从哪条路来，也认不出那行字里的地点。"
        )
        followup = "赛璃问失名旅人：你身上的刻痕疼不疼，呼吸还能稳住吗？"

        self.assertFalse(simulator._repeats_recent_action_lane(followup, recent))

    def test_player_simulator_rejects_second_probe_of_same_powder_and_wax(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "阿凛：伊莉雅查看灰晶粉末和蜡屑，想确认它们是否刚被人带过。\n"
            "时悠：灰晶粉末与蜡屑混着布纤维，朝白碑后方偏东侧延伸。"
        )
        repeated = (
            "赛璃拨开木梁缝边的蜡屑，想把灰晶粉末朝偏东侧延伸的痕迹看得更清楚。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, recent))

    def test_player_simulator_allows_following_new_fragment_revealed_by_previous_check(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "阿凛：伊莉雅查看灰晶粉末和蜡屑，想确认它们是否刚被人带过。\n"
            "时悠：灰晶粉末与蜡屑朝偏东侧延伸，起点夹着一片压扁的铃片碎边。"
        )
        followup = "洛岚收好铃片碎边，检查上面的刻纹是否与驿站风铃一致。"

        self.assertFalse(simulator._repeats_recent_action_lane(followup, recent))

    def test_player_simulator_rejects_rechecking_wax_seal_placement_and_match(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "阿凛：伊莉雅把半片灰色蜡封捡起来，看清上面的痕迹和它是怎么被卡进去的。\n"
            "时悠：蜡封是从底座右侧窄缝被硬塞进去的，压痕与风铃内侧方向一致。"
        )
        repeated = (
            "赛璃把半片灰色蜡封取出来，确认它是怎么被塞进去的，"
            "同时对照风铃内侧压痕能不能对得上。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, recent))

    def test_multiline_gm_prose_does_not_consume_a_player_action_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="multiline_gm_scene",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            legal_actions=["调查"],
        )
        recent = (
            "时悠：白花风铃后侧沾着一线薄霜。\n"
            "阿雾一直看着风铃后面，手里攥着布袋。\n"
            "旧路本身才是守望会卡住的那一层。\n"
            "阿凛：伊莉雅转向白穗，询问旧路与记忆罐的处置。"
        )

        errors = simulator.validate(
            "赛璃走到阿雾身边，顺着她的视线检查风铃后侧的薄霜。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("repeats_recent_action_lane", errors)

    def test_latest_gm_reply_keeps_multiline_continuation(self) -> None:
        context = (
            "白河：洛岚检查门轴。\n"
            "时悠：门轴上有新鲜灰粉。\n"
            "粉末一路延伸到侧门，那里还有半枚鞋印。"
        )

        latest = ConstrainedPlayerSimulator._latest_gm_reply(context)

        self.assertIn("门轴上有新鲜灰粉", latest)
        self.assertIn("半枚鞋印", latest)

    def test_rule_glossary_teaches_final_fabula_terms_to_player_layer(self) -> None:
        rendered = FINAL_FABULA_GLOSSARY.render_for_player_prompt(
            legal_actions=["推进目标命刻", "消耗物语点引入事实"]
        )

        self.assertIn("物语点", rendered)
        self.assertIn("目标命刻", rendered)
        self.assertIn("检定永远是两颗骰", rendered)
        self.assertIn("推进目标命刻", rendered)

    def test_player_simulator_rejects_unsupported_spell_and_uses_fallback(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="bad_spell",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            message="南星: 赛璃施放超级复活术，自动成功把大家回满。",
        )
        context = LegalActionContext(
            stage_goal="测试非法法术拦截",
            conflict_active=False,
            known_pcs=["赛璃"],
            legal_actions=["调查"],
            legal_spells=["治愈术"],
        )

        utterance = simulator.compose(step=step, legal_context=context)

        self.assertTrue(utterance.used_fallback)
        self.assertIn("unsupported_spell_claim", utterance.validation_errors)
        self.assertNotIn("超级复活术", utterance.text)

    def test_player_simulator_answers_gm_theme_followup_before_scripted_line(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="theme_followup",
            kind="session_zero_message",
            speaker="时雨",
            actor="艾薇娅",
            message="时雨: 我先去调查旧钟塔，不理刚才的问题。",
        )
        context = LegalActionContext(
            stage_goal="补充角色主题",
            legal_actions=["创建或补全角色"],
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="艾薇娅的主题“妥协”很有意思。它通常会在什么时刻推着这位英雄行动？又有什么底线会让他拒绝退让？",
        )

        self.assertIn("妥协", utterance.text)
        self.assertIn("底线", utterance.text)
        self.assertNotIn("旧钟塔", utterance.text)

    def test_player_simulator_answers_explicit_npc_identity_question_before_clock_pressure(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="npc_identity_followup",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            message="阿凛: 伊莉雅继续检查闸门，不理刚才的问题。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["监察官艾蕾娜"],
            active_clocks=["[财团巡逻队逼近] 5/8"],
            legal_actions=["社交交涉", "调查"],
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply=(
                "监察官艾蕾娜盯着廊柱后的影子：\n"
                "“现在只要你把姓名、你与柱影里那位的关系、以及你是不是代表他答话，说清楚。”\n"
                "【财团巡逻队逼近】5/8"
            ),
        )

        self.assertIn("艾蕾娜", utterance.text)
        self.assertIn("我是伊莉雅", utterance.text)
        self.assertIn("只代表自己", utterance.text)
        self.assertNotIn("命刻", utterance.text)
        self.assertNotIn("检查闸门", utterance.text)

    def test_player_simulator_does_not_mistake_a_named_memory_for_pc_identity_question(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="memory_name_not_pc_identity",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["白花守望会会长", "失名旅人"],
            legal_actions=["社交交涉", "调查", "保护同行者"],
        )
        reply = (
            "白花守望会会长说：‘下一声铃响后，失名旅人只需说清，"
            "那名字听起来像自己的，还是像别人；说完，我让巡守带你们通过。’"
        )

        self.assertIsNone(
            simulator._explicit_npc_identity_question(
                reply,
                known_npcs=context.known_npcs,
            )
        )

    def test_player_simulator_answers_only_the_requested_character_piece(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="partial_character",
            kind="session_zero_message",
            speaker="南星",
            actor="赛璃",
            intent="创建角色",
        )
        context = LegalActionContext(
            stage_goal="补充角色身份",
            legal_actions=["创建或补全角色"],
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="赛璃还没听到身份；先说一句她目前怎么看待自己就好。",
        )

        self.assertIn("身份", utterance.text)
        self.assertNotIn("属性", utterance.text)
        self.assertNotIn("装备", utterance.text)
        self.assertNotIn("技能", utterance.text)

    def test_player_simulator_character_fallback_is_not_full_form_fill(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="human_character_seed",
            kind="session_zero_message",
            speaker="白河",
            actor="洛岚",
            intent="创建角色",
        )
        context = LegalActionContext(
            stage_goal="角色初步构思",
            legal_actions=["创建或补全角色"],
        )

        utterance = simulator.compose(step=step, legal_context=context)

        self.assertTrue(utterance.used_fallback)
        self.assertIn("先", utterance.text)
        self.assertNotIn("身份、主题、故乡、职业、属性和技能", utterance.text)
        self.assertNotIn("属性", utterance.text)

    def test_player_simulator_does_not_wrap_safety_or_duplicate_check_request(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        context = LegalActionContext(stage_goal="第零章")
        safety = ReplayStep(
            id="safety",
            kind="session_zero_message",
            speaker="时雨",
            stage_goal="确认安全工具",
            intent="safety",
            method_hint="界限：不要酷刑细节。帷幕：亲密内容淡出。",
        )
        investigation = ReplayStep(
            id="investigate",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="调查旅人",
            intent="调查",
            target="失名旅人",
            method_hint="观察旅人的呼吸；如果需要检定，请按合适属性处理。",
        )

        safety_text = simulator.compose(step=safety, legal_context=context).text
        investigation_text = simulator.compose(step=investigation, legal_context=context).text

        self.assertEqual(safety_text.count("大家觉得"), 0)
        self.assertEqual(investigation_text.count("需要检定"), 0)
        self.assertIn("失名旅人", investigation_text)

    def test_player_simulator_does_not_treat_acknowledgement_nouns_as_questions(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="country",
            kind="session_zero_message",
            speaker="南星",
            stage_goal="贡献国家",
            intent="world_detail",
            method_hint="我贡献一个国家：钟鸣公国依靠正午大钟安抚灵魂。",
        )
        context = LegalActionContext(stage_goal="创建世界")

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="魔法与科技的关系定下来了，之后城市、装备和冲突都会按这个味道展开。",
        )

        self.assertIn("钟鸣公国", utterance.text)
        self.assertNotIn("法杖", utterance.text)

    def test_legal_action_layer_mentions_out_of_turn_limit(self) -> None:
        context = LegalActionContext(
            stage_goal="测试抢跑",
            current_actor="伊莉雅",
            conflict_active=True,
            known_pcs=["伊莉雅", "洛岚"],
            legal_actions=["回合外等待", "给当前行动者建议"],
            notes=["当前行动者是 伊莉雅，洛岚 不能结算消耗回合的行动。"],
        )

        rendered = LegalActionLayer().as_prompt_block(context)

        self.assertIn("当前行动者：伊莉雅", rendered)
        self.assertIn("回合外等待", rendered)
        self.assertIn("不能结算消耗回合", rendered)

    def test_player_simulator_does_not_auto_reply_to_clock_pressure_out_of_turn(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="clock_followup",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
        )
        context = LegalActionContext(
            stage_goal="测试行动条",
            current_actor="赛璃",
            conflict_active=True,
            known_pcs=["伊莉雅", "赛璃"],
            active_clocks=["[财团巡逻队逼近] 5/6"],
            legal_actions=["回合外等待"],
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="命刻【财团巡逻队逼近】变化：4/6 -> 5/6。下一位行动者：赛璃。",
        )

        self.assertTrue(utterance.used_fallback)
        self.assertIn("等轮到我", utterance.text)
        self.assertNotIn("提醒当前行动者", utterance.text)

    def test_action_slot_rejects_peer_suggestion_without_committed_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="committed_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            legal_actions=["调查"],
        )

        suggestion_errors = simulator.validate(
            "我建议先别散开，赛璃可以先去确认暗号还在不在原处吗？",
            step=step,
            legal_context=context,
        )
        action_errors = simulator.validate(
            "赛璃先去检查守望会暗号，确认它有没有被人动过。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_contains_only_table_discussion", suggestion_errors)
        self.assertNotIn("action_slot_contains_only_table_discussion", action_errors)
        self.assertNotIn("action_slot_has_no_committed_action", action_errors)

    def test_action_slot_accepts_natural_direct_npc_questions_with_dialogue_colons(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="direct_npc_question",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["希缇·白栅"],
            legal_actions=["互动", "调查"],
        )

        messages = (
            "我走到希缇·白栅面前，直接问她：我们要把失忆旅人送往哪里，才算安全路线？",
            "伊莉雅直接走向希缇·白栅，开口问她：白花守望会愿不愿意让旅人走旧路？",
            "伊莉雅往前一步，看着希缇·白栅问一句：你需要我们先证明什么？",
        )
        for message in messages:
            with self.subTest(message=message):
                errors = simulator.validate(message, step=step, legal_context=context)
                self.assertNotIn("action_slot_contains_only_table_discussion", errors)

    def test_action_slot_accepts_action_after_abandoning_an_old_approach(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="pivot_to_fresh_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            legal_actions=["调查"],
        )

        errors = simulator.validate(
            "既然旅人已经退开，我不再碰花瓣或门槛，转而观察驿站内侧的旧路闸门。",
            step=step,
            legal_context=context,
        )

        self.assertNotIn("action_slot_contains_only_table_discussion", errors)

    def test_validated_fallback_handles_newly_disclosed_object_instead_of_rechecking_it(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="disposition_fallback",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            known_npcs=["白花守望会会长"],
            active_clocks=["【财团巡逻队逼近】0/8"],
            settled_npc_exchanges=[
                {
                    "npc": "白花守望会会长",
                    "outcome": "accepted",
                    "settled_terms": "放行北侧风铃廊旧阶；同行时避开主铃架",
                    "player_performance": "pending",
                }
            ],
        )
        public_context = (
            "时悠: 走北侧风铃廊旧阶时别靠近主铃架。\n"
            "时悠: 那片银白铃舌该回主铃架附近的那一侧，不该留在外头。"
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "澄砚: 苍祈先不打断会长，留意她的视线。",
            step=step,
            legal_context=context,
            recent_public_context=public_context,
            last_gm_reply="那片银白铃舌不该留在外头。",
        )

        self.assertEqual(errors, [])
        self.assertIn("银白铃舌", fallback)
        self.assertIn("递到白花守望会会长面前", fallback)
        self.assertIn("不靠近主铃架", fallback)

    def test_validated_fallback_uses_new_detail_after_another_player_uncovers_a_sign(self) -> None:
        """A fresh reveal must not strand the next hero in the old road lane."""

        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="uncovered-sign-fallback",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
            visible_scene_elements=["辉钢收购旗", "旧路标"],
            legal_actions=["调查", "互动"],
        )
        public_context = "\n".join(
            [
                "时悠：旧路前方的岔口亮起一盏冷白提灯；骑手们把一面辉钢收购旗钉上路牌，旗尾压住了原先的旧路标。",
                "时雨：艾薇娅沿旧路朝被辉钢收购旗压住的路牌走去，伸手掀开旗尾，露出下面的旧路标。",
                "时悠：旗尾被掀起，下面的旧路标重新露出：褪色箭头和残缺字样指向旧路前方，但具体地名已无法从现状辨清。辉钢旗仍钉在牌上。",
            ]
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "澄砚：苍祈贴住入口听外面的脚步方位，同时用肩膀抵住最先震动的那一侧。",
            step=step,
            legal_context=context,
            recent_public_context=public_context,
            last_gm_reply="旗尾被掀起，下面的旧路标重新露出。",
        )

        self.assertEqual(errors, [])
        self.assertIn("旧路标", fallback)
        self.assertIn("钉孔", fallback)
        self.assertNotIn("具体地名", fallback)
        self.assertNotIn("贴住入口", fallback)

    def test_action_slot_accepts_committed_oath_and_guarantee_actions(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="committed_oath",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["岑老太"],
            legal_actions=["互动", "普通叙事行动"],
        )

        committed = simulator.validate(
            "洛岚选择以自己的名义承担这项誓约，当众说清愿意为旅人引发的旧路风险负责，并请在场守望者见证。",
            step=step,
            legal_context=context,
        )
        discussion_only = simulator.validate(
            "我觉得可以找个人担保，大家认为谁来宣誓比较合适？",
            step=step,
            legal_context=context,
        )

        self.assertNotIn("action_slot_contains_only_table_discussion", committed)
        self.assertNotIn("action_slot_has_no_committed_action", committed)
        self.assertIn("action_slot_contains_only_table_discussion", discussion_only)

    def test_action_slot_rejects_delegating_the_turn_to_another_pc(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="delegated_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
            legal_actions=["互动", "普通叙事行动"],
        )

        for message in (
            "赛璃转向洛岚，请他把自己的名字签到旧路担保上。",
            "赛璃问队友：谁能把自己的名字落到旧路担保上？",
            "赛璃催促洛岚先开门，她在旁边等着。",
            "洛岚现在伊莉雅当面说明失名旅人的具体去向，并以自己的名义承担护送责任。",
        ):
            with self.subTest(message=message):
                self.assertIn(
                    "action_slot_delegates_to_teammate",
                    simulator.validate(message, step=step, legal_context=context),
                )

        personal_action = simulator.validate(
            "赛璃在旧路担保上签下自己的名字，再把笔递给洛岚。",
            step=step,
            legal_context=context,
        )
        self.assertNotIn("action_slot_delegates_to_teammate", personal_action)

    def test_action_slot_rejects_malformed_adjacent_teammate_delegation(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="malformed_delegated_action",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
            legal_actions=["互动", "普通叙事行动"],
        )

        errors = simulator.validate(
            "洛岚现在伊莉雅当面说明失名旅人的具体去向，并以自己的名义承担护送责任。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_delegates_to_teammate", errors)

    def test_open_condition_fallback_does_not_assign_another_heroes_personal_price(self) -> None:
        action = ConstrainedPlayerSimulator._open_condition_action_fallback(
            "洛岚",
            [
                {
                    "npc": "白花守望会会长",
                    "condition": "伊莉雅当面说明失名旅人的具体去向，并以自己的名义承担护送责任。",
                    "promised_result": "开放旧路。",
                    "status": "open",
                }
            ],
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
        )

        self.assertEqual(action, "")

    def test_action_slot_cannot_claim_another_players_existing_promise(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="promise_owner",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃"],
            known_npcs=["白栀会长"],
            legal_actions=["互动"],
        )
        recent = (
            "阿凛：伊莉雅对失名旅人点头：我来守着你走过前段廊道。\n"
            "时悠：白栀会长看向钥盒，等候现场说明。"
        )

        stolen = simulator.validate(
            "赛璃把现场反应告诉白栀会长：我已经答应护送失名旅人，现在请你放行。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        attributed = simulator.validate(
            "赛璃把现场反应告诉白栀会长：伊莉雅已经答应护送失名旅人，现在请你判断说明是否足够。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("claims_another_players_commitment", stolen)
        self.assertNotIn("claims_another_players_commitment", attributed)

    def test_action_slot_cannot_pay_out_an_npc_controlled_promise(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="npc_promise_owner",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["白花守望会的人"],
            visible_scene_elements=["门边白花风铃", "旧路入口"],
            legal_actions=["互动", "普通叙事行动"],
        )

        invalid = simulator.validate(
            "伊莉雅现在把门边这串风铃继续按稳，别让它再响起；等这一点做成，我就把旧路给你。",
            step=step,
            legal_context=context,
        )
        valid = simulator.validate(
            "伊莉雅把门边这串风铃继续按稳，不让它再响。",
            step=step,
            legal_context=context,
        )

        self.assertIn("claims_npc_controlled_outcome", invalid)
        self.assertNotIn("claims_npc_controlled_outcome", valid)

    def test_action_slot_rejects_addressing_its_own_actor_as_a_teammate(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="self_addressed_action",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
            legal_actions=["互动"],
        )

        errors = simulator.validate(
            "洛岚，我现在就要你把名字签在担保上。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_delegates_to_teammate", errors)

    def test_action_slot_rejects_conditional_backup_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="conditional_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"], legal_actions=["调查", "施法"])

        errors = simulator.validate(
            "赛璃先观察门缝；如果必要，我也可以施放屏障。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_contains_conditional_future_action", errors)

    def test_action_slot_rejects_conditional_backup_with_spatial_phrase(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="conditional_action_with_spatial_phrase",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            legal_actions=["交涉", "施法"],
        )

        errors = simulator.validate(
            "伊莉雅先要求财团停手；如果他们要硬推，我就立刻在门口撑开元素幕障。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_contains_conditional_future_action", errors)

    def test_player_simulator_rejects_invented_elemental_shroud_effect(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="canonical_spell_effect",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["失忆旅人"],
            legal_actions=["施放已掌握法术"],
            legal_spells=["元素幕障"],
            legal_spell_rules=[
                {
                    "name": "元素幕障",
                    "target": "up_to_three_creatures",
                    "target_label": "一至三个生物",
                    "effect_type": "affinity_buff",
                    "requires_check": False,
                    "description": "目标对所选元素获得抵抗相性。",
                    "selectable_damage_types": ["风", "雷", "冰", "火", "土"],
                    "selectable_statuses": [],
                    "selectable_attributes": [],
                }
            ],
        )

        invalid = simulator.validate(
            "伊莉雅施放【元素幕障】遮住驿站廊口与旅人的身影，拖慢财团巡逻队。",
            step=step,
            legal_context=context,
        )
        valid = simulator.validate(
            "伊莉雅对失忆旅人施放【元素幕障】，选择火系，让他在场景中获得火系抵抗。",
            step=step,
            legal_context=context,
        )

        self.assertIn("spell_missing_required_parameter", invalid)
        self.assertIn("spell_effect_mismatch", invalid)
        self.assertNotIn("spell_missing_required_parameter", valid)
        self.assertNotIn("spell_effect_mismatch", valid)

    def test_action_slot_rejects_conditional_backup_npc_question(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="conditional_backup_question",
            kind="game_turn",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["艾薇娅"],
            legal_actions=["交谈"],
        )
        message = (
            "艾薇娅先问岑烛空白收据来自哪里；如果他还是只说不能确定，"
            "那我就改问他能不能把票联给我看一眼。"
        )

        errors = simulator.validate(
            message,
            step=step,
            legal_context=context,
            recent_public_context="时悠：岑烛站在柜台后。",
        )

        self.assertIn("action_slot_contains_conditional_future_action", errors)

    def test_action_slot_must_react_when_a_threat_clock_just_completed(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="react_to_completed_threat",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            immediate_scene_consequence="财团巡逻队包围白花碑驿站。",
            legal_actions=["普通叙事行动", "调查"],
        )
        recent = (
            "时雨：艾薇娅检查柜台上的压痕。\n"
            "时悠：【财团巡逻队逼近】8/8。财团巡逻队包围白花碑驿站。"
        )

        ignored = simulator.validate(
            "苍祈继续比对白瓷铃内侧的刻痕。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        reacted = simulator.validate(
            "苍祈立刻收起拓印，挡在旅人身前，准备和进门的巡逻队交涉。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("ignores_immediate_scene_consequence", ignored)
        self.assertNotIn("ignores_immediate_scene_consequence", reacted)

    def test_action_slot_rejects_third_repeat_of_same_object_and_method(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="repeated_lane",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"], legal_actions=["调查"])
        recent = (
            "阿凛：伊莉雅贴近门缝观察外面的巡逻灯火。\n"
            "时悠：灯火仍在路口移动。\n"
            "白河：洛岚蹲在门缝旁检查巡逻灯火的变化。"
        )

        errors = simulator.validate(
            "赛璃继续贴着门缝观察巡逻灯火。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", errors)

    def test_action_slot_groups_repeated_door_control_methods_into_one_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="door_loop",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["苍祈"], legal_actions=["调查"])
        recent = (
            "阿凛：伊莉雅用盾守住门缝，不让巡逻队看进来。\n"
            "时悠：门外撞击震落了白漆。\n"
            "白河：洛岚把木柜推到门口挡住巡逻视线。"
        )

        errors = simulator.validate(
            "苍祈绕到门边继续按住松动的门板。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", errors)

    def test_action_slot_rejects_cross_family_rechecks_of_same_saturated_evidence(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="evidence_loop",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["苍祈"], legal_actions=["调查"])
        recent = (
            "阿凛：伊莉雅检查黑纸上的灰色粉屑。\n"
            "时悠：粉屑已经确认来自门轴。\n"
            "白河：洛岚把黑纸和灰粉对齐核验。"
        )

        errors = simulator.validate(
            "苍祈把黑纸收拢，再确认灰色粉屑有没有遗漏。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", errors)

    def test_action_slot_must_answer_enemy_ultimatum_instead_of_rechecking_door(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="answer_ultimatum",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"], legal_actions=["防御"])
        recent = "时悠：门外的监察官给出最后警告：现在开门交出旅人，否则立刻破门。"

        ignored = simulator.validate(
            "赛璃继续检查门缝有没有透光。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        answered = simulator.validate(
            "赛璃拒绝交出旅人，举盾守住门口准备迎战。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("ignores_explicit_gm_affordance", ignored)
        self.assertNotIn("ignores_explicit_gm_affordance", answered)

    def test_action_slot_must_answer_is_or_choice_instead_of_asking_npc_again(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="answer_is_or_choice",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["灰金短斗篷的财团使者", "失名旅人"],
            legal_actions=["互动", "防御"],
        )
        recent = (
            "时悠：灰金短斗篷的财团使者敲着门问：‘是按这条去路开口，"
            "还是把人和路一起拖到财团面前？’"
        )
        deferred = "洛岚转向失名旅人，问他这句话现在能不能拿去回使者。"
        answered = "洛岚选择只按已经公开的去路开口，明确拒绝把旅人交给财团。"

        deferred_errors = simulator.validate(
            deferred,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        answered_errors = simulator.validate(
            answered,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("ignores_explicit_gm_affordance", deferred_errors)
        self.assertNotIn("ignores_explicit_gm_affordance", answered_errors)

    def test_action_slot_must_answer_only_if_otherwise_ultimatum(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="answer_only_if_ultimatum",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["灰金短斗篷的财团使者", "失名旅人"],
            visible_scene_elements=["灰纸信封", "门外石阶"],
            legal_actions=["互动", "防御"],
        )
        recent = (
            "失名旅人说：完整名字、全段走法和终点，我还是不说。\n"
            "时悠：灰金短斗篷的财团使者说：现在只要把你们知道的那条去路说出来，"
            "财团就不再逼着门外的人等；否则，我就把这张收购单留在这里等巡逻队来。"
        )
        ignored = simulator.validate(
            "洛岚靠近灰金短斗篷的财团使者，仔细观察他的斗篷和信封。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        answered = simulator.validate(
            "洛岚拒绝再交出更多去路，不碰信封，留在门内准备承担使者宣布的后果。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        fallback = simulator._affordance_response_fallback(
            "洛岚",
            recent,
            known_npcs=context.known_npcs,
        )

        self.assertIn("ignores_explicit_gm_affordance", ignored)
        self.assertNotIn("ignores_explicit_gm_affordance", answered)
        self.assertIn("拒绝再交出更多去路", fallback)

    def test_open_bargain_is_not_misread_as_hostile_ultimatum(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="accept_open_bargain",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        condition = {
            "condition_id": "oath-or-proof",
            "npc": "岑老太",
            "condition": "提供能证明旅人无害的材料，或当众承担一项守望誓约。",
            "promised_result": "开具旧路花章通行纸。",
            "status": "open",
        }
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["岑老太"],
            legal_actions=["叙事行动"],
            open_npc_conditions=[condition],
        )
        recent = (
            "时悠：岑老太说，我的条件不变：要么拿出能当场核对的证明；"
            "要么当众以自己的名义承担守望誓约。条件成了，我就开具花章通行纸。"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply=recent,
            recent_public_context=recent,
        )

        self.assertEqual(result.validation_errors, [])
        self.assertIn("守望誓约", result.text)
        self.assertNotIn("最后通牒", result.text)
        self.assertNotIn(
            "ignores_explicit_gm_affordance",
            simulator.validate(
                result.text,
                step=step,
                legal_context=context,
                recent_public_context=recent,
            ),
        )

    def test_accepting_oath_then_asking_its_procedure_is_not_rejected_as_repetition(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="accept_oath_after_saturated_questions",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal=(
                "这是行动槽：必须提交当前角色的一个明确行动。"
                "本行动不得继续向NPC追问同一个条件。"
            ),
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚", "赛璃"],
            known_npcs=["岑老太"],
            legal_actions=["叙事行动"],
            open_npc_conditions=[
                {
                    "condition_id": "oath-or-proof",
                    "npc": "岑老太",
                    "condition": "提供能当场核对的证明，或当众承担一项守望誓约。",
                    "promised_result": "开放旧路。",
                    "status": "open",
                }
            ],
        )
        recent = (
            "时悠：岑老太把条件说清：要么拿出能当场核对的证明；"
            "要么当众以自己的名义担下守望誓约。条件成了，我就开放旧路。"
        )

        accepted_lines = [
            "白河：洛岚当众担下守望誓约，请岑老太告诉我现在该怎样起誓。",
            "白河：洛岚直接选第二条，以自己的名义承担守望誓约。",
            "白河：洛岚抬手按在胸前，当众替旅人担起这项守望誓约。",
        ]
        for line in accepted_lines:
            errors = simulator.validate(
                line,
                step=step,
                legal_context=context,
                recent_public_context=recent,
            )
            self.assertNotIn("ignores_explicit_gm_affordance", errors, line)
            self.assertNotIn("repeats_saturated_npc_question_lane", errors, line)
            self.assertNotIn("action_slot_delegates_to_teammate", errors, line)

        unresolved = simulator.validate(
            "白河：洛岚问岑老太，这项誓约具体是什么意思？",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        self.assertIn("repeats_saturated_npc_question_lane", unresolved)

    def test_action_lane_detects_repeated_wind_chime_investigation(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="wind_chime_loop",
            kind="game_turn",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["艾薇娅"], legal_actions=["调查"])
        recent = (
            "白河：洛岚俯身检查风铃的接缝与磨损。\n"
            "时悠：风铃内侧露出一道被刮断的刻痕。\n"
            "南星：赛璃把风铃刻痕的新旧边缘逐段比对。"
        )

        errors = simulator.validate(
            "艾薇娅再次检查风铃的接缝和磨损。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", errors)

    def test_action_lane_rejects_second_players_duplicate_oil_trace_direction_check(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="oil_trace_direction_loop",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃"],
            visible_scene_elements=["漆木匣", "匣身油渍"],
            legal_actions=["调查", "互动"],
        )
        recent = (
            "阿凛：伊莉雅蹲下看漆木匣的白漆纹样和新鲜油渍，顺着油渍找它从哪个方向蹭过来。\n"
            "时悠：油渍沿匣身一侧擦过，暂时还不能断定来自门边、地面或背墙。"
        )

        repeated = simulator.validate(
            "赛璃顺着漆木匣边角的新鲜油渍仔细摸一遍，确认它是往哪个方向蹭上来的。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        fresh = simulator.validate(
            "赛璃不再追油渍，转向失名旅人，问他匣子出现前听见过什么动静。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", repeated)
        self.assertNotIn("repeats_recent_action_lane", fresh)

    def test_action_lane_checks_repeated_physical_prefix_before_fresh_npc_question(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = "\n".join(
            [
                "阿凛：伊莉雅把失名旅人带离门口视线，引到屋里更安全的角落。",
                "时悠：失名旅人已经跟着伊莉雅走到屋内角落，门外看不到他们。",
                "南星：赛璃又把失名旅人引到屋里那个离门口视线更远的位置。",
                "时悠：旅人留在墙后的角落。",
            ]
        )
        repeated = (
            "洛岚先把失名旅人带到屋里更深处那个离门口视线更远的角落，"
            "然后转向财团使者问他交接方式是在门口谈，还是带给屋里的人决定。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, recent))

    def test_action_lane_checks_repeated_committed_move_before_attention_pivot(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = "\n".join(
            [
                "阿凛：伊莉雅把失名旅人带离门口视线，引到屋里更安全的角落。",
                "时悠：失名旅人已经跟着伊莉雅走到屋内角落，门外看不到他们。",
                "南星：赛璃把失名旅人引到屋里那个离门口视线更远的位置。",
                "时悠：旅人留在墙后的角落。",
            ]
        )
        repeated = (
            "洛岚不再去碰门槛那条线；我现在就陪失名旅人退到屋内更深处那个"
            "看不见门口的位置，并把注意力转到门外的财团使者身上。"
        )

        self.assertTrue(simulator._repeats_recent_action_lane(repeated, recent))

    def test_action_lane_allows_abandoning_old_watch_to_address_new_npc(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = "\n".join(
            [
                "南星：赛璃检查白花风铃内侧的旧痕。",
                "时悠：旧痕已经确认被近距离碰过。",
                "白河：洛岚把风铃和门缝里的风痕分开记录。",
                "时悠：门外的监察官艾蕾娜把封条信按在门框上。",
            ]
        )
        fresh = (
            "苍祈收回盯着风铃的视线，直接转向门外的监察官艾蕾娜说："
            "你要的是旧痕记录，不是门本身；想拿走什么，就把真正要看的那份说清楚。"
        )

        self.assertFalse(simulator._repeats_recent_action_lane(fresh, recent))

    def test_action_slot_rejects_repeating_information_already_accepted_by_same_npc(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="accepted_route_repeat",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["灰金短斗篷的财团使者", "失名旅人"],
            legal_actions=["互动", "调查"],
        )
        recent = "\n".join(
            [
                "南星：赛璃把那一小段方向感转告门外的财团使者。",
                "时悠：灰金短斗篷的财团使者接着答道：‘可以。你们可以按那一小段方向感来开口；完整名字、全段走法和终点别摊开。’",
                "时悠：使者又敲了敲门：‘把那一小段方向感说给我听。’",
            ]
        )
        repeated = (
            "洛岚直接回应灰金短斗篷的财团使者：我现在把那段方向感讲给你听，"
            "先贴着墙根避开门槛，再往南偏东去；完整名字、全段走法和终点不会摊开。"
        )

        errors = simulator.validate(
            repeated,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_resolved_information_delivery", errors)

    def test_action_slot_allows_first_information_relay_to_a_different_npc(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="first_route_relay",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=["灰金短斗篷的财团使者", "失名旅人"],
            legal_actions=["互动"],
        )
        recent = (
            "时悠：失名旅人说道：‘可以。我只把一小段方向感告诉你：贴着墙根避开门槛，再往南偏东去；"
            "完整名字、全段走法和终点不能说。’"
        )
        first_relay = (
            "赛璃转向灰金短斗篷的财团使者，把旅人允许公开的方向告诉他："
            "先贴着墙根避开门槛，再往南偏东去；完整名字、全段走法和终点不说。"
        )

        errors = simulator.validate(
            first_relay,
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("repeats_resolved_information_delivery", errors)

    def test_action_slot_does_not_treat_a_new_request_as_repeated_delivery(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="new-request-after-record-review",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            known_npcs=["白花守望会会长"],
            legal_actions=["互动"],
        )
        recent = (
            "时悠：白花守望会会长已经接过候选记录，并确认现场核对后才能补入双线印记。"
        )

        errors = simulator.validate(
            "苍祈转向白花守望会会长：请你现在安排一名值守望带我们去现场核对。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("repeats_resolved_information_delivery", errors)

    def test_semantic_progress_can_authorize_explicitly_opened_movement(self) -> None:
        class MovementReviewClient:
            def create_chat_completion(self, **_kwargs):
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": True,
                        "repeats_micro_investigation_lane": False,
                        "responds_to_current_pressure_or_choice": True,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": True,
                        "opens_another_detail_layer": False,
                        "matches_prior_rejected_lane": False,
                        "movement_claimed": True,
                        "movement_is_authorized_by_public_context": True,
                        "movement_authorization_evidence": "值守望将随你们前往旧路闸门",
                        "repeats_resolved_information_delivery": False,
                        "evidence": "随队前往旧路闸门",
                        "reason": "GM已经明确开放这次移动。",
                    },
                    ensure_ascii=False,
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=MovementReviewClient(),
            model="semantic-test",
        )
        step = ReplayStep(
            id="authorized-scene-move",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室",
            scene_location="登记小室",
            known_pcs=["苍祈"],
            known_npcs=["白花守望会会长"],
        )
        recent = "时悠：值守望将随你们前往旧路闸门，现场核对沿线标志。"
        candidate = "苍祈带着候选记录离开登记小室，随队前往旧路闸门。"
        initial = simulator.validate(
            candidate,
            step=step,
            legal_context=context,
            recent_public_context="时悠：队伍仍在登记小室。",
        )
        self.assertIn("leaves_current_scene_without_transition", initial)

        reviewed = simulator._review_candidate_semantics(
            candidate,
            [*initial, "repeats_resolved_information_delivery"],
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("leaves_current_scene_without_transition", reviewed)
        self.assertNotIn("repeats_resolved_information_delivery", reviewed)

    def test_semantic_progress_rejects_reopening_an_exhausted_npc_knowledge_lane(self) -> None:
        class ExhaustedKnowledgeReviewClient:
            def create_chat_completion(self, **_kwargs):
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": True,
                        "repeats_micro_investigation_lane": False,
                        "responds_to_current_pressure_or_choice": False,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": False,
                        "opens_another_detail_layer": True,
                        "matches_prior_rejected_lane": False,
                        "reopens_exhausted_npc_knowledge_lane": True,
                        "new_public_evidence_reopens_npc_knowledge_lane": False,
                        "npc_knowledge_boundary_evidence": "我亲眼记得的，只有回执上写着",
                        "movement_claimed": False,
                        "movement_is_authorized_by_public_context": False,
                        "movement_authorization_evidence": "",
                        "repeats_resolved_information_delivery": False,
                        "evidence": "你最后还记得的清晰片段是什么",
                        "reason": "旅人已经明确只记得回执与半环印记，没有新刺激却再次追问其来路和最后记忆。",
                    },
                    ensure_ascii=False,
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ExhaustedKnowledgeReviewClient(),
            model="semantic-test",
        )
        step = ReplayStep(
            id="exhausted-npc-memory",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            known_npcs=["失忆旅人"],
            present_npcs=["失忆旅人"],
        )
        recent = (
            "赛璃：看到半环印记时，你能想起自己为何被送来吗？\n"
            "时悠：我亲眼记得的，只有回执上写着‘无名旅者，钟鸣前暂存’，"
            "以及半环印记；它们没有让我想起被送来的原因。\n"
            "时悠：听见伊瑟娅这个名字时，我没有具体的记忆。"
        )
        candidate = (
            "赛璃问失忆旅人：你最后还记得的清晰片段是什么？"
            "来到驿站前是谁带你走、你原本要去哪里？"
        )

        reviewed = simulator._review_candidate_semantics(
            candidate,
            [],
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertTrue(
            any(
                item.startswith("semantic_action_reopens_exhausted_npc_knowledge:")
                for item in reviewed
            )
        )
        self.assertTrue(
            any(item.startswith("semantic_action_without_progress:") for item in reviewed)
        )

    def test_semantic_progress_preserves_other_players_and_npc_agency(self) -> None:
        class AgencyReviewClient:
            def create_chat_completion(self, **_kwargs):
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": True,
                        "repeats_micro_investigation_lane": False,
                        "responds_to_current_pressure_or_choice": True,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": True,
                        "opens_another_detail_layer": False,
                        "matches_prior_rejected_lane": False,
                        "reopens_exhausted_npc_knowledge_lane": False,
                        "new_public_evidence_reopens_npc_knowledge_lane": False,
                        "npc_knowledge_boundary_evidence": "",
                        "controls_other_player_characters": True,
                        "party_action_authorized_by_public_consensus": False,
                        "controls_npc_outcome_without_public_answer": True,
                        "npc_outcome_already_public": False,
                        "movement_claimed": True,
                        "movement_is_authorized_by_public_context": True,
                        "movement_authorization_evidence": "去白花碑驿站·旧路闸门还是继续留在登记小室，由你们决定",
                        "repeats_resolved_information_delivery": False,
                        "evidence": "并让失忆旅人与队伍一同撤离",
                        "reason": "艾薇娅可以自己出发或邀请同行，但不能替其他PC和失忆旅人决定已经撤离。",
                    },
                    ensure_ascii=False,
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=AgencyReviewClient(),
            model="semantic-test",
        )
        step = ReplayStep(
            id="party-agency",
            kind="game_turn",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室",
            scene_location="白花碑驿站·登记小室",
            known_pcs=["艾薇娅", "赛璃", "伊莉雅"],
            known_npcs=["失忆旅人"],
            present_npcs=["失忆旅人"],
        )
        candidate = (
            "艾薇娅带头前往白花碑驿站·旧路闸门，"
            "并让失忆旅人与队伍一同撤离。"
        )

        reviewed = simulator._review_candidate_semantics(
            candidate,
            [],
            step=step,
            legal_context=context,
            recent_public_context=(
                "时悠：去白花碑驿站·旧路闸门还是继续留在登记小室，由你们决定；"
                "此刻没有人已经出发。"
            ),
        )

        self.assertTrue(
            any(item.startswith("semantic_action_controls_other_players:") for item in reviewed)
        )
        self.assertTrue(
            any(item.startswith("semantic_action_preempts_npc_decision:") for item in reviewed)
        )

    def test_semantic_progress_rejects_using_a_story_item_held_by_another_pc(self) -> None:
        class StoryItemCustodyReviewClient:
            def create_chat_completion(self, **kwargs):
                request = json.loads(kwargs["messages"][-1].content)
                self.story_items = request["authoritative_story_items"]
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": True,
                        "repeats_micro_investigation_lane": False,
                        "responds_to_current_pressure_or_choice": True,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": True,
                        "opens_another_detail_layer": False,
                        "matches_prior_rejected_lane": False,
                        "reopens_exhausted_npc_knowledge_lane": False,
                        "new_public_evidence_reopens_npc_knowledge_lane": False,
                        "controls_other_player_characters": False,
                        "party_action_authorized_by_public_consensus": False,
                        "controls_npc_outcome_without_public_answer": False,
                        "npc_outcome_already_public": False,
                        "movement_claimed": False,
                        "movement_is_authorized_by_public_context": False,
                        "movement_authorization_evidence": "",
                        "violates_story_item_custody": True,
                        "story_item_custody_evidence": "白蜡路封：持有者=艾薇娅",
                        "acts_outside_authoritative_actor_location": False,
                        "actor_location_evidence": "",
                        "repeats_resolved_information_delivery": False,
                        "evidence": "苍祈取下白蜡路封",
                        "reason": "白蜡路封当前由艾薇娅持有，苍祈不能直接使用。",
                    },
                    ensure_ascii=False,
                )

        client = StoryItemCustodyReviewClient()
        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=client,
            model="semantic-test",
        )
        step = ReplayStep(
            id="story-item-custody",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室",
            scene_location="白花碑驿站·登记小室",
            known_pcs=["苍祈", "艾薇娅"],
            actor_locations={"苍祈": "白花碑驿站·登记小室", "艾薇娅": "白花碑驿站·登记小室"},
            story_items=[
                {
                    "item_id": "white-wax-seal",
                    "name": "白蜡路封",
                    "holder": "艾薇娅",
                    "location": "白花碑驿站·登记小室",
                    "status": "carried",
                }
            ],
        )

        reviewed = simulator._review_action_progress_contract(
            "苍祈取下白蜡路封，把它嵌进旧路闸门的凹槽。",
            [],
            step=step,
            legal_context=context,
            recent_public_context="时悠：白蜡路封仍在艾薇娅手里。",
        )

        self.assertTrue(
            any(
                error.startswith("semantic_action_violates_story_item_custody:")
                for error in reviewed
            )
        )
        self.assertEqual(client.story_items[0]["holder"], "艾薇娅")

    def test_semantic_progress_rejects_operating_a_remote_location_before_moving(self) -> None:
        class ActorLocationReviewClient:
            def create_chat_completion(self, **kwargs):
                request = json.loads(kwargs["messages"][-1].content)
                self.actor_locations = request["actor_locations"]
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": True,
                        "repeats_micro_investigation_lane": False,
                        "responds_to_current_pressure_or_choice": True,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": True,
                        "opens_another_detail_layer": False,
                        "matches_prior_rejected_lane": False,
                        "reopens_exhausted_npc_knowledge_lane": False,
                        "new_public_evidence_reopens_npc_knowledge_lane": False,
                        "controls_other_player_characters": False,
                        "party_action_authorized_by_public_consensus": False,
                        "controls_npc_outcome_without_public_answer": False,
                        "npc_outcome_already_public": False,
                        "movement_claimed": True,
                        "movement_is_authorized_by_public_context": True,
                        "movement_authorization_evidence": "可以去旧路闸门",
                        "violates_story_item_custody": False,
                        "story_item_custody_evidence": "",
                        "acts_outside_authoritative_actor_location": True,
                        "actor_location_evidence": "苍祈=白花碑驿站·登记小室；操作地点=旧路闸门",
                        "repeats_resolved_information_delivery": False,
                        "evidence": "把路封嵌进旧路闸门",
                        "reason": "移动尚未由GM确认，不能同时完成目的地机关操作。",
                    },
                    ensure_ascii=False,
                )

        client = ActorLocationReviewClient()
        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=client,
            model="semantic-test",
        )
        step = ReplayStep(
            id="remote-location-operation",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室",
            scene_location="白花碑驿站·登记小室",
            known_pcs=["苍祈"],
            actor_locations={"苍祈": "白花碑驿站·登记小室"},
        )

        reviewed = simulator._review_action_progress_contract(
            "苍祈赶到旧路闸门，把路封嵌进门上的半环凹槽。",
            [],
            step=step,
            legal_context=context,
            recent_public_context="时悠：你可以离开登记小室，前往旧路闸门。",
        )

        self.assertTrue(
            any(
                error.startswith("semantic_action_acts_outside_actor_location:")
                for error in reviewed
            )
        )
        self.assertEqual(client.actor_locations["苍祈"], "白花碑驿站·登记小室")

    def test_action_slot_rejects_reopening_a_structured_settled_exchange(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="settled_scope_repeat",
            kind="game_turn",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["艾薇娅"],
            known_npcs=["灰金短斗篷的财团使者"],
            legal_actions=["互动", "调查"],
            settled_npc_exchanges=[
                {
                    "npc": "灰金短斗篷的财团使者",
                    "outcome": "accepted",
                    "settled_terms": "能当场说出已知去路且不再继续补全，财团使者就接受这次交换",
                    "player_performance": "complete",
                }
            ],
        )

        repeated = simulator.validate(
            "艾薇娅问灰金短斗篷的财团使者：我们要说到什么程度，才算今天这笔交换成立？",
            step=step,
            legal_context=context,
            recent_public_context="",
        )
        fresh = simulator.validate(
            "艾薇娅问灰金短斗篷的财团使者：你为什么宁愿接受这条残缺去路？",
            step=step,
            legal_context=context,
            recent_public_context="",
        )
        payout = simulator.validate(
            "艾薇娅问灰金短斗篷的财团使者：条件已经完成，你现在按约放行吗？",
            step=step,
            legal_context=context,
            recent_public_context="",
        )
        repeated_delivery = simulator.validate(
            "艾薇娅把那段已知去路再次完整复述给灰金短斗篷的财团使者。",
            step=step,
            legal_context=context,
            recent_public_context="",
        )
        context.settled_npc_exchanges[0]["player_performance"] = "pending"
        first_delivery = simulator.validate(
            "艾薇娅把那段已知去路完整复述给灰金短斗篷的财团使者。",
            step=step,
            legal_context=context,
            recent_public_context="",
        )

        self.assertIn("repeats_settled_npc_exchange", repeated)
        self.assertNotIn("repeats_settled_npc_exchange", fresh)
        self.assertNotIn("repeats_settled_npc_exchange", payout)
        self.assertIn("repeats_settled_npc_exchange", repeated_delivery)
        self.assertNotIn("repeats_settled_npc_exchange", first_delivery)

    def test_pending_accepted_exchange_is_exposed_as_a_legal_player_action(self) -> None:
        layer = LegalActionLayer()
        context = LegalActionContext(
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            known_pcs=["伊莉雅"],
            known_npcs=["财团使者"],
            legal_actions=["互动", "调查"],
            settled_npc_exchanges=[
                {
                    "npc": "财团使者",
                    "outcome": "accepted",
                    "settled_terms": "伊莉雅交出一段旧识，使者提供通行牌",
                    "player_performance": "pending",
                }
            ],
        )

        layer._append_pending_exchange_action(context)
        prompt = layer.as_prompt_block(context)

        self.assertIn("履行已接受的NPC交换", context.legal_actions)
        self.assertIn("玩家尚未实际履行", prompt)
        self.assertNotIn("接受并完成【伊莉雅交出一段旧识", prompt)

    def test_recent_npc_question_profile_recognizes_envoy_scope_paraphrases(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        recent = (
            "白河：洛岚问灰金短斗篷的财团使者，这段去路要说到什么程度才算谈话成立。"
        )

        assert simulator._repeats_recent_npc_question(
            "艾薇娅追问财团使者：按什么范围说，才算这笔交换成立？",
            recent,
        )

    def test_action_lane_ignores_abandoned_targets_before_a_fresh_pivot(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="fresh_pivot_after_clues",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃", "洛岚"],
            legal_actions=["调查", "互动"],
        )
        recent = (
            "阿凛：伊莉雅观察失名旅人掌心的刻痕。\n"
            "时悠：刻痕没有出血。\n"
            "白河：洛岚检查门槛与花瓣边缘的银灰硬屑。\n"
            "时悠：硬屑集中在门槛内侧。"
        )
        fresh_actions = (
            "赛璃不再靠近失名旅人或门槛，转向檐下白花风铃，查看系绳和铃架磨损。",
            "既然旅人已经退开，我不再碰花瓣或门槛，转而观察驿站内侧的旧路闸门。",
            "赛璃收起花瓣，不再靠近门槛或旅人，转身查看可供遮蔽的墙柱与门板。",
            "赛璃收起花瓣，转向失名旅人问：你还记得通往安全地点的声音或方向吗？",
        )

        for action in fresh_actions:
            with self.subTest(action=action):
                errors = simulator.validate(
                    action,
                    step=step,
                    legal_context=context,
                    recent_public_context=recent,
                )
                self.assertNotIn("repeats_recent_action_lane", errors)

    def test_same_test_pc_does_not_repeat_one_already_resolved_probe(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="same_pc_wind_chime_loop",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["洛岚"], legal_actions=["调查"])
        recent = (
            "白河：洛岚俯身检查风铃的接缝与磨损。\n"
            "时悠：接缝里卡着一缕蓝银盐丝，指向第三枚铆钉。\n"
            "南星：这个结果够具体了，我们换个方向。"
        )

        errors = simulator.validate(
            "洛岚把风铃翻过来，再观察接缝与磨损。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("repeats_recent_action_lane", errors)

    def test_repeat_guard_does_not_block_combat_attacks(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="combat_repeat",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            legal_actions=["攻击"],
            conflict_active=True,
            current_actor="洛岚",
        )
        recent = "白河：洛岚挥剑攻击财团机兵。\n时悠：剑锋在装甲上留下一道裂痕。"

        errors = simulator.validate(
            "洛岚继续挥剑攻击财团机兵。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("repeats_recent_action_lane", errors)

    def test_action_slot_must_answer_an_explicit_npc_invitation(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="follow_affordance",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"], legal_actions=["调查"])
        recent = "时悠：守门人推开侧门：‘条件已经做到。跟我来后院。’"

        ignored = simulator.validate(
            "赛璃继续检查门缝有没有透光。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        followed = simulator.validate(
            "赛璃点头跟上守门人，进入后院。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("ignores_explicit_gm_affordance", ignored)
        self.assertNotIn("ignores_explicit_gm_affordance", followed)

    def test_action_slot_follows_a_concrete_party_directive_before_new_small_talk(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="follow_concrete_party_directive",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["失忆旅人", "白花守望会会长"],
            legal_actions=["互动", "移动"],
        )
        recent = (
            "时悠：白花守望会会长抬起旧路图：‘先别站着听铃了。"
            "把失忆旅人往风铃外挪开一步，再把这段名字缺失的路指出来给我看。"
            "做到了，我现在就告诉你们下一段旧路怎么走。’"
        )

        ignored = simulator.validate(
            "伊莉雅看向失忆旅人：‘我是伊莉雅。’",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        followed = simulator.validate(
            "伊莉雅扶住失忆旅人，把失忆旅人往风铃外挪开一步。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )
        fallback = simulator._affordance_response_fallback(
            "伊莉雅",
            recent,
            known_npcs=context.known_npcs,
        )

        self.assertIn("ignores_explicit_gm_affordance", ignored)
        self.assertEqual([], followed)
        self.assertIn("失忆旅人", fallback)
        self.assertIn("风铃外", fallback)
        self.assertEqual(
            [],
            simulator.validate(
                fallback,
                step=step,
                legal_context=context,
                recent_public_context=recent,
            ),
        )

    def test_disclosure_offer_is_a_new_npc_affordance_after_saturated_questions(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="accept_partial_disclosure",
            kind="game_turn",
            speaker="澄砚",
            actor="苍祈",
            stage_goal=(
                "这是行动槽：必须提交当前角色的一个明确行动。"
                "本行动不得继续向NPC追问已经回答过的同一件事。"
            ),
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            known_npcs=["失名旅人"],
            legal_actions=["叙事行动"],
        )
        recent = (
            "时雨：艾薇娅问失名旅人愿不愿意说出那条去路。\n"
            "时悠：失名旅人说：‘我不能当众说完整名字，但我可以先说出还记得的那一小段方向。’"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply=recent,
            recent_public_context=recent,
        )

        self.assertEqual(result.validation_errors, [])
        self.assertIn("失名旅人", result.text)
        self.assertIn("愿意公开的那部分说完", result.text)

    def test_grounded_target_ignores_clock_and_opportunity_ui_entries(self) -> None:
        context = LegalActionContext(
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            known_npcs=["失名旅人"],
            visible_scene_elements=[
                "命刻【财团巡逻队逼近】4/8；自动推进：每轮一格",
                "机会效果【优势】",
            ],
        )

        target = ConstrainedPlayerSimulator._grounded_context_target(
            "时悠：失名旅人退到屋内墙后。",
            context,
        )

        self.assertEqual(target, "失名旅人")

    def test_table_discussion_slot_rejects_disguised_character_actions(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="table_only",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="你正在和其他玩家短暂商量，不要替角色声明行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"])

        for message in (
            "我来帮他挡掉周围那些杂音，你们先按住节奏。",
            "我先补位掩护一下，趁它失衡时找切入角度。",
            "那我先站到礼官旁边检查诊察签。",
            "赛璃靠近【那名失名旅人】仔细检查，用谨慎的方式观察局面。",
            "那就先把这枚签交给白穗对照名册吧，我更在意它对应的是谁。",
            "那我们先按白穗说的，把人从抽取车和风铃边上挪开。",
            "我有点在意刚空出来的托架位置，想先看看它有没有留下什么痕迹。",
            "我对风铃内侧的刻字拿不准，打算直接检查一下刮痕边缘。",
        ):
            with self.subTest(message=message):
                self.assertIn(
                    "table_discussion_declares_character_action",
                    simulator.validate(message, step=step, legal_context=context),
                )

        for proposal in (
            "我们要不要先别分散？我可以顺着旧路再盯一会儿。",
            "我可以先补个屏障，谁来配合压住门缝？",
            "赛璃可以负责观察旅人，谁来留意门外？",
        ):
            with self.subTest(proposal=proposal):
                self.assertNotIn(
                    "table_discussion_declares_character_action",
                    simulator.validate(proposal, step=step, legal_context=context),
                )

        errors = simulator.validate(
            "巡逻队越来越近了，谁盯外面，谁继续谈？",
            step=step,
            legal_context=context,
        )
        self.assertNotIn("table_discussion_declares_character_action", errors)

    def test_table_discussion_rejects_collective_actions_disguised_as_planning(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="collective_action_in_table_talk",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="你正在和其他玩家短暂商量，不要替角色声明行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"])

        for message in (
            "那我们先别挤着过门，谁来照看旅人？",
            "咱们继续跟着队伍往前走，我比较在意前面会不会还有卡口。",
            "那就先别卡在门口了，咱们一边跟上一边盯着巡逻者。",
            "那就贴着内侧走，别碰外沿；我更在意这段路会不会把我们带到更容易被看见的地方。",
        ):
            with self.subTest(message=message):
                self.assertIn(
                    "table_discussion_declares_character_action",
                    simulator.validate(message, step=step, legal_context=context),
                )

        for text in (
            "我们要不要先通过这道门？谁愿意留在最后照应旅人？",
            "我倾向先贴着内侧稳一点，别急着踩外沿。",
        ):
            with self.subTest(text=text):
                proposal = simulator.validate(
                    text,
                    step=step,
                    legal_context=context,
                )
                self.assertNotIn("table_discussion_declares_character_action", proposal)

    def test_semantic_table_discussion_review_repairs_a_conditional_action(self) -> None:
        class TableDiscussionReviewClient:
            def __init__(self) -> None:
                self.generations = 0

            def create_chat_completion(self, **kwargs):
                operation = str(kwargs.get("operation") or "")
                if operation == "fu_pl.table_discussion_contract":
                    message = kwargs["messages"][-1]
                    content = (
                        message.get("content")
                        if isinstance(message, dict)
                        else message.content
                    )
                    if isinstance(content, list):
                        content = "".join(
                            str(item.get("text") or "")
                            for item in content
                            if isinstance(item, dict)
                        )
                    request = json.loads(content)
                    candidate = str(request.get("candidate") or "")
                    if "先看巡守到底是不是已经接上手" in candidate:
                        return json.dumps(
                            {
                                "pure_table_discussion": False,
                                "commits_character_action": True,
                                "commits_party_action": False,
                                "directed_at_gm_or_npc": False,
                                "evidence": "先看巡守到底是不是已经接上手",
                                "reason": "赛璃已经开始观察巡守交接。",
                            },
                            ensure_ascii=False,
                        )
                    return json.dumps(
                        {
                            "pure_table_discussion": True,
                            "commits_character_action": False,
                            "commits_party_action": False,
                            "directed_at_gm_or_npc": False,
                            "evidence": "谁方便盯一下巡守的交接",
                            "reason": "只是在询问分工。",
                        },
                        ensure_ascii=False,
                    )
                self.generations += 1
                if self.generations == 1:
                    return (
                        "那我先别再催这边的位子了，先看巡守到底是不是已经接上手；"
                        "如果还没接稳，咱们就先把这里的秩序守住。"
                    )
                return "谁方便盯一下巡守的交接？我倾向先把这里的秩序稳住。"

        client = TableDiscussionReviewClient()
        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=client,
            model="gpt-5.6-luna",
        )
        step = ReplayStep(
            id="semantic-table-only",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="你正在和其他玩家短暂商量，不要替角色声明行动。",
        )
        utterance = simulator.compose(
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["赛璃"],
            ),
            last_gm_reply="巡守还在交接，门外也有脚步声。",
            recent_public_context="时悠：巡守还在交接，门外也有脚步声。",
        )

        self.assertEqual(
            utterance.text,
            "谁方便盯一下巡守的交接？我倾向先把这里的秩序稳住。",
        )
        self.assertGreaterEqual(client.generations, 2)
        self.assertEqual(utterance.validation_errors, [])

    def test_player_action_cannot_directly_address_a_known_but_absent_npc(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="absent-npc",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        errors = simulator.validate(
            "赛璃转向白花守望会会长，询问她是否愿意开放旧路。",
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["赛璃"],
                known_npcs=["白花守望会会长", "巡守"],
                present_npcs=["巡守"],
                presence_authoritative=True,
                legal_actions=["普通叙事行动", "社交交涉"],
            ),
            recent_public_context="时悠：会长留在驿站，巡守带着你们进入旧路。",
        )

        self.assertTrue(
            any(error.startswith("action_slot_addresses_absent_npc:") for error in errors)
        )

    def test_player_prompt_includes_the_step_specific_task(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="task_prompt",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"], legal_actions=["调查"])

        prompt = simulator._build_prompt(step, context, "门外传来脚步声。")

        self.assertIn("本条发言任务", prompt)
        self.assertIn("必须提交当前角色的一个明确行动", prompt)

    def test_table_discussion_cannot_consume_opportunity_or_declare_an_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="table_only",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="你正在和其他玩家短暂商量；不要替角色声明行动。",
        )
        context = LegalActionContext(stage_goal=step.stage_goal, known_pcs=["赛璃"])

        opportunity_errors = simulator.validate(
            "我先把这个揭示机会留着，看看谁适合接下一步。",
            step=step,
            legal_context=context,
        )
        stale_opportunity_errors = simulator.validate(
            "我先记着这个机会，等大家统一一下再决定要不要现在用。",
            step=step,
            legal_context=context,
        )
        consumed_opportunity_errors = simulator.validate(
            "既然机会已经记录了，我们先想想要不要把它留到更关键的时候用。",
            step=step,
            legal_context=context,
        )
        legitimate_reveal_discussion = simulator.validate(
            "这次揭示补出的赤羽纹样很关键，咱们先商量它和门外压力哪个更急。",
            step=step,
            legal_context=context,
        )
        action_errors = simulator.validate(
            "赛璃先处理仪式命刻，确认情况后再行动。",
            step=step,
            legal_context=context,
        )
        discussion_errors = simulator.validate(
            "那我们先别散开，谁方便盯住门外？",
            step=step,
            legal_context=context,
        )

        self.assertIn("table_discussion_resolves_pending_opportunity", opportunity_errors)
        self.assertIn("table_discussion_resolves_pending_opportunity", stale_opportunity_errors)
        self.assertIn("table_discussion_resolves_pending_opportunity", consumed_opportunity_errors)
        self.assertEqual(legitimate_reveal_discussion, [])
        self.assertIn("table_discussion_declares_character_action", action_errors)
        self.assertEqual(discussion_errors, [])

    def test_action_slot_cannot_act_on_an_undefined_placeholder_object(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="undefined_object",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            legal_actions=["互动"],
        )

        vague = simulator.validate(
            "洛岚拿出最能稳住局面的东西递给会长。",
            step=step,
            legal_context=context,
        )
        clarification = simulator.validate(
            "洛岚问会长：你说的担保具体要什么？",
            step=step,
            legal_context=context,
        )
        concrete = simulator.validate(
            "洛岚递出刚才公开提到的铜质通行牌，请会长核验背面编号。",
            step=step,
            legal_context=context,
        )

        self.assertIn("action_slot_acts_on_undefined_object", vague)
        self.assertNotIn("action_slot_acts_on_undefined_object", clarification)
        self.assertNotIn("action_slot_acts_on_undefined_object", concrete)

    def test_player_fallback_prefers_open_npc_condition_over_repeating_investigation(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="condition_action",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：回应当前局面。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            legal_actions=["普通叙事行动", "调查"],
            open_npc_conditions=[
                {
                    "condition_id": "c1",
                    "npc": "艾蕾娜",
                    "condition": "把碎月遗物放入可封存容器，并把失忆旅人带到记录点",
                    "promised_result": "发放临时放行纸",
                }
            ],
        )

        utterance = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="艾蕾娜已经把条件说清楚。",
            recent_public_context=(
                "白河: 洛岚检查风铃接缝。\n"
                "时悠: 接缝里有灰银粉。\n"
                "阿凛: 伊莉雅又检查风铃刻痕。\n"
                "时悠: 刻痕来自人为刮磨。"
            ),
        )

        self.assertIn("碎月遗物", utterance.text)
        self.assertIn("失忆旅人", utterance.text)
        self.assertEqual(utterance.validation_errors, [])

    def test_player_cannot_handle_a_remote_archive_as_if_it_were_present(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="remote_archive",
            kind="game_turn",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["赛璃"],
            visible_scene_elements=["风铃侧室的金属签角", "桌上的封存标签"],
            legal_actions=["调查"],
        )
        public_context = "时悠: 艾蕾娜说，旧档以后可以去财团档案库查。"

        errors = simulator.validate(
            "赛璃翻开旧账册，把姓名编号和金属签角对照。",
            step=step,
            legal_context=context,
            recent_public_context=public_context,
        )

        self.assertIn("action_slot_acts_on_undefined_object", errors)

    def test_npc_question_and_table_plan_do_not_consume_an_investigation_lane(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="first_real_probe",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            legal_actions=["调查"],
        )
        recent = (
            "阿凛: 岑曜，我想问清楚你要我们先向谁说清楚？\n"
            "时悠: 先核验风铃、黑字和旅人的接触。\n"
            "南星: 岑曜，你能不能先告诉我守望会有没有类似记录？\n"
            "时悠: 有，守望会记录过一次迟拍异响。\n"
            "南星: 我觉得咱们最好分开盯风铃、黑字和旅人。"
        )

        errors = simulator.validate(
            "洛岚走到白花风铃旁，检查铃身裂缝和内侧黑字的刮磨边缘。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertNotIn("repeats_recent_action_lane", errors)

    def test_validated_action_fallback_can_turn_to_an_active_threat(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="fresh_clock_lane",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃"],
            active_clocks=["财团巡逻队逼近"],
            visible_scene_elements=["门外泥地上的财团车辙"],
            legal_actions=["调查", "推进目标"],
        )
        recent = (
            "伊莉雅：伊莉雅检查黑蜡风铃的刻痕。\n"
            "时悠：刻痕里残留着铅屑。\n"
            "赛璃：赛璃继续观察黑蜡风铃和风铃架。\n"
            "时悠：远处已经能听见巡逻队的金属脚步。"
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "阿凛: 伊莉雅继续检查黑蜡风铃。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
            last_gm_reply="远处已经能听见巡逻队的金属脚步。",
        )

        self.assertEqual(errors, [])
        self.assertIn("财团车辙", fallback)
        self.assertNotIn("命刻", fallback)

    def test_validated_action_fallback_prefers_latest_explicit_gm_focus(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="latest-focus-over-stale-object",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚", "伊莉雅"],
            known_npcs=["白花守望会会长"],
            visible_scene_elements=["碎月遗物", "银白铃舌", "登记小室"],
            legal_actions=["调查", "普通叙事行动"],
        )
        recent = (
            "白河：洛岚贴住门框听外面的脚步方位。\n"
            "时悠：右侧廊道传来脚步，银白铃舌露出新的刻痕。\n"
            "时悠：先看碎月遗物。它更容易把记忆缝隙放大；"
            "白花风铃已经在失真，继续先盯它，最容易把失忆旅人带偏。"
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "白河：洛岚继续守住门框。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
            last_gm_reply="先看碎月遗物。",
        )

        self.assertEqual(errors, [])
        self.assertIn("碎月遗物", fallback)
        self.assertNotIn("门框", fallback)
        self.assertNotIn("命刻", fallback)

    def test_player_cannot_treat_opportunity_effect_as_a_scene_object(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="mechanical_effect_is_not_object",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["白守成"],
            visible_scene_elements=["半掩的旧路闸门"],
            active_clocks=["【财团巡逻队逼近】7/8"],
            legal_actions=["普通叙事行动", "调查"],
        )

        effect_errors = simulator.validate(
            "白河: 洛岚靠近【优势】仔细检查，用谨慎的方式观察局面。",
            step=step,
            legal_context=context,
            recent_public_context="时悠: 机会【优势】：赛璃的下一次相关检定获得+4修正。",
        )
        clock_errors = simulator.validate(
            "白河: 洛岚走到【财团巡逻队逼近】旁仔细检查。",
            step=step,
            legal_context=context,
            recent_public_context="时悠: 【财团巡逻队逼近】7/8。",
        )

        self.assertIn("action_slot_targets_mechanical_label", effect_errors)
        self.assertIn("action_slot_targets_mechanical_label", clock_errors)

    def test_player_cannot_turn_a_table_question_fragment_into_a_scene_entity(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="discussion_fragment_target",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["白花守望会会长"],
            visible_scene_elements=["白花风铃"],
            legal_actions=["普通叙事行动", "调查", "社交交涉"],
        )
        recent = (
            "时悠: 白花守望会会长站在白花风铃下，等你们说明来意。\n"
            "南星: 我倾向于先确认安全地点；至于铃声的缘由，谁有把握向会长解释？"
        )

        errors = simulator.validate(
            "阿凛: 伊莉雅靠近【谁有把握向会长】仔细检查，用谨慎的方式观察局面。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
        )

        self.assertIn("action_slot_targets_discussion_fragment", errors)

    def test_player_cannot_invent_a_bracketed_scene_entity(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="invented_bracket_target",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            visible_scene_elements=["白花风铃"],
            legal_actions=["调查"],
        )

        errors = simulator.validate(
            "阿凛: 伊莉雅走向【灰烬王座】，检查王座后的机关。",
            step=step,
            legal_context=context,
            recent_public_context="时悠: 廊下只有一串白花风铃，四周没有别的陈设。",
        )

        self.assertIn("action_slot_targets_unestablished_entity", errors)

    def test_player_model_repairs_a_discussion_fragment_target(self) -> None:
        class RepairingClient:
            def __init__(self) -> None:
                self.outputs = [
                    "阿凛: 伊莉雅靠近【谁有把握向会长】仔细检查。",
                    "阿凛: 伊莉雅走到白花风铃下，检查铃身是否有新留下的擦痕。",
                ]

            def create_chat_completion(self, **_kwargs):
                return self.outputs.pop(0)

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=RepairingClient(),
            model="fake-player-model",
        )
        step = ReplayStep(
            id="repair_discussion_fragment_target",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            known_npcs=["白花守望会会长"],
            visible_scene_elements=["白花风铃"],
            legal_actions=["普通叙事行动", "调查"],
        )
        recent = (
            "时悠: 白花守望会会长站在白花风铃下。\n"
            "南星: 铃声的缘由，谁有把握向会长解释？"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="白花守望会会长站在白花风铃下。",
            recent_public_context=recent,
        )

        self.assertIn("白花风铃", result.text)
        self.assertNotIn("谁有把握", result.text)
        self.assertFalse(result.used_fallback)
        self.assertIn(
            "action_slot_targets_discussion_fragment",
            result.model_attempts[0]["validation_errors"],
        )
        self.assertEqual(result.model_attempts[1]["validation_errors"], [])

    def test_player_model_repairs_until_it_directly_answers_pending_npc_request(self) -> None:
        class ContractReviewClient:
            def __init__(self) -> None:
                self.outputs = [
                    "阿凛: 伊莉雅对赛璃说：我们先商量一下该不该把路线告诉门外的人。",
                    (
                        '{"directed_to_requester":false,"answered_item_ids":[],"complete":false,'
                        '"evidence":"我们先商量一下","reason":"只在和队友商量，没有回应发问者"}'
                    ),
                    "阿凛: 伊莉雅朝门外回答：我们从白花碑驿站来；同行的是一名失忆旅人。",
                    (
                        '{"directed_to_requester":true,'
                        '"answered_item_ids":["origin","companion"],"complete":true,'
                        '"evidence":"伊莉雅朝门外回答","reason":"已直接逐项作答"}'
                    ),
                ]

            def create_chat_completion(self, **_kwargs):
                return self.outputs.pop(0)

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ContractReviewClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="answer_pending_npc_request",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：直接回应门外发问者。",
            payload={
                "npc_response_contract": {
                    "npc": "未具名发问者",
                    "summary": "交代来路与同行者",
                    "remaining_items": [
                        {"item_id": "origin", "prompt": "说明来路"},
                        {"item_id": "companion", "prompt": "说明同行者"},
                    ],
                    "speaker_evidence": "门外传来询问来路与同行者的声音。",
                }
            },
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅", "赛璃"],
            visible_scene_elements=["门外发问者"],
            legal_actions=["普通叙事行动"],
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="门外传来声音，仍在等你们回答。",
            recent_public_context="时悠：门外传来询问来路与同行者的声音。",
        )

        self.assertFalse(result.used_fallback)
        self.assertIn("朝门外回答", result.text)
        self.assertEqual(len(result.model_attempts), 2)
        self.assertTrue(
            any(
                str(error).startswith("does_not_answer_pending_npc_request:")
                for error in result.model_attempts[0]["validation_errors"]
            )
        )
        self.assertEqual(result.model_attempts[1]["validation_errors"], [])

    def test_player_model_repairs_a_clue_recap_disguised_as_an_action(self) -> None:
        class ProgressReviewClient:
            def __init__(self) -> None:
                self.outputs = [
                    (
                        "时雨: 艾薇娅把白蜡封片、铜线压痕和旧登记册的发现完整复述一遍，"
                        "随后站到柱影边缘继续观察。"
                    ),
                    (
                        '{"valid_action_progress":false,'
                        '"mostly_restates_known_information":true,'
                        '"repeats_completed_action":false,"concrete_new_action":false,'
                        '"grounded_in_public_context":true,'
                        '"evidence":"把白蜡封片、铜线压痕和旧登记册的发现完整复述一遍",'
                        '"reason":"主体是在复述已知线索，末尾站位没有新目的"}'
                    ),
                    "时雨: 艾薇娅走到刚到门外的巡骑与驿站门槛之间，摊开双手挡住他们的去路。",
                    (
                        '{"valid_action_progress":true,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":false,"concrete_new_action":true,'
                        '"grounded_in_public_context":true,'
                        '"evidence":"刚到门外的巡骑",'
                        '"reason":"回应最新到场者并采取新的阻挡行动"}'
                    ),
                ]

            def create_chat_completion(self, **_kwargs):
                return self.outputs.pop(0)

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ProgressReviewClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="reject_clue_recap_action",
            kind="player_message",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["艾薇娅"],
            known_npcs=["财团巡骑"],
            visible_scene_elements=["驿站门口", "刚到门外的巡骑"],
            legal_actions=["普通叙事行动", "交涉"],
        )
        recent = (
            "时悠：白蜡封片来自旧登记册，铜线压痕指向驿站后门。\n"
            "时悠：两名财团巡骑已经停在驿站门外，手还按在缰绳上。"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="两名财团巡骑已经停在驿站门外，手还按在缰绳上。",
            recent_public_context=recent,
        )

        self.assertFalse(result.used_fallback)
        self.assertIn("挡住他们的去路", result.text)
        self.assertEqual(len(result.model_attempts), 2)
        self.assertTrue(
            any(
                str(error).startswith("semantic_action_without_progress:")
                for error in result.model_attempts[0]["validation_errors"]
            )
        )
        self.assertEqual(result.model_attempts[1]["validation_errors"], [])

    def test_semantic_progress_review_overrules_false_affordance_on_npc_uncertainty(self) -> None:
        class PressureResponseClient:
            def __init__(self) -> None:
                self.outputs = [
                    (
                        "澄砚: 苍祈侧身贴进石柱背后的阴影，压低身形避开旧路入口的视线，"
                        "暂时不惊动重新对准这里的巡逻队，等它们经过。"
                    ),
                    (
                        '{"valid_action_progress":true,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":false,"concrete_new_action":true,'
                        '"grounded_in_public_context":true,'
                        '"materially_advances_current_situation":true,'
                        '"repeats_micro_investigation_lane":false,'
                        '"responds_to_current_pressure_or_choice":true,'
                        '"actionable_result_or_explicit_choice_is_already_public":false,'
                        '"uses_public_result_or_answers_choice":true,'
                        '"opens_another_detail_layer":false,'
                        '"matches_prior_rejected_lane":false,'
                        '"evidence":"压低身形避开旧路入口的视线",'
                        '"reason":"角色正以隐蔽和等待回应巡逻队逼近的公开压力"}'
                    ),
                ]

            def create_chat_completion(self, **_kwargs):
                return self.outputs.pop(0)

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=PressureResponseClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="npc_uncertainty_is_not_forced_choice",
            kind="player_message",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["苍祈"],
            known_npcs=["失忆旅人"],
            visible_scene_elements=["石柱", "旧路入口", "财团巡逻队"],
            legal_actions=["普通叙事行动", "防御"],
        )
        latest = (
            "我不知道是谁把我送来的，也不知道自己在追寻谁。"
            "我不能确认是被人护送，还是自己走来的。"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply=latest,
            recent_public_context=(
                "时悠：远处巡逻队的脚步声已重新对准这处入口。\n"
                f"时悠：{latest}"
            ),
        )

        self.assertFalse(result.used_fallback)
        self.assertIn("贴进石柱背后的阴影", result.text)
        self.assertEqual(result.model_attempts[0]["validation_errors"], [])

    def test_natural_concealment_is_a_world_facing_action(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="natural_concealment_action",
            kind="player_message",
            speaker="澄砚",
            actor="苍祈",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        errors = simulator.validate(
            "苍祈侧身贴进石柱背后的阴影，压低身形等巡逻队经过。",
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["苍祈"],
                visible_scene_elements=["石柱", "巡逻队"],
            ),
            recent_public_context="时悠：巡逻队的脚步声正对准旧路入口。",
        )

        self.assertNotIn("action_slot_contains_only_table_discussion", errors)

    def test_player_model_cannot_reverse_a_rejected_party_transition_in_same_context(self) -> None:
        class PartyTransitionReviewClient:
            def __init__(self) -> None:
                self.outputs = [
                    "南星: 赛璃跟上众人，进入登记小室。",
                    (
                        '{"valid_action_progress":false,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":true,"concrete_new_action":false,'
                        '"grounded_in_public_context":true,'
                        '"materially_advances_current_situation":false,'
                        '"matches_prior_rejected_lane":false,'
                        '"evidence":"队伍已经进入登记小室",'
                        '"reason":"GM已经宣布队伍整体完成转场"}'
                    ),
                    "南星: 赛璃也踏进登记小室，在众人身后停下。",
                    (
                        '{"valid_action_progress":true,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":false,"concrete_new_action":true,'
                        '"grounded_in_public_context":true,'
                        '"materially_advances_current_situation":true,'
                        '"matches_prior_rejected_lane":true,'
                        '"evidence":"踏进登记小室",'
                        '"reason":"措辞改变，但仍属于刚才已拒绝的重复转场"}'
                    ),
                    "南星: 赛璃走到登记台侧面，查看哪本册子刚被人抽走过。",
                    (
                        '{"valid_action_progress":true,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":false,"concrete_new_action":true,'
                        '"grounded_in_public_context":true,'
                        '"materially_advances_current_situation":true,'
                        '"matches_prior_rejected_lane":false,'
                        '"evidence":"登记台",'
                        '"reason":"在新地点采取了不同的新行动"}'
                    ),
                ]
                self.review_requests: list[dict[str, object]] = []

            def create_chat_completion(self, **kwargs):
                if kwargs.get("operation") == "fu_pl.action_progress_contract":
                    self.review_requests.append(
                        json.loads(kwargs["messages"][-1].content)
                    )
                return self.outputs.pop(0)

        client = PartyTransitionReviewClient()
        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=client,
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="party_transition_already_complete",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            scene_name="登记小室",
            scene_location="登记小室",
            known_pcs=["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"],
            visible_scene_elements=["登记台", "旧册子"],
            legal_actions=["调查", "互动"],
        )
        recent = (
            "时悠：会长推开侧门，队伍带着失忆旅人进入登记小室。\n"
            "时悠：登记台上摊着三本受潮的旧册子。"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="登记台上摊着三本受潮的旧册子。",
            recent_public_context=recent,
        )

        self.assertFalse(result.used_fallback)
        self.assertIn("查看哪本册子", result.text)
        self.assertEqual(len(result.model_attempts), 3)
        self.assertTrue(client.review_requests[1]["prior_rejected_action_attempts"])
        self.assertTrue(
            any(
                "prior_lane=True" in str(error)
                for error in result.model_attempts[1]["validation_errors"]
            )
        )

    def test_action_progress_review_does_not_let_imperfect_evidence_override_valid_semantics(self) -> None:
        class ReviewClient:
            def create_chat_completion(self, **_kwargs):
                return (
                    '{"valid_action_progress":true,'
                    '"mostly_restates_known_information":false,'
                    '"repeats_completed_action":false,"concrete_new_action":true,'
                    '"grounded_in_public_context":true,'
                    '"evidence":"检修门后的新阶梯",'
                    '"reason":"角色正在进入GM刚公开的新通道"}'
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ReviewClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="valid_progress_imperfect_evidence",
            kind="player_message",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        candidate = "艾薇娅取下蓝玻璃提灯，踏上检修门后的第一阶。"

        errors = simulator._review_action_progress_contract(
            candidate,
            [],
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["艾薇娅"],
                visible_scene_elements=["蓝玻璃提灯", "检修门", "黑暗阶梯"],
            ),
            recent_public_context="石灰封住的检修门向内弹开，露出向下的黑暗阶梯。",
        )

        self.assertEqual(errors, [])
        self.assertFalse(simulator.last_action_progress_review["evidence_is_verbatim"])

    def test_action_progress_review_rejects_nested_micro_investigation(self) -> None:
        class ReviewClient:
            def create_chat_completion(self, **_kwargs):
                return (
                    '{"valid_action_progress":true,'
                    '"mostly_restates_known_information":false,'
                    '"repeats_completed_action":false,"concrete_new_action":true,'
                    '"grounded_in_public_context":true,'
                    '"materially_advances_current_situation":false,'
                    '"repeats_micro_investigation_lane":true,'
                    '"responds_to_current_pressure_or_choice":false,'
                    '"actionable_result_or_explicit_choice_is_already_public":true,'
                    '"uses_public_result_or_answers_choice":false,'
                    '"opens_another_detail_layer":true,'
                    '"evidence":"检查铜片背面的第二道划痕",'
                    '"reason":"只把刚揭示的细节继续拆小，没有改变眼前选择"}'
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ReviewClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="reject_nested_micro_investigation",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            payload={
                "dramatic_progress_context": {
                    "unresolved_now": "守望会仍未决定是否开放旧路",
                    "next_gm_need": "需要有人回应守望会的条件",
                }
            },
        )
        candidate = "洛岚检查铜片背面的第二道划痕，看看里面是否还藏着更小的暗号。"

        errors = simulator._review_action_progress_contract(
            candidate,
            [],
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["洛岚"],
                visible_scene_elements=["刚从闸门槽里掉出的铜片"],
            ),
            recent_public_context=(
                "时悠：闸门槽里掉出一枚铜片，划痕只说明它属于守望会旧路钥匙。"
                "守望会会长仍在等你们决定是否接受担保条件。"
            ),
        )

        self.assertTrue(
            any(error.startswith("semantic_action_without_progress:") for error in errors)
        )
        self.assertTrue(simulator.last_action_progress_review["repeats_micro_investigation_lane"])

    def test_action_progress_review_rejects_new_detail_after_actionable_choice(self) -> None:
        class ReviewClient:
            def create_chat_completion(self, **_kwargs):
                return (
                    '{"valid_action_progress":true,'
                    '"mostly_restates_known_information":false,'
                    '"repeats_completed_action":false,"concrete_new_action":true,'
                    '"grounded_in_public_context":true,'
                    '"materially_advances_current_situation":true,'
                    '"repeats_micro_investigation_lane":false,'
                    '"responds_to_current_pressure_or_choice":false,'
                    '"actionable_result_or_explicit_choice_is_already_public":true,'
                    '"uses_public_result_or_answers_choice":false,'
                    '"opens_another_detail_layer":true,'
                    '"evidence":"取一点灰白粉末继续检验",'
                    '"reason":"旧路已经放行且巡逻灯逼近，继续拆查粉末没有回应当前取舍"}'
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ReviewClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="reject_detail_after_actionable_choice",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        errors = simulator._review_action_progress_contract(
            "洛岚取一点灰白粉末继续检验颗粒。",
            [],
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["洛岚"],
                visible_scene_elements=["旧路阶梯", "灰白粉末", "财团探灯"],
            ),
            recent_public_context=(
                "时悠：旧路第一阶已经放行。阶梯边有灰白粉末。"
                "门缝外的财团探灯已经停在入口。"
            ),
        )

        self.assertTrue(
            any(error.startswith("semantic_action_without_progress:") for error in errors)
        )
        self.assertIn("stalls_after_actionable=True", errors[-1])

    def test_action_progress_review_rejects_resource_spend_without_public_tactical_basis(self) -> None:
        class ReviewClient:
            def create_chat_completion(self, **_kwargs):
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": False,
                        "responds_to_current_pressure_or_choice": False,
                        "procedural_micro_clarification_after_sufficient_plan": False,
                        "spends_limited_resource_without_public_tactical_basis": True,
                        "resource_tactical_basis_evidence": "",
                        "evidence": "施放元素幕障，选择风系",
                        "reason": "公开局面没有风系危险、受伤目标或需要风系抗性的战术计划。",
                    },
                    ensure_ascii=False,
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ReviewClient(),
            model="semantic-test",
        )
        step = ReplayStep(
            id="unsupported-resource-spend",
            kind="game_turn",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        errors = simulator._review_action_progress_contract(
            "伊莉雅对苍祈施放元素幕障，选择风系。",
            [],
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["伊莉雅", "苍祈"],
                legal_actions=["施法"],
                legal_spells=["元素幕障"],
            ),
            recent_public_context="时悠：守望会已经允许队伍排演旧路撤离路线。",
        )

        self.assertTrue(
            any(
                error.startswith("semantic_action_spends_resource_without_public_basis:")
                for error in errors
            )
        )

    def test_action_progress_review_rejects_unneeded_procedural_micro_clarification(self) -> None:
        class ReviewClient:
            def create_chat_completion(self, **_kwargs):
                return json.dumps(
                    {
                        "valid_action_progress": True,
                        "mostly_restates_known_information": False,
                        "repeats_completed_action": False,
                        "concrete_new_action": True,
                        "grounded_in_public_context": True,
                        "materially_advances_current_situation": False,
                        "responds_to_current_pressure_or_choice": False,
                        "actionable_result_or_explicit_choice_is_already_public": True,
                        "uses_public_result_or_answers_choice": False,
                        "opens_another_detail_layer": True,
                        "procedural_micro_clarification_after_sufficient_plan": True,
                        "spends_limited_resource_without_public_tactical_basis": False,
                        "resource_tactical_basis_evidence": "",
                        "evidence": "触发回白绳时要把旅人交给谁",
                        "reason": "路线、看护者和回撤信号均已公开，答案不会改变当前排演方法。",
                    },
                    ensure_ascii=False,
                )

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ReviewClient(),
            model="semantic-test",
        )
        step = ReplayStep(
            id="procedural-micro-clarification",
            kind="game_turn",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        errors = simulator._review_action_progress_contract(
            "洛岚再问会长：触发回白绳时要把旅人交给谁？",
            [],
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["洛岚", "艾薇娅", "苍祈"],
                known_npcs=["白花守望会会长", "失忆旅人"],
                legal_actions=["互动"],
            ),
            recent_public_context=(
                "时悠：艾薇娅护送失忆旅人，洛岚在前确认路线，苍祈守在闸门旁传递回白绳信号；"
                "信号响起就沿原路退回风铃廊。"
            ),
        )

        self.assertTrue(
            any(
                error.startswith("semantic_action_stalls_on_procedural_detail:")
                for error in errors
            )
        )

    def test_player_prompts_require_contextual_resource_use_and_execution_over_protocol(self) -> None:
        prompt = ConstrainedPlayerSimulator(use_llm=False)._system_prompt()
        self.assertIn("不要仅因为某个法术或技能在角色卡上合法就消耗有限资源", prompt)
        self.assertIn("不要继续追问不会改变路线、风险、职责或行动方式", prompt)
        self.assertIn(
            "spends_limited_resource_without_public_tactical_basis",
            PLAYER_ACTION_PROGRESS_REVIEW_PROMPT,
        )
        self.assertIn(
            "procedural_micro_clarification_after_sufficient_plan",
            PLAYER_ACTION_PROGRESS_REVIEW_PROMPT,
        )

    def test_fallback_prefers_grounded_scene_entity_after_opportunity_effect(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="grounded_target_after_opportunity",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
            intent="调查",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["白守成"],
            visible_scene_elements=["半掩的旧路闸门"],
            legal_actions=["调查"],
        )
        recent = (
            "时悠: 白守成站在半掩的旧路闸门旁。\n"
            "南星: 我把这次机会用于【优势】。\n"
            "时悠: 机会【优势】：赛璃的下一次相关检定获得+4修正。"
        )

        fallback = simulator._fallback_utterance(
            step,
            context,
            recent_public_context=recent,
            last_gm_reply="机会【优势】：赛璃的下一次相关检定获得+4修正。",
        )

        self.assertNotIn("【优势】", fallback)
        self.assertIn("半掩的旧路闸门", fallback)

    def test_validated_action_fallback_finishes_an_npc_disclosure_promise(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="finish_disclosure",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["洛岚"],
            known_npcs=["阿鸣", "希缇"],
            visible_scene_elements=["桌上的回执", "盐木盒", "白花风铃"],
            legal_actions=["互动", "调查"],
        )
        recent = (
            "洛岚：洛岚贴住门口听外面的脚步方位。\n"
            "时悠：门外脚步仍在逼近。\n"
            "赛璃：赛璃直接问阿鸣一句：那页回执里和盐木盒、风铃有关的部分，"
            "你现在愿不愿意当着希缇说出来？\n"
            "时悠：愿意，我只说我当场能确认、和盐木盒、风铃直接有关的那部分；"
            "整页回执我不原样念出来。"
        )

        fallback, errors = simulator._validated_fallback_utterance(
            "白河: 洛岚沿风铃廊走到转折处查看道路。",
            step=step,
            legal_context=context,
            recent_public_context=recent,
            last_gm_reply=(
                "愿意，我只说我当场能确认、和盐木盒、风铃直接有关的那部分；"
                "整页回执我不原样念出来。"
            ),
        )

        self.assertEqual(errors, [])
        self.assertIn("请他现在把愿意公开的那部分说完", fallback)
        self.assertIn("阿鸣", fallback)

    def test_clock_fallback_does_not_invent_a_route_or_device(self) -> None:
        self.assertEqual(
            ConstrainedPlayerSimulator._clock_method(
                "财团巡逻队逼近",
                "远处传来铁靴声，车厢旁摆着记忆罐。",
            ),
            "",
        )

    def test_generic_investigation_fallback_never_treats_clock_as_physical_target(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="investigate_without_object",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：伊莉雅调查周边环境。",
            intent="调查",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            active_clocks=["[财团巡逻队逼近] 2/6"],
            legal_actions=["调查", "推进目标"],
        )

        fallback = simulator._fallback_utterance(
            step,
            context,
            last_gm_reply="远处传来金属脚步，门廊里的人都停下来听。",
        )

        self.assertIn("环顾周围", fallback)
        self.assertNotIn("【财团巡逻队逼近】", fallback)
        self.assertNotIn("靠近【", fallback)

    def test_accepted_fallback_does_not_inherit_model_attempt_errors(self) -> None:
        class AlwaysRepeatsClient:
            def create_chat_completion(self, **_kwargs):
                return "伊莉雅继续检查黑蜡风铃。"

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=AlwaysRepeatsClient(),
            model="fake",
            continue_on_invalid=True,
        )
        step = ReplayStep(
            id="fallback_metrics",
            kind="player_message",
            speaker="阿凛",
            actor="伊莉雅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        context = LegalActionContext(
            stage_goal=step.stage_goal,
            known_pcs=["伊莉雅"],
            active_clocks=["财团巡逻队逼近"],
        )
        recent = (
            "伊莉雅：伊莉雅检查黑蜡风铃。\n"
            "时悠：刻痕里残留着铅屑。\n"
            "赛璃：赛璃继续观察黑蜡风铃和风铃架。\n"
            "时悠：远处已经能听见巡逻队的金属脚步。"
        )

        result = simulator.compose(
            step=step,
            legal_context=context,
            last_gm_reply="远处已经能听见巡逻队的金属脚步。",
            recent_public_context=recent,
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.validation_errors, [])
        self.assertTrue(result.model_attempts)
        self.assertIn("保持警戒", result.text)
        self.assertNotIn("岔路", result.text)

    def test_latest_gm_reply_is_authoritative_for_player_semantic_review(self) -> None:
        class LatestReplyClient:
            def create_chat_completion(self, **kwargs):
                if kwargs.get("operation") == "fu_pl.action_progress_contract":
                    request = json.loads(kwargs["messages"][-1].content)
                    assert "黄铜分路片" in request["recent_public_context"]
                    return (
                        '{"valid_action_progress":true,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":false,"concrete_new_action":true,'
                        '"grounded_in_public_context":true,'
                        '"evidence":"黄铜分路片",'
                        '"reason":"回应GM刚公开的物件"}'
                    )
                return "艾薇娅俯身取走门槛中央的黄铜分路片。"

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=LatestReplyClient(),
            model="gpt-5.6-luna-test",
        )
        step = ReplayStep(
            id="latest_heartbeat_fact",
            kind="player_message",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        result = simulator.compose(
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["艾薇娅"],
                legal_actions=["互动"],
            ),
            recent_public_context="时悠：巡逻队仍在门外。",
            last_gm_reply="黑色档案筒弹开，黄铜分路片落在门槛中央。",
        )

        self.assertFalse(result.used_fallback)
        self.assertIn("黄铜分路片", result.text)

    def test_continue_on_invalid_uses_a_state_neutral_safe_pass(self) -> None:
        class AlwaysInvalidClient:
            def create_chat_completion(self, **kwargs):
                if kwargs.get("operation") == "fu_pl.action_progress_contract":
                    return (
                        '{"valid_action_progress":false,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":true,"concrete_new_action":false,'
                        '"grounded_in_public_context":false,'
                        '"evidence":"","reason":"没有形成新行动"}'
                    )
                return "艾薇娅宣布门外的敌人已经投降。"

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=AlwaysInvalidClient(),
            model="gpt-5.6-luna-test",
            continue_on_invalid=True,
        )
        step = ReplayStep(
            id="safe_pass_after_exhaustion",
            kind="player_message",
            speaker="时雨",
            actor="艾薇娅",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )

        result = simulator.compose(
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                known_pcs=["艾薇娅"],
            ),
            recent_public_context="时悠：风铃廊里暂时没有新的变化。",
            last_gm_reply="风铃廊里暂时没有新的变化。",
        )

        self.assertEqual(result.text, "时雨: 艾薇娅暂时不采取行动。")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_kind, "exhaustion_safe_pass")
        self.assertTrue(result.fallback_diagnostics)
        self.assertNotIn("投降", result.text)

    def test_table_discussion_exhaustion_stays_out_of_character(self) -> None:
        simulator = ConstrainedPlayerSimulator(use_llm=False)
        step = ReplayStep(
            id="safe_table_discussion_after_exhaustion",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            stage_goal="正在和其他玩家短暂商量下一步，不执行角色行动。",
        )

        result = simulator._exhaustion_safe_pass(
            step,
            diagnostics=["所有候选都越过了桌边讨论边界"],
        )

        self.assertEqual(result.text, "南星: 我还没想好，先听你们的。")
        self.assertEqual(result.fallback_kind, "table_discussion_safe_silence")
        self.assertNotIn("赛璃", result.text)
        self.assertNotIn("行动", result.text)

    def test_continue_on_invalid_interacts_with_current_blocked_entrance_before_passing(self) -> None:
        class ClosedDoorClient:
            def create_chat_completion(self, **kwargs):
                if kwargs.get("operation") == "fu_pl.action_progress_contract":
                    request = json.loads(kwargs["messages"][-1].content)
                    candidate = str(request.get("candidate") or "")
                    if "登记门" in candidate and "敲" in candidate:
                        return (
                            '{"valid_action_progress":true,'
                            '"materially_advances_current_situation":true,'
                            '"mostly_restates_known_information":false,'
                            '"repeats_completed_action":false,'
                            '"repeats_micro_investigation_lane":false,'
                            '"matches_prior_rejected_lane":false,'
                            '"concrete_new_action":true,'
                            '"grounded_in_public_context":true,'
                            '"responds_to_current_pressure_or_choice":true,'
                            '"evidence":"登记门",'
                            '"reason":"角色与当前受阻入口发生了新的具体互动"}'
                        )
                    return (
                        '{"valid_action_progress":false,'
                        '"materially_advances_current_situation":false,'
                        '"mostly_restates_known_information":false,'
                        '"repeats_completed_action":true,'
                        '"concrete_new_action":false,'
                        '"grounded_in_public_context":false,'
                        '"evidence":"","reason":"仍在重复抵达"}'
                    )
                return "洛岚沿交接廊继续走到登记小室前，站在那里等守门人。"

        simulator = ConstrainedPlayerSimulator(
            use_llm=True,
            client=ClosedDoorClient(),
            model="gpt-5.6-luna-test",
            continue_on_invalid=True,
        )
        step = ReplayStep(
            id="blocked_registration_door_rescue",
            kind="player_message",
            speaker="白河",
            actor="洛岚",
            stage_goal="这是行动槽：必须提交当前角色的一个明确行动。",
        )
        latest = "众人已经抵达登记小室前的交接廊，尚未开启的登记门里没有回应。"

        result = simulator.compose(
            step=step,
            legal_context=LegalActionContext(
                stage_goal=step.stage_goal,
                scene_name="白花碑驿站内层",
                scene_location="登记小室前的交接廊",
                known_pcs=["洛岚"],
                visible_scene_elements=["尚未开启的登记门"],
                blocked_routes=["尚未开启的登记门"],
                legal_actions=["互动", "调查"],
            ),
            recent_public_context=f"时悠：{latest}",
            last_gm_reply=latest,
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.validation_errors, [])
        self.assertEqual(result.fallback_kind, "scene_interaction_rescue")
        self.assertIn("登记门", result.text)
        self.assertIn("敲", result.text)
        self.assertNotIn("继续走到", result.text)
        self.assertNotIn("门开", result.text)

    def test_minimal_replay_runner_writes_transcript_records_and_report(self) -> None:
        scenario = ReplayScenario.load(Path("tests/replay_scenarios/minimal_replay_smoke.json"))
        with tempfile.TemporaryDirectory() as tmp:
            runner = HumanLikeReplayRunner(scenario, output_root=tmp, use_llm_gm=False, use_llm_player=False)
            result = runner.run()

            self.assertTrue(result["ok"], result["errors"])
            self.assertTrue(Path(result["conversation_txt"]).exists())
            self.assertTrue(Path(result["records_jsonl"]).exists())
            self.assertTrue(Path(result["report_md"]).exists())
            transcript = Path(result["conversation_txt"]).read_text(encoding="utf-8")
            self.assertIn("玩家贡献世界细节", transcript)
            self.assertIn("白钟大陆", transcript)


if __name__ == "__main__":
    unittest.main()
