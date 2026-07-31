import tempfile
import unittest
from pathlib import Path

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Character, TravelEventType, TravelThreatLevel
from fu_gm.scene_orchestrator import SceneOrchestrator


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class WorldMapManagerTests(unittest.TestCase):
    def test_route_plan_uses_graph_edge_transport_multiplier_and_segment_threat(self) -> None:
        world = WorldState()
        world_map = WorldMapManager(world)
        world_map.add_location("雷尔德村", x=0, y=0, description="宁静村庄。", terrain="村庄")
        world_map.add_location("坎卡山", x=99, y=0, description="盗匪出没的高山矿道。", terrain="高山")
        world_map.add_route(
            "雷尔德村",
            "坎卡山",
            route_id="reald_to_kanka_mainroad",
            route_type="land",
            segments=[
                {"region": "巡逻草原", "distance_days": 3, "threat_level": "low"},
                {"region": "坎卡山", "distance_days": 2, "threat_level": "high"},
            ],
        )

        plan = world_map.plan_route("雷尔德村", "坎卡山", transport="飞行坐骑", party_size=3)

        self.assertEqual(plan.distance, 5)
        self.assertEqual(plan.travel_days, 2)
        self.assertEqual(plan.travel_multiplier, 3)
        self.assertEqual(plan.service_cost, 0)
        self.assertEqual(plan.threat_levels, [TravelThreatLevel.LOW, TravelThreatLevel.HIGH])
        self.assertEqual(plan.regions, ["巡逻草原", "坎卡山"])
        self.assertEqual(plan.route_source, "graph")
        self.assertEqual(plan.route_edge_ids, ["reald_to_kanka_mainroad"])
        self.assertIn("路线规划", plan.summary)
        self.assertIn("坎卡山", world.known_entity_names())

    def test_route_plan_requires_graph_edge_or_explicit_distance(self) -> None:
        world = WorldState()
        world_map = WorldMapManager(world)
        world_map.add_location("雷尔德村", x=0, y=0, description="宁静村庄。", terrain="村庄")
        world_map.add_location("坎卡山", x=5, y=0, description="盗匪出没的高山矿道。", terrain="高山")

        with self.assertRaisesRegex(ValueError, "路线网络"):
            world_map.plan_route("雷尔德村", "坎卡山")

        plan = world_map.plan_route("雷尔德村", "坎卡山", explicit_distance=2)

        self.assertEqual(plan.distance, 2)
        self.assertEqual(plan.route_source, "explicit")

    def test_orchestrator_uses_world_map_route_and_records_discovered_location(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="阿凛",
                attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=40,
                mp=40,
                traits=["pc"],
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([1, 5])
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world = WorldState()
        travel = TravelManager(rules)
        world_map = WorldMapManager(world)
        world_map.add_location("雷尔德村", x=0, y=0, description="宁静村庄。", terrain="村庄")
        world_map.add_location("月井遗迹", x=4, y=0, description="森林深处的古代遗迹。", terrain="森林")
        world_map.add_route(
            "雷尔德村",
            "月井遗迹",
            route_id="reald_to_moonwell",
            segments=[
                {"region": "月井遗迹", "distance_days": 2, "threat_level": "medium"},
                {"region": "月井遗迹", "distance_days": 2, "threat_level": "medium"},
            ],
        )
        app = SceneOrchestrator(
            character_manager=characters,
            clock_manager=clocks,
            conflict_manager=conflict,
            world_state=world,
            interceptor=ActionInterceptor(rules, characters, clocks, conflict, world),
            expressor=Expressor(),
            scene_manager=SceneManager(),
            travel_manager=travel,
            world_map_manager=world_map,
        )

        journey = app.travel(origin="雷尔德村", destination="月井遗迹", transport="地面坐骑")

        self.assertEqual(journey.distance, 4)
        self.assertEqual(journey.days, 2)
        self.assertEqual(journey.day_results[0].event_type, TravelEventType.DISCOVERY)
        self.assertIn("月井遗迹的林间路标", world.map_locations)
        self.assertTrue(any(event.kind == "route_plan" for event in world.memory_events))
        self.assertTrue(any(event.kind == "map_discovery" for event in world.memory_events))
        self.assertTrue(any(event.kind == "journey" for event in world.memory_events))

    def test_memory_store_persists_map_locations_and_routes(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        world = WorldState()
        world_map = WorldMapManager(world)
        world_map.add_location(
            "云海空港",
            x=7,
            y=2,
            description="走私商与旧飞艇停靠处。",
            terrain="云海",
            feature_type="settlement",
            position_hint="northwest",
            threat_level="medium",
        )
        world_map.add_location("银钟站", x=11, y=2, description="云海铁路旧站。", terrain="云海")
        world_map.add_route(
            "云海空港",
            "银钟站",
            route_id="skyport_to_bell_station",
            route_type="air",
            segments=[{"region": "云海航道", "distance_days": 2, "threat_level": "high"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = CampaignMemoryStore(Path(tmp))
            store.save_campaign(
                "map-test",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )
            loaded_world = WorldState()
            store.load_campaign(
                "map-test",
                world_state=loaded_world,
                character_manager=CharacterManager(),
                clock_manager=ClockManager(),
                conflict_manager=ConflictManager(CharacterManager()),
            )

        self.assertIn("云海空港", loaded_world.map_locations)
        self.assertEqual(loaded_world.map_locations["云海空港"].x, 7)
        self.assertEqual(loaded_world.map_locations["云海空港"].route_type.value, "air")
        self.assertEqual(loaded_world.map_locations["云海空港"].feature_type, "settlement")
        self.assertEqual(loaded_world.map_locations["云海空港"].position_hint, "northwest")
        self.assertIn("skyport_to_bell_station", loaded_world.map_routes)
        self.assertEqual(loaded_world.map_routes["skyport_to_bell_station"].segments[0].threat_level, TravelThreatLevel.HIGH)


if __name__ == "__main__":
    unittest.main()
