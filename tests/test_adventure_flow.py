import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.scene_frame_manager import SceneFrame
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
    TimedEffect,
    EffectTiming,
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






class AdventureFlowTests(unittest.TestCase):













































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

    def test_scene_manager_parallel_focus_preserves_scene_and_shared_action_round(self) -> None:
        scenes = SceneManager()
        registration = scenes.start_scene(
            "登记小室查册",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["赛璃", "洛岚"],
        )
        registration.action_round_required_actors = ["伊莉雅", "赛璃", "洛岚", "艾薇娅"]
        registration.action_round_acted_actors = ["赛璃", "洛岚"]
        scenes.actor_locations["艾薇娅"] = "白花碑驿站"

        branch, mode = scenes.focus_actor_branch(
            "艾薇娅",
            name="白花碑回撤点",
            location="白花碑后方檐柱阴影下",
            objective="守住退路",
        )

        self.assertEqual(mode, "created")
        self.assertIs(scenes.current_scene, branch)
        self.assertIn(registration, scenes.suspended_scenes)
        self.assertTrue(registration.active)
        self.assertEqual(scenes.history, [])
        progress = scenes.record_action_round_action(
            "艾薇娅",
            ["伊莉雅", "赛璃", "洛岚", "艾薇娅"],
        )
        self.assertFalse(progress["completed"])
        self.assertEqual(progress["acted"], ["赛璃", "洛岚", "艾薇娅"])
        self.assertEqual(progress["waiting"], ["伊莉雅"])

        restored, mode = scenes.focus_actor_branch(
            "赛璃",
            name="不会使用的新名称",
            location="不会使用的新地点",
        )
        self.assertEqual(mode, "restored")
        self.assertIs(restored, registration)
        self.assertIn(branch, scenes.suspended_scenes)
        self.assertEqual(scenes._action_round_state()[2], ["赛璃", "洛岚", "艾薇娅"])

    def test_scene_manager_joins_actor_to_focused_scene_at_exact_location(self) -> None:
        scenes = SceneManager()
        registration = scenes.start_scene(
            "登记小室查册",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["赛璃", "洛岚"],
        )
        scenes.actor_locations["艾薇娅"] = "白花碑驿站·风铃廊"

        joined, mode = scenes.focus_actor_branch(
            "艾薇娅",
            name="不应创建的重复登记小室",
            location="白花碑驿站·登记小室",
        )

        self.assertEqual(mode, "joined")
        self.assertIs(joined, registration)
        self.assertEqual(joined.participants, ["赛璃", "洛岚", "艾薇娅"])
        self.assertEqual(
            joined.participant_locations["艾薇娅"],
            "白花碑驿站·登记小室",
        )
        self.assertEqual(scenes.suspended_scenes, [])
        self.assertEqual(scenes._scene_counter, 1)

    def test_scene_manager_restores_existing_actor_branch_before_location_join(self) -> None:
        scenes = SceneManager()
        registration = scenes.start_scene(
            "登记小室查册",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["赛璃"],
        )
        branch, _ = scenes.focus_actor_branch(
            "艾薇娅",
            name="后门守望",
            location="白花碑驿站·后门",
        )

        restored, mode = scenes.focus_actor_branch(
            "赛璃",
            name="不应使用的新场景",
            location="白花碑驿站·后门",
        )

        self.assertEqual(mode, "restored")
        self.assertIs(restored, registration)
        self.assertNotIn("赛璃", branch.participants)
        self.assertIn(branch, scenes.suspended_scenes)

    def test_parallel_scene_end_can_restore_branch_and_session_end_closes_all(self) -> None:
        scenes = SceneManager()
        registration = scenes.start_scene(
            "登记小室",
            SceneType.STANDARD,
            location="白花碑驿站·登记小室",
            participants=["伊莉雅"],
        )
        scenes.actor_locations["艾薇娅"] = "白花碑驿站"
        branch, _ = scenes.focus_actor_branch(
            "艾薇娅",
            name="白花碑回撤点",
            location="白花碑后方",
        )

        ended = scenes.end_scene("回撤点暂时安全。")
        restored = scenes.restore_latest_suspended()

        self.assertIs(ended, branch)
        self.assertIs(restored, registration)
        self.assertTrue(registration.active)
        self.assertFalse(branch.active)
        closed = scenes.end_all_scenes("本场收束。")
        self.assertEqual(closed, [registration])
        self.assertIsNone(scenes.current_scene)
        self.assertEqual(scenes.suspended_scenes, [])
        self.assertFalse(registration.active)

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
        clocks.add(
            Clock(
                name="帝国追兵逼近",
                max_segments=6,
                current=2,
                clock_type="threat",
                scope="campaign",
                advance_on_rest=True,
            )
        )
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

    def test_rest_can_recover_only_the_present_split_party(self) -> None:
        characters = CharacterManager()
        for name in ("瓦莉亚", "米菈"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=45,
                    hp=10,
                    max_mp=35,
                    mp=5,
                    traits=["pc"],
                    statuses=[StatusEffect.SLOW],
                )
            )
        rest = RestManager(characters, ClockManager())

        result = rest.rest(
            RestType.SETTLEMENT,
            safe_source="山中驿舍",
            participants=["瓦莉亚"],
        )

        self.assertEqual(result.recovered_characters, ["瓦莉亚"])
        self.assertEqual(characters.get("瓦莉亚").hp, 45)
        self.assertEqual(characters.get("瓦莉亚").mp, 35)
        self.assertEqual(characters.get("瓦莉亚").statuses, [])
        self.assertEqual(characters.get("米菈").hp, 10)
        self.assertEqual(characters.get("米菈").mp, 5)
        self.assertEqual(characters.get("米菈").statuses, [StatusEffect.SLOW])

    def test_invalid_rest_clock_is_rejected_before_resources_or_recovery_change(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                max_hp=45,
                hp=10,
                max_mp=35,
                mp=5,
                inventory_points=6,
                traits=["pc"],
            )
        )
        clocks = ClockManager()
        clocks.add(
            Clock(
                name="修好断桥",
                max_segments=6,
                current=2,
                clock_type="objective",
                scope="campaign",
            )
        )
        rest = RestManager(characters, clocks)

        with self.assertRaisesRegex(ValueError, "没有登记"):
            rest.rest(
                RestType.WILDERNESS,
                safe_source="魔法帐篷",
                payer="瓦莉亚",
                threat_clocks=["修好断桥"],
            )

        character = characters.get("瓦莉亚")
        self.assertEqual(character.hp, 10)
        self.assertEqual(character.mp, 5)
        self.assertEqual(character.inventory_points, 6)
        self.assertEqual(clocks.get("修好断桥").current, 2)

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
        clocks.add(
            Clock(
                name="天启仪式",
                max_segments=8,
                current=1,
                clock_type="villain",
                scope="campaign",
                advance_on_rest=True,
            )
        )
        conflict = ConflictManager(characters)
        rules = RulesEngine()
        rules._rng = FakeRandom([1])
        world_state = WorldState()
        world_state.world_profile.villain_seeds.append("水镜女王正在唤醒天启仪式。")
        world_state.world_profile.mysteries.append("镜之水道为什么会倒映未来？")
        app = SceneOrchestrator(
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
