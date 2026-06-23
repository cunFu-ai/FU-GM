import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Bond, Character, Clock, EnemyRank, StatusEffect


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


def make_character(name: str, traits: list[str], **overrides) -> Character:
    defaults = {
        "attributes": {"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        "max_hp": 60,
        "hp": 60,
        "max_mp": 40,
        "mp": 40,
        "fabula_points": 3,
        "traits": traits,
        "abilities": [
            "可装备职业近战武器",
            "可装备职业远程武器",
            "可装备职业盔甲",
            "可装备职业盾牌",
        ],
    }
    defaults.update(overrides)
    return Character(name=name, **defaults)


class PriorityRuleClosureTests(unittest.TestCase):
    def make_interceptor(self, rng_values=None):
        characters = CharacterManager()
        rules = RulesEngine()
        if rng_values is not None:
            rules._rng = FakeRandom(rng_values)
        clocks = ClockManager()
        world = WorldState()
        conflict = ConflictManager(characters)
        interceptor = ActionInterceptor(rules, characters, clocks, conflict, world)
        return interceptor, characters, rules, clocks, conflict, world

    def test_trait_invocation_rerolls_pending_check_and_spends_fabula(self) -> None:
        interceptor, characters, rules, _, _, _ = self.make_interceptor([2, 3, 8])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))

        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 12, "non_damage": True},
            )
        )
        self.assertFalse(first.payload["roll"].success)

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {"actor": "赛璃", "trait_name": "怜悯", "reroll_indices": [1]},
            )
        )
        self.assertEqual(invoked.payload["roll"].total, 11)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)

    def test_bond_invocation_adds_strength_once_and_spends_fabula(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([4, 4])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                bonds=[Bond("白河", ["信赖", "喜爱"])],
            )
        )

        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 10, "non_damage": True},
            )
        )
        self.assertFalse(first.payload["roll"].success)

        invoked = interceptor.resolve(
            Action(ActionType.INVOKE_BOND, {"actor": "赛璃", "bond_target": "白河"})
        )
        self.assertTrue(invoked.payload["roll"].success)
        self.assertEqual(invoked.payload["bond_strength"], 2)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)

    def test_opportunity_progress_bond_suffer_and_advantage_are_hard_rules(self) -> None:
        interceptor, characters, _, clocks, conflict, _ = self.make_interceptor([3, 4])
        characters.add(make_character("阿凛", ["pc"]))
        characters.add(make_character("财团机兵", ["enemy"]))
        clocks.add(Clock("封住裂隙", 6))

        progress = interceptor.resolve(
            Action(ActionType.TRIGGER_OPPORTUNITY, {"actor": "阿凛", "effect": "进展", "clock_name": "封住裂隙"})
        )
        self.assertEqual(progress.payload["clock_change"].delta, 2)
        self.assertEqual(clocks.get("封住裂隙").current, 2)

        bond = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {"actor": "阿凛", "effect": "纽带", "target": "赛璃", "emotions": ["信赖", "喜爱"]},
            )
        )
        self.assertEqual(bond.payload["bond"].strength, 2)

        suffer = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {"actor": "阿凛", "effect": "受苦", "target": "财团机兵", "status_effect": "动摇"},
            )
        )
        self.assertEqual(suffer.payload["status"], StatusEffect.SHAKEN)
        self.assertIn(StatusEffect.SHAKEN, characters.get("财团机兵").statuses)

        interceptor.resolve(
            Action(ActionType.TRIGGER_OPPORTUNITY, {"actor": "阿凛", "effect": "优势", "target": "阿凛"})
        )
        advantaged = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "阿凛", "attributes": ["DEX", "INS"], "target_number": 11, "non_damage": True},
            )
        )
        self.assertEqual(advantaged.payload["advantage_bonus"], 4)
        self.assertTrue(advantaged.payload["roll"].success)

    def test_equip_action_and_sell_item_apply_inventory_rules(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor()
        characters.add(
            make_character(
                "洛岚",
                ["pc"],
                equipment=["青铜剑", "青铜盾", "旅行装束"],
                zenit=0,
            )
        )
        conflict.start_scene("装备测试", ["洛岚"])

        equipped = interceptor.resolve(
            Action(ActionType.EQUIP, {"actor": "洛岚", "items": ["青铜剑", "青铜盾"]})
        )
        self.assertEqual(characters.get("洛岚").equipped_main_hand, "青铜剑")
        self.assertEqual(characters.get("洛岚").equipped_shield, "青铜盾")
        self.assertEqual(equipped.payload["equipped_items"], ["青铜剑", "青铜盾"])

        with self.assertRaises(ValueError):
            interceptor.resolve(Action(ActionType.EQUIP, {"actor": "洛岚", "items": ["旅行装束"]}))

        sold = interceptor.resolve(Action(ActionType.SELL_ITEM, {"actor": "洛岚", "item_name": "青铜盾"}))
        self.assertEqual(sold.payload["transaction"].total_cost, -50)
        self.assertEqual(characters.get("洛岚").zenit, 50)
        self.assertNotIn("青铜盾", characters.get("洛岚").equipment)

    def test_start_conflict_rolls_initiative_and_awards_villain_appearance_once(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor([6, 6, 5, 5])
        characters.add(make_character("阿凛", ["pc"], initiative=0))
        characters.add(make_character("赛璃", ["pc"], initiative=0))
        characters.add(make_character("黑日将军", ["enemy", "villain"], initiative=10))

        started = interceptor.resolve(
            Action(
                ActionType.START_CONFLICT,
                {
                    "scene_name": "黑日初见",
                    "pcs": ["阿凛", "赛璃"],
                    "enemies": ["黑日将军"],
                    "leader": "阿凛",
                    "supporters": ["赛璃"],
                    "ultima_points": 3,
                },
            )
        )
        self.assertTrue(started.payload["players_first"])
        self.assertEqual(conflict.state.turn_order, ["阿凛", "黑日将军", "赛璃"])
        self.assertEqual(characters.get("阿凛").fabula_points, 4)
        self.assertEqual(characters.get("赛璃").fabula_points, 4)
        second_award = conflict.award_villain_appearance_fabula("黑日将军")
        self.assertEqual(second_award.event_type, "villain_appearance_already_awarded")

    def test_conflict_teamwork_consumes_supporter_turn_and_adds_highest_bond(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor([4, 4])
        characters.add(make_character("阿凛", ["pc"]))
        characters.add(make_character("赛璃", ["pc"], bonds=[Bond("阿凛", ["信赖", "喜爱"])]))
        characters.add(make_character("财团机兵", ["enemy"]))
        conflict.start_scene("协作测试", ["阿凛", "财团机兵", "赛璃"])

        roll = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "阿凛",
                    "attributes": ["MIG", "WLP"],
                    "target_number": 11,
                    "non_damage": True,
                    "supporters": ["赛璃"],
                },
            )
        )
        self.assertTrue(roll.payload["roll"].success)
        self.assertEqual(roll.payload["conflict_teamwork"]["total_bonus"], 3)
        self.assertEqual(conflict.state.action_penalties.get("赛璃"), 1)

    def test_bond_manager_enforces_limit_opposites_and_self_bond(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor()
        characters.add(make_character("伊莉雅", ["pc"]))

        bond = interceptor.resolve(
            Action(
                ActionType.MANAGE_BOND,
                {"actor": "伊莉雅", "target": "赛璃", "emotions": ["信赖", "猜忌", "喜爱"]},
            )
        ).payload["bond"]
        self.assertEqual(bond.emotions, ["猜忌", "喜爱"])

        with self.assertRaises(ValueError):
            interceptor.resolve(
                Action(ActionType.MANAGE_BOND, {"actor": "伊莉雅", "target": "伊莉雅", "emotions": ["信赖"]})
            )

        for index in range(2, 7):
            interceptor.resolve(
                Action(
                    ActionType.MANAGE_BOND,
                    {"actor": "伊莉雅", "target": f"对象{index}", "emotions": ["信赖"]},
                )
            )
        with self.assertRaises(ValueError):
            interceptor.resolve(
                Action(ActionType.MANAGE_BOND, {"actor": "伊莉雅", "target": "第七人", "emotions": ["信赖"]})
            )

    def test_pvp_requires_consent_before_opposed_roll(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([5, 5, 4, 4])
        characters.add(make_character("阿凛", ["pc"]))
        characters.add(make_character("洛岚", ["pc"]))

        blocked = interceptor.resolve(Action(ActionType.PLAYER_VS_PLAYER, {"actor": "阿凛", "target": "洛岚"}))
        self.assertTrue(blocked.payload["requires_consent"])

        resolved = interceptor.resolve(
            Action(
                ActionType.PLAYER_VS_PLAYER,
                {"actor": "阿凛", "target": "洛岚", "consent_confirmed": True},
            )
        )
        self.assertIn(resolved.payload["opposed_check"].winner, {"阿凛", "洛岚"})

    def test_session_zero_world_fact_cleaning_separates_vote_and_villain_seed_noise(self) -> None:
        world = WorldState()
        manager = SessionZeroManager(world)
        manager.start(participants=["白河"])
        manager.apply_world_updates(
            {
                "mysteries": [
                    "每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写；我投这个第一幕。额外补一个反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民。"
                ],
                "villain_seeds": ["我投这个第一幕。额外补一个反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民。"],
            }
        )
        self.assertEqual(manager.state.world.mysteries, ["每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写"])
        self.assertEqual(manager.state.world.villain_seeds, ["第七采掘城的监察官艾蕾娜曾是赤羽遗民"])


if __name__ == "__main__":
    unittest.main()
