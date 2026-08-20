import tempfile
import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Bond,
    Character,
    Clock,
    DecisionWindowStatus,
    EnemyRank,
    RollOutcome,
    StatusEffect,
)


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
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃不愿让眼前的人因自己的失败受苦。",
                    "reroll_indices": [1],
                },
            )
        )
        self.assertEqual(invoked.payload["roll"].total, 11)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)

    def test_selected_reroll_window_is_resolved_and_siblings_expire(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor([2, 3, 8])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                theme="怜悯",
                bonds=[Bond("白河", ["信赖"])],
            )
        )
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 12, "non_damage": True},
            )
        )
        trait_window = next(
            window for window in first.payload["post_check_windows"] if window["kind"] == "trait_invocation"
        )
        source_ids = {window["window_id"] for window in first.payload["post_check_windows"]}

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃以怜悯为先，不能放任这次失败伤及他人。",
                    "reroll_indices": [1],
                    "window_id": trait_window["window_id"],
                },
            )
        )

        self.assertEqual(world.decision_windows[trait_window["window_id"]].status, DecisionWindowStatus.RESOLVED)
        sibling_statuses = {
            world.decision_windows[window_id].status
            for window_id in source_ids
            if window_id != trait_window["window_id"]
        }
        self.assertEqual(sibling_statuses, {DecisionWindowStatus.EXPIRED})
        self.assertTrue(interceptor.decision_window_manager.pending(kind="trait_invocation", owner="赛璃"))
        self.assertTrue(interceptor.decision_window_manager.pending(kind="bond_invocation", owner="赛璃"))
        rendered = Expressor().render(invoked)
        self.assertIn("结算值 11", rendered)
        self.assertNotIn("从 5 变为 11", rendered)
        self.assertNotIn("赛璃以怜悯为先", rendered)
        self.assertNotIn("【叙事】", rendered)

    def test_trait_can_be_invoked_repeatedly_before_result_is_final(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([2, 3, 4, 8])
        characters.add(
            make_character(
                "赛璃",
                ["pc", "不应成为可援用特质的内部标签"],
                theme="怜悯",
                identity="守望者",
                fabula_points=3,
            )
        )
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        first_window = next(
            window
            for window in first.payload["post_check_windows"]
            if window["kind"] == "trait_invocation"
        )
        self.assertNotIn(
            {"trait": "不应成为可援用特质的内部标签"},
            first_window["options"],
        )

        rerolled_once = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃不愿因这次失手让他人承担后果。",
                    "reroll_indices": [0],
                    "window_id": first_window["window_id"],
                },
            )
        )
        self.assertTrue(rerolled_once.payload["check_result_provisional"])
        self.assertEqual(rerolled_once.payload["roll"].total, 7)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)
        second_window = interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="赛璃",
            blocking_only=True,
        )[0]

        rerolled_twice = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "reroll_indices": [1],
                    "reroll_index_base": 0,
                    "window_id": second_window.window_id,
                },
            )
        )
        self.assertFalse(rerolled_twice.payload.get("check_result_provisional"))
        self.assertTrue(rerolled_twice.payload["roll"].success)
        self.assertEqual(rerolled_twice.payload["roll"].total, 12)
        self.assertEqual(characters.get("赛璃").fabula_points, 1)
        self.assertEqual(
            len(rerolled_twice.payload["check_transaction_invocation_history"]),
            2,
        )

        self.assertFalse(
            interceptor.decision_window_manager.pending(
                kind="trait_invocation",
                owner="赛璃",
                blocking_only=True,
            )
        )
        self.assertEqual(characters.get("赛璃").fabula_points, 1)

    def test_bond_invocation_is_available_only_once_per_check(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([2, 3])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                theme="怜悯",
                fabula_points=3,
                bonds=[Bond("白河", ["信赖", "喜爱"])],
            )
        )
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        bond_window = next(
            window
            for window in first.payload["post_check_windows"]
            if window["kind"] == "bond_invocation"
        )

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_BOND,
                {
                    "actor": "赛璃",
                    "bond_target": "白河",
                    "window_id": bond_window["window_id"],
                },
            )
        )

        self.assertTrue(invoked.payload["check_result_provisional"])
        self.assertFalse(
            interceptor.decision_window_manager.pending(
                kind="bond_invocation",
                owner="赛璃",
            )
        )
        self.assertTrue(
            interceptor.decision_window_manager.pending(
                kind="trait_invocation",
                owner="赛璃",
                blocking_only=True,
            )
        )
        self.assertEqual(characters.get("赛璃").fabula_points, 2)

    def test_failed_trait_invocation_hydrates_after_runtime_state_is_lost(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor([2, 3, 8])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 12, "non_damage": True},
            )
        )
        trait_window = next(
            window for window in first.payload["post_check_windows"] if window["kind"] == "trait_invocation"
        )
        persisted = world.decision_windows[trait_window["window_id"]]
        self.assertTrue(persisted.payload["portable_check_resume"])
        self.assertEqual(persisted.resume_point, "post_check")
        self.assertTrue(persisted.transaction_id)

        interceptor.post_check_state.rolls.clear()
        interceptor.post_check_state.clock_checks.clear()
        interceptor.pending_check_transactions.clear()

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃希望保护眼前受困的人。",
                    "reroll_indices": [1],
                    "window_id": persisted.window_id,
                },
            )
        )

        self.assertTrue(invoked.payload["check_transaction_replayed"])
        self.assertNotIn("赛璃希望保护眼前受困的人", invoked.rules_text)
        self.assertNotIn("旧结果已回滚", invoked.rules_text)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)
        self.assertEqual(
            world.decision_windows[persisted.window_id].status,
            DecisionWindowStatus.RESOLVED,
        )

    def test_failed_check_transaction_survives_campaign_save_and_load(self) -> None:
        interceptor, characters, _, clocks, conflict, world = self.make_interceptor([2, 3])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 12, "non_damage": True},
            )
        )
        trait_window_id = next(
            window["window_id"]
            for window in first.payload["post_check_windows"]
            if window["kind"] == "trait_invocation"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            store.save_campaign(
                "post-check-resume",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
            )

            resumed, resumed_characters, _, resumed_clocks, resumed_conflict, resumed_world = self.make_interceptor([8])
            store.load_campaign(
                "post-check-resume",
                world_state=resumed_world,
                character_manager=resumed_characters,
                clock_manager=resumed_clocks,
                conflict_manager=resumed_conflict,
            )
            restored_window = resumed_world.decision_windows[trait_window_id]
            self.assertIsInstance(restored_window.payload["source_action"], dict)
            self.assertIsInstance(restored_window.payload["source_roll"], dict)

            invoked = resumed.resolve(
                Action(
                    ActionType.INVOKE_TRAIT,
                    {
                        "actor": "赛璃",
                        "trait_name": "怜悯",
                        "invocation_rationale": "赛璃不愿让同伴因这次失败陷入险境。",
                        "reroll_indices": [1],
                        "window_id": trait_window_id,
                    },
                )
            )

        self.assertTrue(invoked.payload["check_transaction_replayed"])
        self.assertEqual(resumed_characters.get("赛璃").fabula_points, 2)
        self.assertEqual(
            resumed_world.decision_windows[trait_window_id].status,
            DecisionWindowStatus.RESOLVED,
        )

    def test_successful_check_commits_with_silent_nonblocking_invocation_right(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor([5, 6])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        result = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 10, "non_damage": True},
            )
        )
        self.assertTrue(result.payload["roll"].success)
        self.assertFalse(result.payload.get("check_result_provisional"))
        windows = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(windows), 1)
        self.assertFalse(windows[0].blocking)
        self.assertTrue(windows[0].payload["silent_success_invocation"])
        self.assertFalse(interceptor.decision_window_manager.public_summary())
        self.assertIn(windows[0].window_id, world.decision_windows)

    def test_reroll_preserves_new_critical_opportunity_window(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([2, 3, 6, 6])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        first = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 12, "non_damage": True},
            )
        )
        trait_window = next(
            window for window in first.payload["post_check_windows"] if window["kind"] == "trait_invocation"
        )

        rerolled = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃为了不让无辜者承担失败后果而再次尝试。",
                    "reroll_indices": [0, 1],
                    "reroll_index_base": 0,
                    "window_id": trait_window["window_id"],
                },
            )
        )

        self.assertFalse(rerolled.payload.get("check_result_provisional"))
        self.assertTrue(interceptor.decision_window_manager.pending(kind="critical_opportunity", owner="赛璃"))
        continuing = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(continuing), 1)
        self.assertFalse(continuing[0].blocking)
        self.assertTrue(continuing[0].payload["continuing_trait_invocation"])
        self.assertTrue(continuing[0].payload["suppress_public_prompt"])

    def test_lucky_seven_is_a_blocking_pre_final_choice(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([4, 5])
        characters.add(make_character("赛璃", ["pc"], skills={"幸运七": 1}))

        provisional = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )

        lucky_window = next(
            window
            for window in provisional.payload["post_check_windows"]
            if window["kind"] == "skill_judgement" and window["label"] == "幸运七"
        )
        self.assertTrue(provisional.payload["check_result_provisional"])
        self.assertTrue(lucky_window["blocking"])
        self.assertIn("赛璃", interceptor.pending_check_transactions)

        resolved = interceptor.resolve(
            Action(
                ActionType.SKILL,
                {
                    "actor": "赛璃",
                    "skill_name": "幸运七",
                    "die_index": 1,
                    "window_id": lucky_window["window_id"],
                },
            )
        )

        self.assertTrue(resolved.payload["check_transaction_replayed"])
        self.assertEqual(resolved.payload["roll"].total, 12)
        self.assertFalse(
            interceptor.decision_window_manager.pending(
                kind="skill_judgement",
                owner="赛璃",
            )
        )

    def test_fumble_opportunity_is_persisted_for_gm_without_becoming_player_choice(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([1, 1])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))

        result = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 10, "non_damage": True},
            )
        )

        self.assertTrue(result.payload["roll"].fumble)
        self.assertEqual(result.payload["gm_post_check_windows"][0]["kind"], "fumble_opportunity")
        pending = interceptor.decision_window_manager.pending(
            kind="fumble_opportunity",
            owner="__gm__",
        )
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].blocking)
        self.assertEqual(pending[0].payload["source_actor"], "赛璃")
        self.assertFalse(interceptor.decision_window_manager.awaiting_player_response(owner="赛璃"))
        self.assertEqual(characters.get("赛璃").fabula_points, 4)

    def test_npc_fumble_gives_opportunity_to_target_pc_without_fabula_gain(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([1, 1])
        characters.add(make_character("两名看守", ["enemy"], fabula_points=0))
        characters.add(make_character("艾丽妮", ["pc"]))

        result = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "两名看守",
                    "target": "艾丽妮",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )

        self.assertTrue(result.payload["roll"].fumble)
        self.assertNotIn("fabula_gain", result.payload)
        self.assertEqual(characters.get("两名看守").fabula_points, 0)
        pending = interceptor.decision_window_manager.pending(
            kind="fumble_opportunity",
            owner="艾丽妮",
        )
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].blocking)
        self.assertEqual(pending[0].payload["source_actor"], "两名看守")
        self.assertFalse(
            interceptor.decision_window_manager.pending(
                kind="fumble_opportunity",
                owner="__gm__",
            )
        )

    def test_npc_critical_opportunity_is_controlled_by_gm(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([8, 8])
        characters.add(make_character("两名看守", ["enemy"], fabula_points=0))
        characters.add(make_character("艾丽妮", ["pc"]))

        result = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "两名看守",
                    "target": "艾丽妮",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )

        self.assertTrue(result.payload["roll"].critical_success)
        pending = interceptor.decision_window_manager.pending(
            kind="critical_opportunity",
            owner="__gm__",
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(result.payload["gm_post_check_windows"][0]["owner"], "__gm__")

    def test_scene_object_investigation_keeps_roll_for_trait_invocation(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([2, 3, 8])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))

        first = interceptor.resolve(
            Action(
                ActionType.INVESTIGATE,
                {
                    "actor": "赛璃",
                    "target": "门缝里的灰晶粉末",
                    "attributes": ["INS", "INS"],
                    "target_number": 12,
                },
            )
        )

        self.assertFalse(first.payload["roll"].success)
        self.assertEqual(interceptor.post_check_state.rolls["赛璃"], first.payload["roll"])
        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃无法坐视眼前的人因失败受苦。",
                    "reroll_indices": [1],
                },
            )
        )
        self.assertEqual(invoked.payload["roll"].total, 11)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)

    def test_scene_object_investigation_keeps_teamwork_and_advantage(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor([2, 3])
        characters.add(make_character("赛璃", ["pc"]))
        characters.add(make_character("洛岚", ["pc"]))
        characters.add(make_character("财团机兵", ["enemy"]))
        conflict.start_scene(
            "协作调查",
            ["赛璃", "财团机兵", "洛岚"],
        )
        interceptor.post_check_state.grant_advantage("赛璃", 4)

        result = interceptor.resolve(
            Action(
                ActionType.INVESTIGATE,
                {
                    "actor": "赛璃",
                    "target": "炉心纹路",
                    "attributes": ["INS", "INS"],
                    "target_number": 10,
                    "teamwork_supporters": ["洛岚"],
                },
            )
        )

        self.assertTrue(result.payload["roll"].success)
        self.assertEqual(result.payload["roll"].modifier, 5)
        self.assertEqual(result.payload["roll"].total, 10)
        self.assertEqual(result.payload["advantage_bonus"], 4)
        self.assertEqual(
            result.payload["conflict_teamwork"]["total_bonus"],
            1,
        )
        self.assertEqual(conflict.pending_assists_for("赛璃"), [])
        self.assertIn("洛岚", conflict.state.acted_this_round)
        self.assertIn("机会【优势】提供 +4 修正", result.rules_text)
        self.assertIn("团队合作提供 +1 修正", result.rules_text)

    def test_scene_object_investigation_keeps_critical_opportunity_window(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([6, 6])
        characters.add(make_character("赛璃", ["pc"]))

        result = interceptor.resolve(
            Action(
                ActionType.INVESTIGATE,
                {
                    "actor": "赛璃",
                    "target": "炉心纹路",
                    "attributes": ["INS", "INS"],
                    "target_number": 13,
                },
            )
        )

        self.assertTrue(result.payload["roll"].critical_success)
        self.assertIn(
            "critical_opportunity",
            {
                window["kind"]
                for window in result.payload["post_check_windows"]
            },
        )
        self.assertTrue(
            interceptor.decision_window_manager.pending(
                kind="critical_opportunity",
                owner="赛璃",
            )
        )

    def test_archived_pressure_clock_is_not_recreated_by_later_investigation(self) -> None:
        interceptor, characters, _, clocks, _, _ = self.make_interceptor([4, 4])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(Clock(name="巡逻队逼近", max_segments=6, current=6, clock_type="threat"))
        clocks.resolve("巡逻队逼近", note="巡逻队已经包围现场", archive=True)

        resolution = interceptor.resolve(
            Action(
                ActionType.INVESTIGATE,
                {
                    "actor": "赛璃",
                    "target": "门外脚步",
                    "attributes": ["INS", "INS"],
                    "target_number": 7,
                    "establish_threat_clock_name": "巡逻队逼近",
                    "establish_threat_clock_segments": 6,
                },
            )
        )

        self.assertNotIn("clock_change", resolution.payload)
        self.assertFalse(clocks.exists("巡逻队逼近"))
        self.assertTrue(clocks.is_retired("巡逻队逼近"))

    def test_successful_attack_commits_damage_with_hidden_trait_right(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([6, 5, 1])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯", weapon_damage=6))
        characters.add(make_character("黑甲兵", ["npc"]))

        first = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "赛璃",
                    "target": "黑甲兵",
                    "attributes": ["DEX", "MIG"],
                    "target_number": 10,
                },
            )
        )
        self.assertTrue(first.payload["roll"].success)
        self.assertFalse(first.payload.get("check_result_provisional"))
        self.assertLess(characters.get("黑甲兵").hp, 60)
        trait_windows = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(trait_windows), 1)
        self.assertFalse(trait_windows[0].blocking)
        self.assertFalse(interceptor.decision_window_manager.public_summary())
        self.assertEqual(characters.get("赛璃").fabula_points, 3)

    def test_successful_hinder_commits_status_with_hidden_trait_right(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([6, 5, 1])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        characters.add(make_character("黑甲兵", ["npc"]))

        first = interceptor.resolve(
            Action(
                ActionType.HINDER,
                {
                    "actor": "赛璃",
                    "target": "黑甲兵",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "status_effect": StatusEffect.SHAKEN.value,
                },
            )
        )
        self.assertTrue(first.payload["roll"].success)
        self.assertFalse(first.payload.get("check_result_provisional"))
        self.assertIn(StatusEffect.SHAKEN, characters.get("黑甲兵").statuses)
        trait_windows = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(trait_windows), 1)
        self.assertFalse(trait_windows[0].blocking)

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

    def test_trait_invocation_recommits_objective_clock_from_new_result(self) -> None:
        interceptor, characters, _, clocks, _, _ = self.make_interceptor([2, 3, 8])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(Clock(name="打开旧门", max_segments=6, clock_type="objective"))

        first = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "赛璃",
                    "target": "打开旧门",
                    "clock_name": "打开旧门",
                    "clock_direction": 1,
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                },
            )
        )
        self.assertFalse(first.payload["roll"].success)
        self.assertEqual(clocks.get("打开旧门").current, 0)

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃以怜悯驱使自己再作尝试。",
                    "reroll_indices": [1],
                },
            )
        )

        self.assertTrue(invoked.payload["roll"].success)
        self.assertTrue(invoked.payload["clock_reconciled"])
        self.assertEqual(clocks.get("打开旧门").current, 1)

    def test_failed_threat_check_waits_for_post_check_choice_before_committing(self) -> None:
        interceptor, characters, _, clocks, _, _ = self.make_interceptor([1, 2])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(
            Clock(
                name="巡逻队包围",
                max_segments=6,
                current=5,
                clock_type="threat",
                completion_consequence="巡逻队包围现场。",
            )
        )

        provisional = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "赛璃",
                    "target": "拖慢巡逻队",
                    "clock_name": "巡逻队包围",
                    "threat_clock_name": "巡逻队包围",
                    "clock_direction": -1,
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                },
            )
        )

        self.assertTrue(provisional.payload["check_result_provisional"])
        self.assertEqual(clocks.get("巡逻队包围").current, 5)
        self.assertTrue(interceptor.decision_window_manager.pending(blocking_only=True))
        self.assertIn("赛璃", interceptor.post_check_state.rolls)
        self.assertNotIn("巡逻队包围现场", Expressor().render(provisional))
        window_id = interceptor.decision_window_manager.pending(
            owner="赛璃",
            blocking_only=True,
        )[0].window_id
        interceptor.post_check_state.rolls.clear()
        interceptor.post_check_state.clock_checks.clear()
        interceptor.pending_check_transactions.clear()

        accepted = interceptor.resolve(
            Action(
                ActionType.NARRATE,
                {
                    "actor": "赛璃",
                    "summary": "接受这次失败。",
                    "scene_clarification": True,
                    "post_check_acceptance": True,
                    "window_id": window_id,
                },
            )
        )

        self.assertTrue(accepted.payload["check_transaction_accepted"])
        self.assertEqual(clocks.get("巡逻队包围").current, 6)
        self.assertIn("巡逻队包围", accepted.rules_text)

    def test_blocking_post_check_window_rejects_unrelated_action_without_losing_roll(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([1, 2])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        characters.add(make_character("洛岚", ["pc"], theme="赎罪"))

        interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )

        with self.assertRaisesRegex(ValueError, "先由【赛璃】处理"):
            interceptor.resolve(
                Action(
                    ActionType.REQUEST_ROLL,
                    {
                        "actor": "洛岚",
                        "attributes": ["INS", "INS"],
                        "target_number": 7,
                    },
                )
            )

        self.assertIn("赛璃", interceptor.post_check_state.rolls)
        self.assertTrue(interceptor.decision_window_manager.pending(owner="赛璃", blocking_only=True))

    def test_successful_objective_commits_clock_with_hidden_trait_right(self) -> None:
        interceptor, characters, _, clocks, _, _ = self.make_interceptor([6, 5, 1])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(Clock(name="打开旧门", max_segments=6, clock_type="objective"))

        first = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "赛璃",
                    "target": "打开旧门",
                    "clock_name": "打开旧门",
                    "clock_direction": 1,
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                },
            )
        )
        self.assertTrue(first.payload["roll"].success)
        self.assertFalse(first.payload.get("check_result_provisional"))
        self.assertEqual(clocks.get("打开旧门").current, 1)
        trait_windows = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(trait_windows), 1)
        self.assertFalse(trait_windows[0].blocking)

    def test_bond_invocation_recommits_objective_clock(self) -> None:
        interceptor, characters, _, clocks, _, _ = self.make_interceptor([4, 3])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                bonds=[Bond("白河", ["信赖", "喜爱"])],
            )
        )
        clocks.add(Clock(name="争取信任", max_segments=6, clock_type="objective"))

        first = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "赛璃",
                    "target": "争取信任",
                    "clock_name": "争取信任",
                    "clock_direction": 1,
                    "attributes": ["INS", "WLP"],
                    "target_number": 9,
                },
            )
        )
        self.assertFalse(first.payload["roll"].success)

        invoked = interceptor.resolve(
            Action(ActionType.INVOKE_BOND, {"actor": "赛璃", "bond_target": "白河"})
        )

        self.assertTrue(invoked.payload["roll"].success)
        self.assertEqual(clocks.get("争取信任").current, 1)

    def test_post_check_windows_expose_invocations_and_opportunities_without_rendering(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([3, 4, 6, 6, 7, 6])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                theme="怜悯",
                bonds=[Bond("白河", ["信赖", "喜爱"])],
                skills={"灵光洞见": 2},
            )
        )

        failed = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 10, "non_damage": True},
            )
        )
        failed_windows = failed.payload["post_check_windows"]
        failed_kinds = {window["kind"] for window in failed_windows}
        self.assertIn("trait_invocation", failed_kinds)
        self.assertIn("bond_invocation", failed_kinds)
        invocation_windows = [
            window
            for window in failed_windows
            if window["kind"] in {"trait_invocation", "bond_invocation"}
        ]
        self.assertTrue(invocation_windows)
        self.assertTrue(
            all(not str(window.get("guidance") or "") for window in invocation_windows)
        )

        interceptor.resolve(
            Action(
                ActionType.NARRATE,
                {
                    "actor": "赛璃",
                    "summary": "接受这次失败。",
                    "post_check_acceptance": True,
                    "scene_clarification": True,
                },
            )
        )

        critical = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 10, "non_damage": True},
            )
        )
        critical_kinds = {window["kind"] for window in critical.payload["post_check_windows"]}
        self.assertIn("critical_opportunity", critical_kinds)
        self.assertIn("trait_invocation", critical_kinds)
        self.assertIn("bond_invocation", critical_kinds)
        self.assertFalse(critical.payload.get("check_result_provisional"))

        interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {"actor": "赛璃", "effect": "优势", "target": "赛璃"},
            )
        )

        insight = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "INS"],
                    "target_number": 10,
                    "target": "白花碑驿站",
                    "non_damage": True,
                },
            )
        )
        self.assertFalse(insight.payload.get("check_result_provisional"))
        insight_windows = [
            window for window in insight.payload["post_check_windows"] if window["label"] == "灵光洞见"
        ]
        self.assertEqual(insight_windows[0]["options"][0]["target"], "白花碑驿站")
        self.assertEqual(insight_windows[0]["options"][0]["max_questions"], 2)

    def test_success_invocation_right_does_not_block_and_expires_on_next_action(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([4, 5])
        characters.add(
            make_character(
                "赛璃",
                ["pc"],
                theme="怜悯",
                bonds=[Bond("白河", ["信赖"])],
            )
        )

        checked = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "赛璃", "attributes": ["INS", "WLP"], "target_number": 7, "non_damage": True},
            )
        )
        self.assertTrue(checked.payload["roll"].success)
        trait_windows = interceptor.decision_window_manager.pending(
            kind="trait_invocation"
        )
        bond_windows = interceptor.decision_window_manager.pending(
            kind="bond_invocation"
        )
        self.assertEqual(len(trait_windows), 1)
        self.assertEqual(len(bond_windows), 1)
        self.assertFalse(trait_windows[0].blocking)
        self.assertFalse(bond_windows[0].blocking)
        following = interceptor.resolve(
            Action(ActionType.NARRATE, {"summary": "另一名英雄开始行动。"})
        )
        self.assertEqual(following.action.action_type, ActionType.NARRATE)
        self.assertFalse(interceptor.decision_window_manager.pending(kind="trait_invocation"))
        self.assertFalse(interceptor.decision_window_manager.pending(kind="bond_invocation"))

    def test_player_can_invoke_trait_after_success_before_next_action(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([5, 6, 8])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))

        checked = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        window = interceptor.decision_window_manager.pending(
            kind="trait_invocation",
            owner="赛璃",
        )[0]

        invoked = interceptor.resolve(
            Action(
                ActionType.INVOKE_TRAIT,
                {
                    "actor": "赛璃",
                    "trait_name": "怜悯",
                    "invocation_rationale": "赛璃不愿让眼前的人因自己的疏忽受苦。",
                    "reroll_indices": [0],
                    "window_id": window.window_id,
                },
            )
        )

        self.assertEqual(invoked.payload["roll"].total, 14)
        self.assertEqual(characters.get("赛璃").fabula_points, 2)
        continuing = interceptor.decision_window_manager.pending(
            kind="trait_invocation", owner="赛璃"
        )
        self.assertEqual(len(continuing), 1)
        self.assertFalse(continuing[0].blocking)
        self.assertEqual(continuing[0].payload["invoked_trait"], "怜悯")

    def test_critical_success_keeps_opportunity_visible_and_invocation_silent(self) -> None:
        interceptor, characters, _, _, _, _ = self.make_interceptor([6, 6])
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))

        checked = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )

        self.assertTrue(checked.payload["roll"].critical_success)
        self.assertTrue(
            interceptor.decision_window_manager.pending(
                kind="critical_opportunity",
                owner="赛璃",
            )
        )
        self.assertTrue(
            interceptor.decision_window_manager.pending(
                kind="trait_invocation",
                owner="赛璃",
            )
        )
        public_kinds = {
            item["kind"]
            for item in interceptor.decision_window_manager.public_summary()
        }
        self.assertIn("critical_opportunity", public_kinds)
        self.assertNotIn("trait_invocation", public_kinds)

    def test_unrelated_opportunity_cannot_consume_provisional_check(self) -> None:
        interceptor, characters, rules, clocks, _, world = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(Clock("封住裂隙", 6))
        rules.force_next_check_outcome(
            RollOutcome(
                actor="赛璃",
                attributes=["INS", "WLP"],
                dice=[(8, 2), (8, 3)],
                total=5,
                modifier=0,
                high_roll=3,
                target_number=10,
                success=False,
                critical_success=False,
                fumble=False,
                opportunity_count=0,
                margin=-5,
            )
        )
        provisional = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        trait_window = next(
            window
            for window in provisional.payload["post_check_windows"]
            if window["kind"] == "trait_invocation"
        )

        with self.assertRaisesRegex(ValueError, "不能用机会动作处理"):
            interceptor.resolve(
                Action(
                    ActionType.TRIGGER_OPPORTUNITY,
                    {
                        "actor": "赛璃",
                        "window_id": trait_window["window_id"],
                        "effect": "进展",
                        "clock_name": "封住裂隙",
                    },
                )
            )

        self.assertEqual(clocks.get("封住裂隙").current, 0)
        self.assertIn("赛璃", interceptor.pending_check_transactions)
        self.assertEqual(
            world.decision_windows[trait_window["window_id"]].status,
            DecisionWindowStatus.PENDING,
        )

    def test_opportunity_requires_a_live_grant_and_cannot_repeat(self) -> None:
        interceptor, characters, rules, clocks, _, _ = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"], theme="怜悯"))
        clocks.add(Clock("封住裂隙", 6))

        with self.assertRaisesRegex(ValueError, "没有唯一可处理的机会窗口"):
            interceptor.resolve(
                Action(
                    ActionType.TRIGGER_OPPORTUNITY,
                    {
                        "actor": "赛璃",
                        "effect": "进展",
                        "clock_name": "封住裂隙",
                    },
                )
            )

        rules.force_next_check_outcome(
            RollOutcome(
                actor="赛璃",
                attributes=["INS", "WLP"],
                dice=[(8, 8), (8, 8)],
                total=16,
                modifier=0,
                high_roll=8,
                target_number=10,
                success=True,
                critical_success=True,
                fumble=False,
                opportunity_count=1,
                margin=6,
            )
        )
        finalized = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {
                    "actor": "赛璃",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "non_damage": True,
                },
            )
        )
        opportunity_window = next(
            window
            for window in finalized.payload["post_check_windows"]
            if window["kind"] == "critical_opportunity"
        )
        action = Action(
            ActionType.TRIGGER_OPPORTUNITY,
            {
                "actor": "赛璃",
                "window_id": opportunity_window["window_id"],
                "effect": "进展",
                "clock_name": "封住裂隙",
            },
        )
        interceptor.resolve(action)
        self.assertEqual(clocks.get("封住裂隙").current, 2)

        with self.assertRaisesRegex(ValueError, "已经结束或不存在"):
            interceptor.resolve(action)
        self.assertEqual(clocks.get("封住裂隙").current, 2)

    def test_opportunity_progress_bond_suffer_and_advantage_are_hard_rules(self) -> None:
        interceptor, characters, _, clocks, conflict, _ = self.make_interceptor([3, 4])
        characters.add(make_character("阿凛", ["pc"]))
        characters.add(make_character("财团机兵", ["enemy"]))
        clocks.add(Clock("封住裂隙", 6))

        progress = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "阿凛",
                    "effect": "进展",
                    "clock_name": "封住裂隙",
                    "_trusted_opportunity_grant": True,
                },
            )
        )
        self.assertEqual(progress.payload["clock_change"].delta, 2)
        self.assertEqual(clocks.get("封住裂隙").current, 2)

        bond = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "阿凛",
                    "effect": "纽带",
                    "target": "赛璃",
                    "emotions": ["信赖", "喜爱"],
                    "_trusted_opportunity_grant": True,
                },
            )
        )
        self.assertEqual(bond.payload["bond"].strength, 2)

        suffer = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "阿凛",
                    "effect": "受苦",
                    "target": "财团机兵",
                    "status_effect": "动摇",
                    "_trusted_opportunity_grant": True,
                },
            )
        )
        self.assertEqual(suffer.payload["status"], StatusEffect.SHAKEN)
        self.assertIn(StatusEffect.SHAKEN, characters.get("财团机兵").statuses)

        interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "阿凛",
                    "effect": "优势",
                    "target": "阿凛",
                    "_trusted_opportunity_grant": True,
                },
            )
        )
        advantaged = interceptor.resolve(
            Action(
                ActionType.REQUEST_ROLL,
                {"actor": "阿凛", "attributes": ["DEX", "INS"], "target_number": 11, "non_damage": True},
            )
        )
        self.assertEqual(advantaged.payload["advantage_bonus"], 4)
        self.assertTrue(advantaged.payload["roll"].success)

    def test_reveal_opportunity_waits_for_target_then_discloses_motivation(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"]))
        world.ensure_npc_persona(
            "白花守望会会长",
            public_identity="白花守望会会长",
            goals=["守住旧路，并让失忆旅人避开财团的追捕"],
        )

        missing_target = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "赛璃",
                    "effect": "揭示",
                    "_trusted_opportunity_grant": True,
                },
            )
        )

        self.assertIn("哪一个生物", missing_target.rules_text)
        self.assertTrue(missing_target.payload["opportunity_parameter_required"])
        self.assertEqual(interceptor.pending_opportunity("赛璃")["effect"], "reveal")
        self.assertIsNone(interceptor.pending_opportunity("艾薇娅"))

        resolved = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "赛璃",
                    "effect": "揭示",
                    "target": "白花守望会会长",
                    "target_explicit": True,
                    "window_id": interceptor.decision_window_manager.pending(
                        kind="opportunity_parameter",
                        owner="赛璃",
                    )[0].window_id,
                },
            )
        )

        self.assertIn("守住旧路", resolved.rules_text)
        self.assertEqual(resolved.payload["target"], "白花守望会会长")
        self.assertIsNone(interceptor.pending_opportunity("赛璃"))
        self.assertTrue(any("目标或动机" in memory for memory in world.memories))

    def test_reveal_prefers_npc_current_active_goal(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"]))
        world.ensure_npc_persona(
            "守门人",
            goals=["长远保护驿站"],
            active_goal="让英雄先承诺保护失忆旅人",
        )

        result = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "赛璃",
                    "effect": "揭示",
                    "target": "守门人",
                    "target_explicit": True,
                    "_trusted_opportunity_grant": True,
                },
            )
        )

        self.assertIn("先承诺保护失忆旅人", result.rules_text)

    def test_reveal_prefers_scene_provider_over_generic_npc_role(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"]))
        world.ensure_npc_persona(
            "会长",
            public_identity="会长",
            role_in_story="当前场景中的非玩家角色",
        )
        interceptor.reveal_motivation_provider = lambda target: (
            "守住旧路秘密，也不让失忆旅人落入财团手中" if target == "会长" else ""
        )

        result = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "赛璃",
                    "effect": "揭示",
                    "target": "会长",
                    "target_explicit": True,
                    "_trusted_opportunity_grant": True,
                },
            )
        )

        self.assertIn("守住旧路秘密", result.rules_text)
        self.assertNotIn("非玩家角色", result.rules_text)

    def test_reveal_fallback_never_exposes_backstage_npc_vocabulary(self) -> None:
        interceptor, characters, _, _, _, world = self.make_interceptor()
        characters.add(make_character("赛璃", ["pc"]))
        world.ensure_npc_persona(
            "陌生守门人",
            public_identity="陌生守门人",
            role_in_story="当前场景中的非玩家角色",
        )

        result = interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": "赛璃",
                    "effect": "揭示",
                    "target": "陌生守门人",
                    "target_explicit": True,
                    "_trusted_opportunity_grant": True,
                },
            )
        )

        self.assertNotIn("非玩家角色", result.rules_text)
        self.assertNotIn("当前场景", result.rules_text)

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

    def test_equip_action_can_target_slots_and_unequip_items(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor()
        characters.add(
            make_character(
                "洛岚",
                ["pc"],
                equipment=["青铜剑", "钢匕首", "青铜盾", "旅行装束"],
            )
        )
        conflict.start_scene("换装测试", ["洛岚"])

        dual_wield = interceptor.resolve(
            Action(
                ActionType.EQUIP,
                {
                    "actor": "洛岚",
                    "slots": {
                        "main_hand": "青铜剑",
                        "off_hand": "钢匕首",
                        "shield": "",
                    },
                },
            )
        )

        self.assertEqual(characters.get("洛岚").equipped_main_hand, "青铜剑")
        self.assertEqual(characters.get("洛岚").equipped_off_hand, "钢匕首")
        self.assertEqual(characters.get("洛岚").equipped_shield, "")
        self.assertEqual(dual_wield.payload["equipped_slots"]["off_hand"], "钢匕首")

        interceptor.resolve(
            Action(
                ActionType.EQUIP,
                {
                    "actor": "洛岚",
                    "slots": {"off_hand": "", "shield": "青铜盾"},
                },
            )
        )

        self.assertEqual(characters.get("洛岚").equipped_off_hand, "")
        self.assertEqual(characters.get("洛岚").equipped_shield, "青铜盾")
        with self.assertRaisesRegex(ValueError, "不能更换或卸下防具"):
            interceptor.resolve(
                Action(
                    ActionType.EQUIP,
                    {
                        "actor": "洛岚",
                        "slots": {"armor": "旅行装束"},
                    },
                )
            )

    def test_start_conflict_rolls_initiative_and_awards_villain_appearance_once(self) -> None:
        interceptor, characters, _, _, conflict, _ = self.make_interceptor([6, 5, 5, 4])
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
                    "_confirmed_supporters": ["赛璃"],
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
