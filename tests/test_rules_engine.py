import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Bond, Character, Clock, StatusEffect


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class RulesEngineTests(unittest.TestCase):
    def test_equal_sixes_is_critical_success_and_grants_opportunity(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 6, "MIG": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])

        outcome = engine.roll_check(actor, ["DEX", "MIG"], target_number=20)

        self.assertTrue(outcome.critical_success)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.opportunity_count, 1)

    def test_roll_check_always_uses_exactly_two_canonical_attribute_dice(self) -> None:
        actor = Character(
            name="洛岚",
            attributes={"DEX": 8, "INS": 10, "MIG": 10, "WLP": 6},
            max_hp=55,
            hp=55,
            max_mp=35,
            mp=35,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([9, 8])

        outcome = engine.roll_check(actor, ["INS", "DEX", "敏捷", "洞察"], target_number=10)

        self.assertEqual(outcome.attributes, ["INS", "DEX"])
        self.assertEqual(outcome.dice, [(10, 9), (8, 8)])
        self.assertEqual(outcome.total, 17)

    def test_fumble_awards_fabula_point(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            fabula_points=0,
        )
        target = Character(
            name="帝国暗骑士",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=20,
            hp=20,
            max_mp=0,
            mp=0,
        )
        characters = CharacterManager()
        characters.add(actor)
        characters.add(target)
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        engine = RulesEngine()
        engine._rng = FakeRandom([1, 1])
        interceptor = ActionInterceptor(engine, characters, clocks, conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "瓦莉亚",
                    "attributes": ["DEX", "MIG"],
                    "target": "帝国暗骑士",
                    "target_number": 10,
                },
            )
        )

        self.assertTrue(resolution.payload["roll"].fumble)
        self.assertEqual(characters.get("瓦莉亚").fabula_points, 1)
        self.assertIn("fabula_gain", resolution.payload)

    def test_status_effect_reduces_attribute_die_but_not_below_d6(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            statuses=[StatusEffect.POISONED, StatusEffect.SLOW],
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 5])

        outcome = engine.roll_check(actor, ["DEX", "INS"], target_number=10)

        self.assertEqual(outcome.dice[0][0], 8)
        self.assertEqual(outcome.dice[1][0], 8)

    def test_poison_and_enraged_reduce_their_correct_attribute_pairs(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 10, "INS": 10, "MIG": 10, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            statuses=[StatusEffect.POISONED],
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([7, 7, 7, 7])

        physical = engine.roll_check(actor, ["DEX", "INS"], target_number=10)
        endurance = engine.roll_check(actor, ["MIG", "WLP"], target_number=10)

        self.assertEqual([die for die, _ in physical.dice], [10, 10])
        self.assertEqual([die for die, _ in endurance.dice], [8, 8])

        actor.statuses = [StatusEffect.ENRAGED]
        engine._rng = FakeRandom([7, 7, 7, 7])
        physical = engine.roll_check(actor, ["DEX", "INS"], target_number=10)
        endurance = engine.roll_check(actor, ["MIG", "WLP"], target_number=10)

        self.assertEqual([die for die, _ in physical.dice], [8, 8])
        self.assertEqual([die for die, _ in endurance.dice], [10, 10])

    def test_clock_progress_uses_margin_and_optional_critical_opportunity(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])

        outcome = engine.roll_check(actor, ["DEX", "MIG"], target_number=10)

        self.assertEqual(outcome.margin, 6)
        self.assertEqual(engine.clock_segments_from_roll(outcome), 3)
        self.assertEqual(engine.clock_segments_from_roll(outcome, spend_critical_opportunity=True), 5)

    def test_team_check_adds_successful_supports_and_highest_bond_strength_once(self) -> None:
        leader = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        supporter = Character(
            name="露琪亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            bonds=[Bond(target="瓦莉亚", emotions=["信赖", "钦佩"])],
        )
        second_supporter = Character(
            name="伊莎贝尔",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            bonds=[Bond(target="瓦莉亚", emotions=["喜爱"])],
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 4, 7, 3, 5, 5])

        outcome = engine.roll_team_check(
            leader=leader,
            supporters=[supporter, second_supporter],
            attributes=["DEX", "INS"],
            target_number=12,
        )

        self.assertEqual(outcome.support_bonus, 4)
        self.assertEqual(outcome.final_total, 12)
        self.assertTrue(outcome.success)

    def test_opposed_check_rerolls_on_tie(self) -> None:
        left = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        right = Character(
            name="帝国暗骑士",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 4, 5, 3, 7, 2, 2, 2])

        outcome = engine.roll_opposed_check(left, right, ["DEX", "INS"])

        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.winner, "瓦莉亚")

    def test_opposed_check_critical_beats_higher_total(self) -> None:
        left = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        right = Character(
            name="帝国暗骑士",
            attributes={"DEX": 12, "INS": 12},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6, 12, 11])

        outcome = engine.roll_opposed_check(left, right, ["DEX", "INS"])

        self.assertTrue(outcome.left_roll.critical_success)
        self.assertEqual(outcome.winner, "瓦莉亚")

    def test_opposed_check_rerolls_when_both_sides_crit(self) -> None:
        left = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        right = Character(
            name="帝国暗骑士",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6, 7, 7, 5, 4, 4, 3])

        outcome = engine.roll_opposed_check(left, right, ["DEX", "INS"])

        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.winner, "瓦莉亚")

    def test_guarding_adds_two_to_opposed_checks(self) -> None:
        left = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            guarding=True,
        )
        right = Character(
            name="帝国暗骑士",
            attributes={"DEX": 8, "INS": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([3, 3, 4, 3])

        outcome = engine.roll_opposed_check(left, right, ["DEX", "INS"])

        self.assertEqual(outcome.left_roll.modifier, 2)
        self.assertEqual(outcome.winner, "瓦莉亚")

    def test_crisis_uses_half_hp_floor(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 8},
            max_hp=45,
            hp=22,
            max_mp=20,
            mp=20,
        )
        self.assertTrue(actor.in_crisis)
        actor.hp = 23
        self.assertFalse(actor.in_crisis)

    def test_guard_counts_as_resistance_without_stacking_existing_resistance(self) -> None:
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=100,
            hp=100,
            max_mp=0,
            mp=0,
            affinities={"lightning": Affinity.RESIST},
            guarding=True,
        )
        engine = RulesEngine()

        damage, affinity = engine.compute_damage(7, 5, "lightning", target)

        self.assertEqual(affinity, Affinity.RESIST)
        self.assertEqual(damage, 6)

    def test_affinities_merge_by_rules_precedence(self) -> None:
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=100,
            hp=100,
            max_mp=0,
            mp=0,
            affinities={"fire": Affinity.WEAK},
            equipment_affinities={"fire": Affinity.RESIST},
        )
        engine = RulesEngine()

        damage, affinity = engine.compute_damage(7, 5, "fire", target)
        self.assertEqual(affinity, Affinity.NORMAL)
        self.assertEqual(damage, 12)

        damage, affinity = engine.compute_damage(7, 5, "fire", target, ignore_resist=True)
        self.assertEqual(affinity, Affinity.WEAK)
        self.assertEqual(damage, 24)

        target.temporary_affinities["fire"] = Affinity.ABSORB
        damage, affinity = engine.compute_damage(7, 5, "fire", target)
        self.assertEqual(affinity, Affinity.ABSORB)
        self.assertEqual(damage, -12)

    def test_failure_can_advance_threat_clock(self) -> None:
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
        )
        target = Character(
            name="帝国暗骑士",
            attributes={"DEX": 6, "MIG": 6},
            max_hp=20,
            hp=20,
            max_mp=0,
            mp=0,
        )
        characters = CharacterManager()
        characters.add(actor)
        characters.add(target)
        clocks = ClockManager()
        clocks.add(Clock(name="魔导炉过载", max_segments=6, current=1))
        conflict = ConflictManager(characters)
        engine = RulesEngine()
        engine._rng = FakeRandom([2, 2])
        interceptor = ActionInterceptor(engine, characters, clocks, conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "瓦莉亚",
                    "attributes": ["DEX", "MIG"],
                    "target": "帝国暗骑士",
                    "target_number": 12,
                    "threat_clock_name": "魔导炉过载",
                },
            )
        )

        self.assertIn("clock_change", resolution.payload)
        self.assertEqual(clocks.get("魔导炉过载").current, 4)

    def test_clock_manager_accepts_panel_formatted_names(self) -> None:
        clocks = ClockManager()
        clocks.add(Clock(name="开启歌唱宝箱", max_segments=6, current=0))

        self.assertTrue(clocks.exists("[开启歌唱宝箱]"))
        self.assertTrue(clocks.exists("【开启歌唱宝箱】"))
        before, after = clocks.advance("[开启歌唱宝箱] 0/6", 2)

        self.assertEqual((before, after), (0, 2))
        self.assertEqual(clocks.get("开启歌唱宝箱").current, 2)

    def test_advance_clock_can_create_gm_clock(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        interceptor = ActionInterceptor(RulesEngine(), characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ADVANCE_CLOCK,
                parameters={
                    "clock_name": "警报响彻遗迹",
                    "max_segments": 4,
                    "delta": 0,
                    "clock_type": "threat",
                    "stakes": "填满后守卫包围入口。",
                    "reason": "GM 判断玩家拖延过久，公开建立威胁命刻。",
                },
            )
        )

        clock = clocks.get("警报响彻遗迹")
        self.assertEqual(clock.max_segments, 4)
        self.assertEqual(clock.current, 0)
        self.assertEqual(clock.clock_type, "threat")
        self.assertIn("创建", resolution.rules_text)


if __name__ == "__main__":
    unittest.main()
