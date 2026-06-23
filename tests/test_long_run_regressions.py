import json
import unittest

from fu_gm.action_brain import LLMActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Character,
    Clock,
    ClockChange,
    GamePanel,
    RitualDiscipline,
    RitualPotency,
    RitualScope,
    RollOutcome,
)
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)

    def post_json(self, url, headers, payload, timeout):
        content = self.responses.pop(0)
        return {"choices": [{"message": {"content": content}}]}


def _llm_brain_with_response(response: dict) -> LLMActionBrain:
    config = LLMConfig(
        api_base_url="https://api.example.test",
        api_key="test-key",
        action_model="gpt-test",
        expressor_model="gpt-test",
    )
    client = OpenAICompatibleClient(config, transport=FakeTransport([json.dumps(response, ensure_ascii=False)]))
    return LLMActionBrain(client=client, model=config.action_model)


def _character_manager() -> CharacterManager:
    characters = CharacterManager()
    characters.add(
        Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=40,
            mp=40,
            fabula_points=2,
            traits=["pc"],
        )
    )
    characters.add(
        Character(
            name="赛璃",
            attributes={"DEX": 6, "MIG": 8, "INS": 10, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=70,
            mp=70,
            traits=["pc"],
            skills={"御魂系仪式": 1},
        )
    )
    characters.add(
        Character(
            name="财团机兵",
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=30,
            mp=30,
            traits=["enemy"],
        )
    )
    return characters


class LongRunRegressionTests(unittest.TestCase):
    def test_repeated_start_conflict_keeps_existing_turn_order(self) -> None:
        characters = _character_manager()
        conflict = ConflictManager(characters)
        conflict.start_scene("白花碑驿站伏击", ["财团机兵", "伊莉雅", "赛璃"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                ActionType.START_CONFLICT,
                {
                    "scene_name": "白花碑驿站伏击",
                    "pcs": ["伊莉雅", "赛璃"],
                    "enemies": ["财团机兵"],
                    "leader": "伊莉雅",
                },
            )
        )

        self.assertTrue(resolution.payload["conflict_already_active"])
        self.assertEqual(conflict.state.scene_name, "白花碑驿站伏击")
        self.assertEqual(conflict.state.turn_order, ["财团机兵", "伊莉雅", "赛璃"])
        self.assertIn("不重新初始化冲突", resolution.rules_text)

    def test_player_success_on_threat_clock_erases_instead_of_advancing(self) -> None:
        characters = _character_manager()
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])
        clocks = ClockManager()
        clocks.add(Clock(name="财团巡逻队逼近", max_segments=6, current=5, clock_type="threat"))
        interceptor = ActionInterceptor(engine, characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "伊莉雅",
                    "clock_name": "财团巡逻队逼近",
                    "attributes": ["DEX", "INS"],
                    "target_number": 1,
                },
            )
        )

        change = resolution.payload["clock_change"]
        self.assertLess(change.after, change.before)
        self.assertTrue(resolution.payload["clock_direction_corrected"])
        self.assertEqual(clocks.get("财团巡逻队逼近").current, 2)

    def test_objective_target_number_zero_is_clamped_to_ten(self) -> None:
        characters = _character_manager()
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 5])
        clocks = ClockManager()
        clocks.add(Clock(name="旧路闸门开启", max_segments=6, current=0, clock_type="objective"))
        interceptor = ActionInterceptor(engine, characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "伊莉雅",
                    "clock_name": "旧路闸门开启",
                    "attributes": ["DEX", "INS"],
                    "target_number": 0,
                },
            )
        )

        self.assertEqual(resolution.payload["roll"].target_number, 10)
        self.assertFalse(resolution.payload["roll"].success)

    def test_conditional_trait_invocation_does_not_spend_when_prior_roll_succeeded(self) -> None:
        characters = _character_manager()
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 6])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())
        interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "伊莉雅",
                    "target": "旧路闸门开启",
                    "attributes": ["MIG", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        before_fp = characters.get("伊莉雅").fabula_points

        resolution = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "伊莉雅",
                    "trait_name": "责任",
                    "reroll_indices": [1],
                    "skip_if_pending_roll_success": True,
                },
            )
        )

        self.assertTrue(resolution.payload["skipped_invocation"])
        self.assertEqual(characters.get("伊莉雅").fabula_points, before_fp)
        self.assertIn("不触发", resolution.rules_text)

    def test_action_brain_forces_conditional_invocation_window_even_if_llm_misroutes(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "Objective",
                "parameters": {"clock_name": "责任", "target": "责任", "attributes": ["INS", "DEX"]},
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["[旧路闸门开启] 6/6"],
                pc_status=["伊莉雅: HP 60/60", "洛岚: HP 45/45"],
                enemy_status=[],
                recent_chat="阿凛: 如果刚才伊莉雅的推进检定差一点，我消耗1点物语点援用主题【责任】重掷低的那枚骰子；如果已经成功，就把这句话当作不触发的规则窗口说明。",
                current_actor="伊莉雅",
            )
        )

        self.assertEqual(action.action_type, ActionType.INVOKE_TRAIT)
        self.assertTrue(action.parameters["skip_if_pending_roll_success"])
        self.assertEqual(action.parameters["trait_name"], "责任")

    def test_ritual_contribution_uses_planned_ritual_dl(self) -> None:
        characters = _character_manager()
        engine = RulesEngine()
        engine._rng = FakeRandom([3, 4])
        clocks = ClockManager()
        ritual = RitualManager(engine, characters, clocks)
        plan = ritual.plan_ritual(
            caster="赛璃",
            name="风铃回声",
            discipline=RitualDiscipline.SPIRITISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="确认旅人的记忆回声。",
        )
        ritual.start_conflict_ritual(plan)

        outcome, change = ritual.contribute_to_ritual(plan.clock_name, actor="赛璃")

        self.assertEqual(outcome.target_number, 7)
        self.assertTrue(outcome.success)
        self.assertGreater(change.after, change.before)

    def test_action_brain_coerces_descriptive_arcanum_assist_to_objective(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "Skill",
                "parameters": {"actor": "苍祈", "skill_name": "契约与召唤", "target": "旧路闸门开启"},
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["[旧路闸门开启] 2/6"],
                pc_status=["苍祈: HP 40/40", "洛岚: HP 45/45"],
                enemy_status=["财团机兵: HP 48/60"],
                recent_chat="澄砚: 苍祈协助洛岚推进【旧路闸门开启】，他用奥灵留下的树皮名纹稳定门轴。",
                current_actor="苍祈",
            )
        )

        self.assertEqual(action.action_type, ActionType.OBJECTIVE)
        self.assertEqual(action.parameters["actor"], "苍祈")
        self.assertEqual(action.parameters["clock_name"], "旧路闸门开启")
        self.assertTrue(action.parameters["cooperative_progress"])

    def test_action_brain_prefers_project_over_ritual_for_engineering_work(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "PlanRitual",
                "parameters": {
                    "caster": "洛岚",
                    "name": "修复白花守望会旧式信号塔",
                    "potency": "medium",
                    "scope": "facility",
                    "effect": "提前发现巡逻。",
                },
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="幕间场景",
                active_clocks=[],
                pc_status=["洛岚: HP 45/45"],
                enemy_status=[],
                recent_chat="白河: 洛岚启动工程【修复白花守望会旧式信号塔】，目标是让守望会能提前发现财团巡逻。",
                current_actor="洛岚",
            )
        )

        self.assertEqual(action.action_type, ActionType.START_PROJECT)
        self.assertEqual(action.parameters["inventor"], "洛岚")
        self.assertEqual(action.parameters["owner"], "洛岚")
        self.assertIn("信号塔", action.parameters["name"])

    def test_action_brain_recovers_incomplete_objective_as_scene_narration(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "Objective",
                "parameters": {"reasoning": "误把开场交谈当成目标命刻。"},
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="第一章场景",
                active_clocks=[],
                pc_status=["伊莉雅: HP 60/60", "赛璃: HP 45/45"],
                enemy_status=[],
                recent_chat="阿凛: 伊莉雅把碎月遗物固定在盾后，走进白花碑驿站的风铃廊。她先向守望会会长说明来意。",
                current_actor="",
            )
        )

        self.assertEqual(action.action_type, ActionType.NARRATE)
        self.assertIn("伊莉雅", action.parameters["summary"])

    def test_action_brain_recovers_incomplete_objective_as_investigation(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "Objective",
                "parameters": {"attributes": ["INS", "WLP"], "reasoning": "误把普通调查当成目标命刻。"},
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="第一章场景",
                active_clocks=[],
                pc_status=["伊莉雅: HP 60/60", "赛璃: HP 45/45"],
                enemy_status=[],
                recent_chat="南星: 赛璃只做普通调查：她观察旅人的呼吸和灰晶光泽，想判断记忆是否被导向灵魂中枢。若需要检定，我用洞察+意志。",
                current_actor="",
            )
        )

        self.assertEqual(action.action_type, ActionType.INVESTIGATE)
        self.assertEqual(action.parameters["actor"], "赛璃")
        self.assertEqual(action.parameters["attributes"], ["INS", "WLP"])
        self.assertIn("旅人", action.parameters["target"])

    def test_action_brain_normalizes_equip_item_dicts(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "Equip",
                "parameters": {
                    "actor": "洛岚",
                    "items": [{"slot": "main_hand", "item_name": "铁锤（按铁锤模板结算）"}],
                },
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["洛岚: HP 45/45", "伊莉雅: HP 60/60"],
                enemy_status=["财团机兵: HP 48/60"],
                recent_chat="白河: 洛岚执行装备动作，把主手换成铁锤，副手空出来方便调整机关；不要更换防具。",
                current_actor="洛岚",
            )
        )

        self.assertEqual(action.action_type, ActionType.EQUIP)
        self.assertEqual(action.parameters["actor"], "洛岚")
        self.assertEqual(action.parameters["items"], ["铁锤"])

    def test_project_actor_prefers_earliest_character_not_helper(self) -> None:
        brain = _llm_brain_with_response(
            {
                "action_type": "PlanRitual",
                "parameters": {"caster": "伊莉雅", "name": "修复白花守望会旧式信号塔"},
            }
        )

        action = brain.decide(
            GamePanel(
                game_phase="幕间场景",
                active_clocks=[],
                pc_status=["伊莉雅: HP 60/60", "赛璃: HP 45/45", "洛岚: HP 45/45"],
                enemy_status=[],
                recent_chat="白河: 洛岚启动工程【修复白花守望会旧式信号塔】，目标是让守望会能提前发现财团巡逻。赛璃和伊莉雅今天帮工。",
                current_actor="",
            )
        )

        self.assertEqual(action.action_type, ActionType.START_PROJECT)
        self.assertEqual(action.parameters["inventor"], "洛岚")

    def test_noncombat_investigation_and_objective_render_full_roll_panel(self) -> None:
        investigation = ActionResolution(
            action=Action(ActionType.INVESTIGATE, {}),
            rules_text="调查检定 14: 成功。",
            payload={
                "roll": RollOutcome(
                    actor="赛璃",
                    attributes=["INS", "INS"],
                    dice=[(10, 7), (10, 7)],
                    total=14,
                    modifier=0,
                    high_roll=7,
                    target_number=10,
                    success=True,
                    critical_success=True,
                    fumble=False,
                    opportunity_count=1,
                    target="风铃回声",
                ),
                "information": ["风铃内侧有被改写的名字。"],
            },
        )
        objective = ActionResolution(
            action=Action(ActionType.OBJECTIVE, {}),
            rules_text="命刻 [旧路闸门开启] 推进 1 格。",
            payload={
                "roll": RollOutcome(
                    actor="洛岚",
                    attributes=["INS", "DEX"],
                    dice=[(10, 4), (8, 6)],
                    total=10,
                    modifier=0,
                    high_roll=6,
                    target_number=10,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="旧路闸门开启",
                ),
                "clock_change": ClockChange("旧路闸门开启", 2, 3, 1, 6),
            },
        )

        rendered_investigation = Expressor().render(investigation)
        rendered_objective = Expressor().render(objective)

        self.assertIn("掷骰 d10=7 + d10=7 = 14", rendered_investigation)
        self.assertIn("大成功", rendered_investigation)
        self.assertIn("掷骰 d10=4 + d8=6 = 10", rendered_objective)
        self.assertIn("结算值 10 vs DL 10", rendered_objective)

    def test_cooperative_objective_renders_as_coordinated_progress(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.OBJECTIVE, {"cooperative_progress": True}),
            rules_text="命刻 [旧路闸门开启] 推进 1 格。",
            payload={
                "cooperative_progress": True,
                "roll": RollOutcome(
                    actor="苍祈",
                    attributes=["INS", "DEX"],
                    dice=[(8, 5), (8, 5)],
                    total=10,
                    modifier=0,
                    high_roll=5,
                    target_number=10,
                    success=True,
                    critical_success=False,
                    fumble=False,
                    target="旧路闸门开启",
                ),
                "clock_change": ClockChange("旧路闸门开启", 2, 3, 1, 6),
            },
        )

        rendered = Expressor().render(resolution)

        self.assertIn("【协同推进】苍祈", rendered)
        self.assertNotIn("【目标行动】", rendered)

    def test_session_zero_ack_uses_hero_fact_not_location_fact_for_name(self) -> None:
        facilitator = HeuristicSessionZeroFacilitator()

        line = facilitator._acknowledgement_line(
            "澄砚",
            [
                "记录关键地点【苍绿森林边境村】：沉默森林边缘的村社。",
                "已记录【苍祈】的技能选择。",
            ],
        )

        self.assertIn("【苍祈】的技能选择记好了", line)
        self.assertNotIn("苍绿森林边境村】的技能", line)
