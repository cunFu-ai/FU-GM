import unittest

from fu_gm.components.adventure_event_manager import AdventureEventManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import DungeonImportance, DungeonPreparation, SecretLockLevel


class AdventureEventManagerTests(unittest.TestCase):
    def test_travel_templates_use_terrain_faction_public_memory_and_secret_hooks(self) -> None:
        world = WorldState()
        world_map = WorldMapManager(world)
        world_map.add_location(
            "精灵村庄",
            x=0,
            y=0,
            description="被帝国军占领的森林村庄。",
            terrain="森林",
            faction="帝国军",
            threat_level="high",
        )
        world.record_memory_event(
            "卡尔占领了精灵村庄，并夺走灵魂水晶。",
            kind="plot",
            entities=["卡尔", "精灵村庄", "帝国军"],
        )
        world.upsert_gm_secret(
            "karl_moon_god",
            title="卡尔的真正目标",
            content="卡尔想用灵魂水晶唤醒沉睡月神。",
            lock_level=SecretLockLevel.SEEDED,
            related_entities=["卡尔", "精灵村庄"],
        )
        events = AdventureEventManager(world)

        tables = events.travel_event_tables_for_region("精灵村庄")
        danger_names = [template.name for template in tables["danger"]]
        discovery_names = [template.name for template in tables["discovery"]]

        self.assertIn("迷雾兽径", danger_names)
        self.assertIn("帝国军的巡逻", danger_names)
        self.assertIn("旧怨回声", danger_names)
        self.assertIn("暗线阴影", danger_names)
        self.assertIn("林间路标", discovery_names)
        self.assertIn("帝国军的遗留物", discovery_names)
        self.assertIn("旧日线索", discovery_names)

    def test_prepared_location_candidates_are_backstage_travel_discovery_hooks(self) -> None:
        world = WorldState()
        world.world_profile.magic_tech_role = "辉钢财团垄断灵魂能源，上层城市享受阳光，下层街区承受污染。"
        world.world_profile.major_locations["永雨工业城"] = "公司安保和魔导工厂统治的双层城市。"
        world_map = WorldMapManager(world)
        world_map.add_location(
            "永雨工业城",
            x=0,
            y=0,
            description="公司安保和魔导工厂统治的双层城市。",
            terrain="城市",
            faction="辉钢财团",
            threat_level="medium",
        )
        events = AdventureEventManager(world)

        candidates = events.prepared_location_candidates()
        tables = events.travel_event_tables_for_region("永雨工业城")
        discovery_names = [template.name for template in tables["discovery"]]

        self.assertTrue(any(seed.name in {"企业星城", "灵魂中枢"} for seed in candidates))
        self.assertTrue(any(name.startswith("预备地点线索：") for name in discovery_names))

    def test_dungeon_templates_enrich_areas_and_surface_during_exploration(self) -> None:
        world = WorldState()
        world_map = WorldMapManager(world)
        world_map.add_location(
            "月井遗迹",
            x=2,
            y=2,
            description="森林深处的旧文明遗迹，被辉钢财团盯上。",
            terrain="森林",
            faction="辉钢财团",
        )
        world.record_memory_event(
            "辉钢财团曾在月井遗迹附近失踪了一支调查队。",
            kind="plot",
            entities=["辉钢财团", "月井遗迹"],
        )
        dungeon = DungeonManager(ClockManager(), RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "月井遗迹",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )
        state = dungeon.start_from_brief(brief, location="月井遗迹")
        world_map.enrich_dungeon_state(state)

        treasure_area = next(area for area in state.areas if area.name == "宝箱侧室")
        boss_area = next(area for area in state.areas if area.name == "Boss房")
        self.assertTrue(any(template.name == "带故事的宝箱" for template in treasure_area.event_templates))
        self.assertTrue(any(template.name == "辉钢财团痕迹" for template in boss_area.event_templates))

        result = dungeon.explore_area("宝箱侧室", actor="阿凛", action="open_treasure")

        self.assertEqual(result.event_name, "带故事的宝箱")
        self.assertIn("宝箱旁", result.event_detail)
        self.assertIn("treasure", result.event_tags)


if __name__ == "__main__":
    unittest.main()
