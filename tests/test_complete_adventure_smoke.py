import unittest

from fu_gm.components.chapter_manager import ChapterManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.progression_manager import ProgressionManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Character,
    DungeonExploreMode,
    DungeonImportance,
    DungeonPreparation,
    EnemyRank,
    EscalationStage,
    SceneType,
    TravelEventType,
)
from fu_gm.scene_orchestrator import SceneOrchestrator


class CompleteAdventureSmokeTests(unittest.TestCase):
    def test_session_zero_to_chapter_settlement_smoke(self) -> None:
        rules = RulesEngine(seed=7)
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world = WorldState()
        travel = TravelManager(rules)
        dungeon = DungeonManager(clocks, rules)
        world_map = WorldMapManager(world)
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            session_zero_manager=SessionZeroManager(world),
            rest_manager=RestManager(characters, clocks),
            travel_manager=travel,
            dungeon_manager=dungeon,
            world_map_manager=world_map,
        )
        economy = EconomyManager(characters, world, rules)
        progression = ProgressionManager(characters, world)
        chapter = ChapterManager(progression, economy, world)

        app.session_zero_manager.start()
        app.session_zero_manager.apply_world_updates(
            {
                "campaign_title": "星尘宝箱谭",
                "world_style": "高度奇幻",
                "map_card": "沿海大陆与近海岛屿地图卡",
                "travel_day_length": "一天路程",
                "magic_tech_role": "魔法被视为古代遗迹技术的一部分，现代工坊只能仿制少量魔导装置。",
                "group_concept": "追寻遗失传说的旅行英雄团",
                "starting_region": "雾潮边境村",
                "major_locations": {
                    "雾潮边境村": "建在断崖海岸与古代遗迹之间的补给村。",
                    "星尘迷宫": "每次被月光照到都会改变路径的古代地下城。",
                },
                "kingdoms": {"苍蓝海岸同盟": "由海岸村镇与近海岛屿组成的松散同盟，依靠港口贸易和遗迹地图维生。"},
                "historical_events": ["星尘迷宫在月蚀之夜从海雾中显现，改变了各国对古代遗迹的争夺。"],
                "factions": {"苍蓝探险者协会": "垄断遗迹地图，也会雇佣英雄探索危险迷宫。"},
                "villain_seeds": ["宝箱王收藏英雄的愿望，并把它们锁进活体宝箱。"],
                "world_threats": ["活体宝箱正在吞没边境村镇的愿望与记忆。"],
                "villain_mirrors": ["宝箱王映照英雄对奇遇、宝藏与命运捷径的渴望。"],
                "mysteries": ["星尘迷宫为何会根据英雄的愿望改变房间？"],
                "safety_lines": ["详细酷刑"],
                "hero_drafts": {
                    "阿凛": {
                        "hero_name": "露娜",
                        "identity": "失国公主",
                        "theme": "正义",
                        "origin": "水晶王国",
                        "classes": {"元素使": 2, "守护者": 3},
                        "attributes": {"DEX": "d8", "INS": "d10", "MIG": "d6", "WLP": "d8"},
                        "skills": {"元素魔法": 1, "元素系仪式": 1, "铁壁": 1, "保镖": 1, "挺身守护": 1},
                        "spells": ["元素幕障"],
                        "equipment": ["法杖", "青铜盾"],
                    }
                },
            }
        )
        app.session_zero_manager.generate_first_act_candidates()
        app.session_zero_manager.record_first_act_vote("阿凛", "2")
        app.session_zero_manager.confirm_first_act("2")
        self.assertTrue(app.session_zero_manager.finish_if_ready())

        characters.add(self.hero("阿凛"))
        characters.add(self.hero("白河"))
        world_map.add_location(
            "云海边境村",
            x=0,
            y=0,
            description="浮岛边缘的补给村。",
            terrain="村庄",
        )
        world_map.add_location(
            "星尘迷宫",
            x=1,
            y=0,
            description="月光改变道路的古代地下城。",
            terrain="森林",
            faction="苍蓝探险者协会",
        )

        app.start_scene(
            "第一幕：月光下的委托",
            SceneType.STANDARD,
            location="云海边境村",
            participants=["阿凛", "白河"],
            objective="接受前往星尘迷宫的委托",
        )
        app.end_scene("苍蓝探险者协会交出半张地图，英雄们决定出发。")

        journey = app.travel(origin="云海边境村", destination="星尘迷宫", transport="徒步", distance=1)
        self.assertEqual(journey.destination, "星尘迷宫")
        self.assertGreaterEqual(len(journey.day_results), 1)
        self.assertIn(journey.day_results[0].event_type, {TravelEventType.QUIET, TravelEventType.DANGER, TravelEventType.DISCOVERY})

        brief = dungeon.design_dungeon(
            "星尘迷宫",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            purpose="寻找传说中会回应愿望的星尘宝箱。",
            concept="被遗忘的迷宫",
            focus="一枚元素裂片",
            inhabitants="魔导科技构装体",
            peculiarity="移动的走廊和阶梯",
            mode=DungeonExploreMode.DETAILED,
        )
        dungeon_state = dungeon.start_from_brief(brief, location="星尘迷宫入口")
        placements = economy.plan_dungeon_rewards(dungeon_state, party_level=5, pc_count=2, rare_items=["银爪"])
        treasure_area = next(area for area in dungeon_state.areas if area.name == "宝箱侧室")
        self.assertTrue(placements)
        self.assertEqual(treasure_area.reward_item, "银爪")

        app.explore_dungeon_area("入口", actor="阿凛", action="enter")
        app.explore_dungeon_area("前厅", actor="白河", action="search", success=True)
        treasure = app.explore_dungeon_area("宝箱侧室", actor="阿凛", action="open_treasure", collect_treasure=True)
        chest = economy.open_chest(
            "阿凛",
            "星尘宝箱",
            fixed_item=treasure.reward_item,
            fixed_zenit=treasure.reward_zenit,
        )
        self.assertIn("银爪", characters.get("阿凛").equipment)
        self.assertGreater(chest.zenit, 0)

        boss = Character(
            name="宝箱王",
            attributes={"DEX": 8, "MIG": 10, "INS": 10, "WLP": 10},
            max_hp=60,
            hp=1,
            max_mp=40,
            mp=20,
            traits=["enemy", "villain"],
            level=5,
        )
        characters.add(boss)
        conflict.register_enemy(
            "宝箱王",
            EnemyRank.VILLAIN,
            ultima_points=1,
            escalation_stages=[EscalationStage(name="愿望吞噬者", ultima_points=5, hp_restore=20)],
        )
        conflict.start_scene("Boss：星尘宝箱深处", ["阿凛", "宝箱王", "白河"])
        characters.modify_resource("宝箱王", "hp", -1)
        escape = conflict.resolve_zero_hp("宝箱王")
        self.assertEqual(escape.event_type, "villain_escape")
        characters.modify_resource("宝箱王", "hp", -20)
        surrender = conflict.resolve_zero_hp("宝箱王", villain_mode="surrender", allow_escalation=False)
        self.assertEqual(surrender.event_type, "villain_surrender")
        conflict.end_scene()
        app.end_dungeon("英雄们打开真正的星尘宝箱，宝箱王的影子被封回水晶。")
        world.record_location_facility(
            name="星尘宝箱净化阵",
            description="英雄们把元素裂片安置在迷宫核心，使宝箱不再吞噬愿望。",
            source="Boss 战结局",
            location="星尘迷宫",
        )

        settlement = chapter.settle_chapter(
            chapter_title="星尘迷宫",
            participating_pcs=["阿凛", "白河"],
            party_level=5,
            ultima_spent=4,
            fabula_spent=2,
            difficulty="boss",
            rare_item="物语之戒",
        )

        self.assertTrue(settlement.level_up_available)
        self.assertIn("物语之戒", characters.get("阿凛").equipment)
        self.assertTrue(any("星尘宝箱净化阵" in change for change in settlement.world_changes))
        self.assertTrue(any(event.kind == "chapter_settlement" for event in world.memory_events))
        self.assertTrue(any("Session 0 第一幕" in memory for memory in world.memories))

    def hero(self, name: str) -> Character:
        return Character(
            name=name,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=35,
            mp=35,
            crisis_threshold=22,
            inventory_points=6,
            max_inventory_points=6,
            fabula_points=3,
            zenit=200,
            traits=["pc"],
            classes={"旅人": 2, "武器大师": 3},
            skills={"宝藏猎人": 1, "近战武器掌握": 1},
        )


if __name__ == "__main__":
    unittest.main()
