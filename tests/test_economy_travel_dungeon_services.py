import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.economy_manager import EconomyManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Character,
    DungeonImportance,
    DungeonPreparation,
    PersistentChangeType,
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


class EconomyTravelDungeonServiceTests(unittest.TestCase):
    def test_lodging_and_travel_service_charge_zenit_by_party_size_and_days(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=500))
        economy = EconomyManager(characters, WorldState(), RulesEngine(seed=1))

        lodging = economy.buy_lodging("阿凛", settlement_size="village", party_size=3)
        travel = economy.pay_travel_service("阿凛", "空中旅行服务", days=2, party_size=3)

        self.assertEqual(lodging.total_cost, 15)
        self.assertEqual(travel.total_cost, 240)
        self.assertEqual(characters.get("阿凛").zenit, 245)

    def test_buy_transport_records_party_asset_and_travel_manager_can_enforce_ownership(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=1000))
        world = WorldState()
        economy = EconomyManager(characters, world, RulesEngine(seed=1))
        travel = TravelManager(RulesEngine(seed=1))

        with self.assertRaises(ValueError):
            travel.travel(
                origin="村庄",
                destination="古堡",
                threat_levels=[TravelThreatLevel.LOW],
                transport="地面载具",
                enforce_owned_transport=True,
            )

        purchase = economy.buy_transport("阿凛", "地面载具")
        travel.register_owned_transport(purchase.transport_name)
        result = travel.travel(
            origin="村庄",
            destination="古堡",
            threat_levels=[TravelThreatLevel.LOW],
            transport="地面载具",
            enforce_owned_transport=True,
        )

        self.assertEqual(purchase.total_cost, 600)
        self.assertEqual(characters.get("阿凛").zenit, 400)
        self.assertEqual(world.persistent_changes[0].change_type, PersistentChangeType.TRANSPORT)
        self.assertEqual(result.transport, "地面载具")

    def test_shop_action_routes_lodging_travel_service_and_transport_purchase(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=1200))
        world = WorldState()
        interceptor = ActionInterceptor(
            RulesEngine(seed=1),
            characters,
            ClockManager(),
            ConflictManager(characters),
            world,
        )

        lodging = interceptor.resolve(
            Action(ActionType.SHOP, {"actor": "阿凛", "mode": "lodging", "settlement_size": "city", "party_size": 2})
        )
        service = interceptor.resolve(
            Action(
                ActionType.SHOP,
                {"actor": "阿凛", "mode": "travel_service", "transport": "陆地旅行服务", "days": 3, "party_size": 2},
            )
        )
        transport = interceptor.resolve(
            Action(ActionType.SHOP, {"actor": "阿凛", "mode": "buy_transport", "transport": "地面坐骑"})
        )

        self.assertIn("40Z", lodging.rules_text)
        self.assertIn("60Z", service.rules_text)
        self.assertIn("200Z", transport.rules_text)
        self.assertEqual(characters.get("阿凛").zenit, 900)

        expressor = Expressor()
        self.assertIn("阿凛 的资金：1200Z -> 1160Z。", expressor.render(lodging))
        self.assertIn("阿凛 的资金：1160Z -> 1100Z。", expressor.render(service))
        self.assertIn("阿凛 的资金：1100Z -> 900Z。", expressor.render(transport))

    def test_dungeon_reward_plan_places_budgeted_rewards_and_opening_area_awards_them(self) -> None:
        characters = CharacterManager()
        characters.add(self.hero("阿凛", zenit=0))
        world = WorldState()
        rules = RulesEngine()
        rules._rng = FakeRandom([1])
        clocks = ClockManager()
        dungeon = DungeonManager(clocks, RulesEngine(seed=1))
        brief = dungeon.design_dungeon(
            "星落地下城",
            importance=DungeonImportance.MAJOR,
            preparation=DungeonPreparation.PREPARED,
            rolls={"concept": 13, "focus": 18, "inhabitants": 17, "peculiarity": 15},
        )
        state = dungeon.start_from_brief(brief)
        economy = EconomyManager(characters, world, rules)
        placements = economy.plan_dungeon_rewards(state, party_level=5, pc_count=3, rare_items=["银爪"])
        interceptor = ActionInterceptor(
            RulesEngine(seed=1),
            characters,
            clocks,
            ConflictManager(characters),
            world,
            dungeon_manager=dungeon,
            economy_manager=economy,
        )

        result = interceptor.resolve(
            Action(
                ActionType.EXPLORE_DUNGEON,
                {
                    "actor": "阿凛",
                    "area_name": placements[0].area_name,
                    "mode": "open_treasure",
                    "collect_treasure": True,
                },
            )
        )

        placement = placements[0]
        self.assertGreater(placement.reward_zenit, 0)
        self.assertIn("地下城奖励硬配置", placement.hard_rule_summary)
        self.assertIn("GM 私密奖励配置", placement.llm_narrative_prompt)
        self.assertEqual(result.payload["chest_reward"].zenit, placement.reward_zenit)
        self.assertIn(placement.reward_item, characters.get("阿凛").equipment)
        self.assertEqual(characters.get("阿凛").zenit, placement.reward_zenit)

    def hero(self, name: str, *, zenit: int = 0) -> Character:
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
            zenit=zenit,
        )


if __name__ == "__main__":
    unittest.main()
