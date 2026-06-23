import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.chapter_manager import ChapterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Character,
    DungeonAreaType,
    DungeonImportance,
    DungeonPreparation,
    MemoryVisibility,
    PersistentChangeType,
    TravelEventType,
    TravelThreatLevel,
)


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class AdventurePhase3Tests(unittest.TestCase):
    def test_travel_manager_uses_event_tables_transport_and_distance(self) -> None:
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 6, 5])
        travel = TravelManager(rules)

        result = travel.travel(
            origin="雷尔德村",
            destination="坎卡山",
            distance=5,
            transport="飞行载具",
            threat_levels=[
                TravelThreatLevel.MEDIUM,
                TravelThreatLevel.HIGH,
                TravelThreatLevel.LOW,
            ],
            regions=["丘陵小径", "盗匪草原", "山脚营地"],
        )

        self.assertEqual(travel.calculate_travel_days(5, transport="飞行载具"), 2)
        self.assertEqual(result.transport, "飞行载具")
        self.assertEqual(result.travel_multiplier, 3)
        self.assertEqual(result.service_cost, 0)
        self.assertEqual(result.day_results[0].event_type, TravelEventType.DISCOVERY)
        self.assertEqual(result.day_results[0].event_detail, "古代废墟入口：队伍发现一处被遗忘的遗迹或地下城入口。")
        self.assertIn("威胁骰 d10=1", result.day_results[0].hard_rule_summary)
        self.assertIn("请 GM LLM", result.day_results[0].llm_narrative_prompt)
        self.assertIn("不要添加需要硬结算", result.day_results[2].llm_narrative_prompt)
        self.assertEqual(result.day_results[1].event_type, TravelEventType.DANGER)
        self.assertEqual(result.day_results[1].mechanical_hint, "可要求团队检定；失败时造成即兴伤害或推进威胁命刻。")
        self.assertEqual(travel.known_route("雷尔德村", "坎卡山").travel_days, 3)
        self.assertIn("古代废墟入口", travel.route_summary("雷尔德村", "坎卡山"))

    def test_travel_service_cost_uses_party_size_and_days(self) -> None:
        travel = TravelManager(RulesEngine(seed=1))

        cost = travel.service_cost("空中旅行服务", days=3, party_size=4)

        self.assertEqual(cost, 480)

    def test_dungeon_manager_builds_area_map_with_treasure_traps_and_boss_room(self) -> None:
        dungeon = DungeonManager(ClockManager(), RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "月井遗迹",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )

        state = dungeon.start_from_brief(brief)
        treasure_area = dungeon.add_area_treasure("宝箱侧室", "银爪")
        trap_area = dungeon.add_area_trap("危险走廊", "毒雾机关", state.danger_clocks[0])
        boss_area = dungeon.enter_area("Boss房")

        self.assertGreaterEqual(len(state.areas), 6)
        self.assertEqual(state.current_area, "Boss房")
        self.assertEqual(state.boss_room, "Boss房")
        self.assertEqual(dungeon.clock_manager.get(state.danger_clocks[0]).clock_type, "threat")
        self.assertEqual(treasure_area.treasure, "银爪")
        self.assertEqual(trap_area.trap, "毒雾机关")
        self.assertEqual(boss_area.area_type, DungeonAreaType.BOSS)
        self.assertIn("Boss", boss_area.notes[0])

    def test_dungeon_manager_runs_room_events_traps_treasure_and_boss_room(self) -> None:
        clocks = ClockManager()
        dungeon = DungeonManager(clocks, RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "月井遗迹",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )
        state = dungeon.start_from_brief(brief)
        dungeon.add_area_treasure("宝箱侧室", "银爪")
        dungeon.add_area_trap("危险走廊", "毒雾机关", state.danger_clocks[0])

        failed_disarm = dungeon.explore_area("危险走廊", actor="阿凛", action="disarm_trap", success=False)
        treasure = dungeon.explore_area("宝箱侧室", actor="阿凛", action="open_treasure")
        boss = dungeon.explore_area("Boss房", actor="阿凛", action="confront_boss")

        self.assertTrue(failed_disarm.trap_triggered)
        self.assertEqual(failed_disarm.danger_change.after, 1)
        self.assertIn("不要在叙事中自行发放额外奖励", failed_disarm.hard_rule_summary)
        self.assertIn("请 GM LLM", failed_disarm.llm_narrative_prompt)
        self.assertTrue(treasure.treasure_collected)
        self.assertEqual(treasure.treasure, "银爪")
        self.assertIn("奖励已被取得", treasure.llm_narrative_prompt)
        self.assertTrue(boss.boss_revealed)
        self.assertIn("不要默认每个 Boss 都有多部件", boss.llm_narrative_prompt)
        self.assertEqual(state.current_area, "Boss房")

    def test_dungeon_area_lookup_accepts_natural_language_aliases(self) -> None:
        clocks = ClockManager()
        dungeon = DungeonManager(clocks, RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "旧港星匣金库",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )
        state = dungeon.start_from_brief(brief)
        dungeon.add_area_trap("危险走廊", "旋转镜面机关", state.danger_clocks[0])

        result = dungeon.explore_area(
            "旧港星匣金库：旋转镜面走廊",
            actor="阿凛",
            action="disarm_trap",
            success=False,
            danger_segments=None,
        )

        self.assertEqual(result.area_name, "危险走廊")
        self.assertTrue(result.trap_triggered)
        self.assertEqual(result.danger_change.after, 1)

    def test_explore_dungeon_action_awards_area_treasure_and_writes_memory(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛"))
        world = WorldState()
        clocks = ClockManager()
        dungeon = DungeonManager(clocks, RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "月井遗迹",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )
        dungeon.start_from_brief(brief)
        dungeon.add_area_treasure("宝箱侧室", "银爪")
        interceptor = ActionInterceptor(
            RulesEngine(seed=1),
            characters,
            clocks,
            ConflictManager(characters),
            world,
            dungeon_manager=dungeon,
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.EXPLORE_DUNGEON,
                parameters={
                    "actor": "阿凛",
                    "area_name": "宝箱侧室",
                    "mode": "open_treasure",
                    "collect_treasure": True,
                    "fixed_zenit": 0,
                },
            )
        )

        self.assertTrue(resolution.payload["dungeon_exploration"].treasure_collected)
        self.assertIn("银爪", characters.get("阿凛").equipment)
        self.assertTrue(any(event.kind == "dungeon_exploration" for event in world.memory_events))

    def test_world_state_extracts_chinese_entities_for_memory_recall(self) -> None:
        world = WorldState()
        world.map_notes["精灵村庄"] = "被帝国军占领的森林村庄。"
        world.ensure_npc_persona("卡尔", public_identity="帝国将军", core_drive="夺取灵魂水晶")
        world.record_memory_event("卡尔占领了精灵村庄，并夺走了灵魂水晶。", kind="plot", entities=["卡尔", "精灵村庄"])
        world.record_relation("阿凛", "憎恨", "卡尔", evidence="卡尔毁掉了她的故乡。")
        world.upsert_gm_secret(
            "karl_truth",
            title="卡尔的真正目标",
            content="卡尔其实想用灵魂水晶唤醒沉睡的月神。",
            related_entities=["卡尔", "灵魂水晶"],
            lock_level="seeded",
        )

        recall = world.recall_context("我要回到精灵村庄质问卡尔", include_private=True)

        self.assertIn("精灵村庄", recall.entities)
        self.assertIn("卡尔", recall.entities)
        self.assertTrue(any("卡尔占领了精灵村庄" in memory for memory in recall.public_memory))
        self.assertTrue(any("卡尔的真正目标" in memory for memory in recall.private_memory))

    def test_chapter_manager_settles_xp_rewards_and_world_changes(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛"))
        characters.add(self.hero("白河"))
        world = WorldState()
        world.record_location_facility(
            name="月井净化阵",
            description="英雄们完成仪式后，月井重新流动。",
            source="仪式",
            location="月井遗迹",
        )
        progression = ProgressionManager(characters, world)
        economy = EconomyManager(characters, world, RulesEngine(seed=1))
        chapter = ChapterManager(progression, economy, world)

        settlement = chapter.settle_chapter(
            chapter_title="月井遗迹",
            participating_pcs=["阿凛", "白河"],
            party_level=5,
            ultima_spent=4,
            fabula_spent=2,
            difficulty="normal",
        )

        self.assertEqual(settlement.experience_report.total_xp, 10)
        self.assertEqual(settlement.reward.zenit, 500)
        self.assertEqual(characters.get("阿凛").zenit, 250)
        self.assertEqual(settlement.level_up_available, ["阿凛", "白河"])
        self.assertTrue(any("月井净化阵" in change for change in settlement.world_changes))
        self.assertTrue(any(event.kind == "chapter_settlement" for event in world.memory_events))

    def hero(self, name) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=40,
            mp=40,
            level=5,
            crisis_threshold=20,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
        )


if __name__ == "__main__":
    unittest.main()
