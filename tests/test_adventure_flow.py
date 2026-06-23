import unittest

from fu_gm.action_brain import HeuristicActionBrain
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Character,
    Clock,
    DungeonExploreMode,
    RestType,
    SceneType,
    StatusEffect,
    TravelEventType,
    TravelThreatLevel,
    HeroDraft,
)
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class SequenceActionBrain:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def decide(self, _panel):
        self.calls += 1
        return self.actions.pop(0)


class AdventureFlowTests(unittest.TestCase):
    def _orchestrator_with_brain(self, brain) -> SceneOrchestrator:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world_state = WorldState()
        rules = RulesEngine(seed=7)
        return SceneOrchestrator(
            action_brain=brain,
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
        )

    def test_turn_replans_once_when_action_references_missing_character(self) -> None:
        brain = SequenceActionBrain(
            [
                Action(ActionType.GUARD, {"actor": "尚未建卡的洛岚"}),
                Action(ActionType.NARRATE, {"summary": "洛岚的动作先进入叙事，等待角色卡补齐。"}),
            ]
        )
        app = self._orchestrator_with_brain(brain)

        reply = app.run_turn("洛岚检查灵魂晶炉。")

        self.assertEqual(brain.calls, 2)
        self.assertIn("等待角色卡补齐", reply)
        recovery = app.pipeline_telemetry()["last_turn"]["recovery"]
        self.assertEqual(recovery[0]["kind"], "action_replan")

    def test_turn_restores_valid_unconfirmed_hero_draft_before_rules(self) -> None:
        brain = SequenceActionBrain([Action(ActionType.GUARD, {"actor": "露娜"})])
        app = self._orchestrator_with_brain(brain)
        app.world_state.world_profile.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="露娜",
            identity="失国公主",
            theme="正义",
            origin="水晶王国",
            classes={"元素使": 2, "守护者": 3},
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 8},
            skills={"元素魔法": 1, "元素系仪式": 1, "铁壁": 1, "保镖": 1, "挺身守护": 1},
            spells=["元素幕障"],
            confirmed=False,
        )

        reply = app.run_turn("露娜举盾保护同伴。")

        self.assertEqual(brain.calls, 1)
        self.assertTrue(app.character_manager.exists("露娜"))
        self.assertIn("防御", reply)
        recovery = app.pipeline_telemetry()["last_turn"]["recovery"]
        self.assertEqual(recovery[0]["kind"], "hero_draft_restore")

    def test_conflict_out_of_turn_player_action_is_acknowledged_without_rules(self) -> None:
        brain = SequenceActionBrain([Action(ActionType.GUARD, {"actor": "洛岚"})])
        app = self._orchestrator_with_brain(brain)
        app.character_manager.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
                max_hp=60,
                hp=60,
                max_mp=30,
                mp=30,
                traits=["pc"],
            )
        )
        app.character_manager.add(
            Character(
                name="洛岚",
                attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 6},
                max_hp=50,
                hp=50,
                max_mp=25,
                mp=25,
                traits=["pc"],
            )
        )
        app.character_manager.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 6},
                max_hp=50,
                hp=50,
                max_mp=20,
                mp=20,
                traits=["enemy"],
            )
        )
        app.conflict_manager.start_scene("白花碑驿站伏击", ["伊莉雅", "洛岚", "财团机兵"])

        reply = app.run_turn("白河: 洛岚举锤防御。")

        self.assertIn("【回合提示】", reply)
        self.assertIn("现在轮到【伊莉雅】行动", reply)
        self.assertFalse(app.character_manager.get("洛岚").guarding)
        self.assertEqual(app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertTrue(app.conflict_manager.state.held_actions)

    def test_conflict_out_of_turn_assist_is_registered_and_consumed(self) -> None:
        brain = SequenceActionBrain(
            [
                Action(ActionType.OBJECTIVE, {"actor": "洛岚", "clock_name": "旧路闸门开启", "attributes": ["INS", "DEX"], "target_number": 10}),
                Action(ActionType.OBJECTIVE, {"actor": "伊莉雅", "clock_name": "旧路闸门开启", "attributes": ["MIG", "WLP"], "target_number": 10}),
            ]
        )
        app = self._orchestrator_with_brain(brain)
        app.interceptor.rules_engine._rng = FakeRandom([5, 4])
        for name, traits in [("伊莉雅", ["pc"]), ("洛岚", ["pc"]), ("财团机兵", ["enemy"])]:
            app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=50,
                    hp=50,
                    max_mp=25,
                    mp=25,
                    traits=traits,
                )
            )
        app.clock_manager.add(Clock("旧路闸门开启", max_segments=6, current=0, clock_type="objective"))
        app.conflict_manager.start_scene("白花碑驿站伏击", ["伊莉雅", "洛岚", "财团机兵"])

        assist_reply = app.run_turn("白河: 洛岚协助伊莉雅推进【旧路闸门开启】，用钟鸣机关稳定门轴。")

        self.assertIn("协助", assist_reply)
        self.assertEqual(app.conflict_manager.state.current_actor(), "伊莉雅")
        self.assertEqual(app.conflict_manager.state.pending_assists, {"伊莉雅": ["洛岚"]})
        self.assertIn("洛岚", app.conflict_manager.state.acted_this_round)

        action_reply = app.run_turn("阿凛: 伊莉雅用肩甲顶住门闸，推进【旧路闸门开启】。")

        self.assertIn("团队合作提供 +1 修正", action_reply)
        self.assertEqual(app.conflict_manager.state.current_actor(), "财团机兵")
        self.assertNotIn("洛岚", app.conflict_manager.state.pending_assists.get("伊莉雅", []))

    def test_conflict_turn_consuming_action_auto_advances_to_next_actor(self) -> None:
        brain = SequenceActionBrain([Action(ActionType.GUARD, {"actor": "伊莉雅"})])
        app = self._orchestrator_with_brain(brain)
        for name, traits in [("伊莉雅", ["pc"]), ("洛岚", ["pc"]), ("财团机兵", ["enemy"])]:
            app.character_manager.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=50,
                    hp=50,
                    max_mp=25,
                    mp=25,
                    traits=traits,
                )
            )
        app.conflict_manager.start_scene("白花碑驿站伏击", ["伊莉雅", "洛岚", "财团机兵"])

        reply = app.run_turn("阿凛: 伊莉雅举盾防御。")

        self.assertIn("【防御】", reply)
        self.assertIn("下一位行动者：洛岚", reply)
        self.assertTrue(app.character_manager.get("伊莉雅").guarding)
        self.assertEqual(app.conflict_manager.state.current_actor(), "洛岚")

    def test_scene_manager_tracks_current_scene_and_history(self) -> None:
        scenes = SceneManager()

        current = scenes.start_scene(
            "月下营火",
            SceneType.INTERLUDE,
            location="苍蓝森林",
            participants=["瓦莉亚", "米菈"],
            objective="确认下一步路线",
        )

        self.assertEqual(current.scene_type, SceneType.INTERLUDE)
        self.assertIn("插曲场景", scenes.format_phase())
        ended = scenes.end_scene("队伍决定前往旧矿坑。")
        self.assertIsNotNone(ended)
        self.assertFalse(ended.active)
        self.assertEqual(len(scenes.history), 1)

    def test_rest_recovers_pcs_spends_tent_ip_and_advances_threat_clock(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=7,
            max_mp=30,
            mp=3,
            inventory_points=6,
            statuses=[StatusEffect.SLOW],
            traits=["pc"],
        )
        npc = Character(
            name="旅店老板",
            attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 8},
            max_hp=20,
            hp=5,
            max_mp=10,
            mp=0,
            traits=["npc"],
        )
        characters.add(pc)
        characters.add(npc)
        clocks = ClockManager()
        clocks.add(Clock(name="帝国追兵逼近", max_segments=6, current=2))
        rest = RestManager(characters, clocks)

        result = rest.rest(
            RestType.WILDERNESS,
            safe_source="魔法帐篷",
            payer="瓦莉亚",
            threat_clocks=["帝国追兵逼近"],
        )

        self.assertEqual(characters.get("瓦莉亚").hp, 45)
        self.assertEqual(characters.get("瓦莉亚").mp, 30)
        self.assertEqual(characters.get("瓦莉亚").inventory_points, 2)
        self.assertEqual(characters.get("瓦莉亚").statuses, [])
        self.assertEqual(characters.get("旅店老板").hp, 5)
        self.assertEqual(result.ip_spent, 4)
        self.assertEqual(clocks.get("帝国追兵逼近").current, 3)

    def test_travel_manager_resolves_discovery_danger_and_quiet_days(self) -> None:
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 6, 5])
        travel = TravelManager(rules)

        result = travel.travel(
            origin="雷尔德村",
            destination="坎卡山",
            threat_levels=[
                TravelThreatLevel.MEDIUM,
                TravelThreatLevel.HIGH,
                TravelThreatLevel.LOW,
            ],
            regions=["丘陵小径", "盗匪草原", "山脚营地"],
        )

        self.assertEqual(result.days, 3)
        self.assertEqual(result.day_results[0].event_type, TravelEventType.DISCOVERY)
        self.assertEqual(result.day_results[1].event_type, TravelEventType.DANGER)
        self.assertEqual(result.day_results[2].event_type, TravelEventType.QUIET)
        self.assertEqual(result.day_results[1].die_size, 12)

    def test_dungeon_manager_creates_and_advances_danger_clocks(self) -> None:
        clocks = ClockManager()
        dungeon = DungeonManager(clocks)

        state = dungeon.start_dungeon(
            "镜之水道",
            DungeonExploreMode.DETAILED,
            location="旧王国地下",
            danger_clocks={"高度警戒": 4, "水道坍塌": 8},
        )
        change = dungeon.exploration_failure("高度警戒", margin=-4)

        self.assertTrue(state.active)
        self.assertEqual(clocks.get("高度警戒").current, 2)
        self.assertEqual(change.delta, 2)
        self.assertIn("高度警戒", dungeon.format_status())
        ended = dungeon.end_dungeon("英雄们抵达水道深处。")
        self.assertIsNotNone(ended)
        self.assertFalse(dungeon.state.active)

    def test_orchestrator_can_run_adventure_loop_outside_conflict(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=10,
                max_mp=30,
                mp=5,
                inventory_points=4,
                traits=["pc"],
            )
        )
        clocks = ClockManager()
        clocks.add(Clock(name="天启仪式", max_segments=8, current=1))
        conflict = ConflictManager(characters)
        rules = RulesEngine()
        rules._rng = FakeRandom([1])
        world_state = WorldState()
        world_state.world_profile.villain_seeds.append("水镜女王正在唤醒天启仪式。")
        world_state.world_profile.mysteries.append("镜之水道为什么会倒映未来？")
        app = SceneOrchestrator(
            action_brain=HeuristicActionBrain(),
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world_state,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world_state),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            rest_manager=RestManager(characters, clocks),
            travel_manager=TravelManager(rules),
            dungeon_manager=DungeonManager(clocks),
        )

        app.start_scene("出发前夜", SceneType.INTERLUDE, location="雷尔德村")
        panel = app.build_panel("整理装备")
        self.assertIn("插曲场景", panel.game_phase)
        self.assertIn("主持流程指导", panel.memory_guidance)
        self.assertIn("幕间", panel.memory_guidance)
        self.assertIn("长期故事节奏", panel.memory_guidance)
        self.assertIn("反派压力", panel.memory_guidance)

        rest_result = app.take_rest(
            RestType.WILDERNESS,
            safe_source="魔法帐篷",
            payer="瓦莉亚",
            threat_clocks=["天启仪式"],
        )
        journey = app.travel(
            origin="雷尔德村",
            destination="镜之水道",
            threat_levels=[TravelThreatLevel.MEDIUM],
        )
        dungeon = app.start_dungeon(
            "镜之水道",
            DungeonExploreMode.SCENE,
            danger_clocks={"游荡的守卫": 6},
        )

        self.assertEqual(rest_result.ip_spent, 4)
        self.assertEqual(journey.day_results[0].event_type, TravelEventType.DISCOVERY)
        self.assertTrue(dungeon.active)
        self.assertIn("地下城场景", app.build_panel("继续探索").game_phase)
        self.assertTrue(world_state.memories)


if __name__ == "__main__":
    unittest.main()
