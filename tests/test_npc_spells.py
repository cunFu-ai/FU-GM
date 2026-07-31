from __future__ import annotations

import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Affinity,
    Character,
    EnemyRank,
    EscalationStage,
    GamePanel,
    StatusEffect,
)
from fu_gm.spellbook import get_spell_definition


class FakeRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(
                f"掷骰值 {value} 超出范围 {low}-{high}"
            )
        return value


class NPCSpellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.characters = CharacterManager()
        self.caster = Character(
            name="灾厄祭司",
            level=30,
            attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 10},
            max_hp=120,
            hp=120,
            max_mp=200,
            mp=200,
            defenses={"physical": 10, "magic": 11},
            weapon_accuracy_attributes=["INS", "WLP"],
            weapon_damage=5,
            traits=["enemy", "villain"],
            spells=[
                "范围异常",
                "恶毒诅咒",
                "战吼",
                "削弱",
                "毁灭",
                "抢攻",
            ],
        )
        self.hero = Character(
            name="伊莉雅",
            level=5,
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=40,
            mp=40,
            defenses={"physical": 9, "magic": 9},
            weapon_accuracy_attributes=["DEX", "MIG"],
            weapon_damage=5,
            weapon_type="physical",
            traits=["pc"],
            fabula_points=0,
        )
        self.ally = Character(
            name="铁卫",
            level=10,
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
            max_hp=70,
            hp=70,
            max_mp=30,
            mp=30,
            defenses={"physical": 10, "magic": 8},
            weapon_accuracy_attributes=["MIG", "MIG"],
            weapon_damage=10,
            traits=["enemy"],
        )
        for character in (self.caster, self.hero, self.ally):
            self.characters.add(character)
        self.caster = self.characters.get(self.caster.name)
        self.hero = self.characters.get(self.hero.name)
        self.ally = self.characters.get(self.ally.name)
        self.rules_engine = RulesEngine()
        self.conflict = ConflictManager(self.characters)
        self.conflict.register_enemy(
            self.caster.name,
            EnemyRank.ELITE,
        )
        self.conflict.register_enemy(
            self.ally.name,
            EnemyRank.SOLDIER,
        )
        self.conflict.start_scene(
            "钟塔决战",
            [self.caster.name, self.hero.name, self.ally.name],
        )
        self.interceptor = ActionInterceptor(
            self.rules_engine,
            self.characters,
            ClockManager(),
            self.conflict,
            WorldState(),
        )

    @staticmethod
    def panel() -> GamePanel:
        return GamePanel(
            game_phase="冲突场景",
            active_clocks=[],
            pc_status=[],
            enemy_status=[],
            recent_chat="轮到灾厄祭司。",
            current_actor="灾厄祭司",
        )

    def cast(self, spell_name: str, **parameters: object):
        return self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": self.caster.name,
                    "spell_name": spell_name,
                    **parameters,
                },
            )
        )

    def test_official_npc_spell_definitions_are_not_shifted(self) -> None:
        self.assertEqual(
            get_spell_definition("范围异常").mp_cost,
            20,
        )
        self.assertEqual(
            get_spell_definition("恶毒诅咒").selectable_status_count,
            2,
        )
        self.assertEqual(
            get_spell_definition("偷取精神").drain_to,
            "mp",
        )
        self.assertEqual(
            get_spell_definition("战吼").check_bonus,
            1,
        )

    def test_area_status_is_automatic_and_costs_once(self) -> None:
        resolution = self.cast(
            "范围异常",
            targets=[self.hero.name, self.ally.name],
            chosen_status="slow",
        )

        self.assertNotIn("roll", resolution.payload)
        self.assertEqual(self.caster.mp, 180)
        self.assertIn(StatusEffect.SLOW, self.hero.statuses)
        self.assertIn(StatusEffect.SLOW, self.ally.statuses)

    def test_vicious_curse_requires_and_applies_two_statuses(self) -> None:
        self.rules_engine._rng = FakeRandom([8, 8])
        resolution = self.cast(
            "恶毒诅咒",
            target=self.hero.name,
            chosen_statuses=["slow", "dazed"],
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertIn(StatusEffect.SLOW, self.hero.statuses)
        self.assertIn(StatusEffect.DAZED, self.hero.statuses)

    def test_npc_spell_uses_its_persisted_attribute_pair(self) -> None:
        self.caster.npc_spell_attributes["恶毒诅咒"] = ["MIG", "WLP"]
        self.rules_engine._rng = FakeRandom([7, 8])

        resolution = self.cast(
            "恶毒诅咒",
            target=self.hero.name,
            chosen_statuses=["slow", "dazed"],
        )

        roll = resolution.payload["roll"]
        self.assertEqual(roll.attributes, ["MIG", "WLP"])
        self.assertEqual(
            [die_size for die_size, _value in roll.dice],
            [8, 10],
        )

    def test_war_cry_bonuses_only_hit_checks(self) -> None:
        self.cast("战吼", target=self.ally.name)
        self.rules_engine._rng = FakeRandom([4, 4])
        attack = self.interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": self.ally.name,
                    "target": self.hero.name,
                },
            )
        )

        self.assertEqual(attack.payload["roll"].modifier, 1)
        self.assertTrue(attack.payload["roll"].success)

    def test_weaken_only_bonuses_selected_damage_type(self) -> None:
        self.rules_engine._rng = FakeRandom([8, 8])
        self.cast(
            "削弱",
            target=self.hero.name,
            chosen_damage_type="fire",
        )

        self.assertEqual(
            self.interceptor._incoming_damage_bonus(
                self.hero.name,
                "fire",
            ),
            5,
        )
        self.assertEqual(
            self.interceptor._incoming_damage_bonus(
                self.hero.name,
                "physical",
            ),
            0,
        )

    def test_devastation_catalog_enforces_rank_level_and_last_turn(self) -> None:
        combat_rules = NPCCombatRules(
            self.characters,
            self.conflict,
            WorldState(),
        )
        self.conflict.state.enemy_action_counts[self.caster.name] = 2
        self.conflict.state.current_bonus_actor = None
        catalog = combat_rules.build_legal_action_catalog(
            self.panel(),
            self.caster.name,
        )
        self.assertNotIn(
            "毁灭",
            [
                item.get("spell_name")
                for item in catalog
                if item.get("npc_action_type") == "Spell"
            ],
        )

        self.conflict.state.current_bonus_actor = self.caster.name
        self.conflict.state.queued_turns = []
        self.conflict.state.queued_turn_kinds = []
        catalog = combat_rules.build_legal_action_catalog(
            self.panel(),
            self.caster.name,
        )
        self.assertIn(
            "毁灭",
            [
                item.get("spell_name")
                for item in catalog
                if item.get("npc_action_type") == "Spell"
            ],
        )

    def test_npc_spell_catalog_limits_targets_by_affordable_total_cost(self) -> None:
        second_hero = Character(
            name="洛岚",
            level=5,
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=40,
            mp=40,
            defenses={"physical": 9, "magic": 9},
            traits=["pc"],
        )
        self.characters.add(second_hero)
        self.conflict.state.turn_order.append(second_hero.name)
        self.caster.spells.append("炎弹")
        self.caster.mp = 15
        combat_rules = NPCCombatRules(
            self.characters,
            self.conflict,
            WorldState(),
        )

        catalog = combat_rules.build_legal_action_catalog(
            self.panel(),
            self.caster.name,
        )
        fireball = next(
            item
            for item in catalog
            if item.get("spell_name") == "炎弹"
        )
        self.assertEqual(fireball["max_targets"], 1)
        self.assertEqual(fireball["minimum_total_mp_cost"], 10)

        with self.assertRaisesRegex(ValueError, "目标不在当前合法选项"):
            combat_rules.validate_action(
                self.panel(),
                self.caster.name,
                {
                    "npc_action_type": "Spell",
                    "spell_name": "炎弹",
                    "targets": [self.hero.name, second_hero.name],
                    "action_description": "祭司同时向两名英雄掷出火焰。",
                },
            )

    def test_devastation_uses_fixed_damage_and_opens_pc_zero_hp_window(self) -> None:
        self.hero.hp = 10
        self.conflict.state.enemy_action_counts[self.caster.name] = 1
        resolution = self.cast(
            "毁灭",
            chosen_damage_type="fire",
        )

        self.assertEqual(self.hero.hp, 0)
        self.assertTrue(resolution.payload["damage_results"])
        windows = self.interceptor.decision_window_manager.pending(
            kind="zero_hp",
            owner=self.hero.name,
        )
        self.assertEqual(len(windows), 1)

    def test_devastation_adds_the_npc_level_damage_bonus_once(self) -> None:
        self.caster.npc_spell_damage_bonus = 5
        self.conflict.state.enemy_action_counts[self.caster.name] = 1

        resolution = self.cast(
            "毁灭",
            chosen_damage_type="fire",
        )

        result = next(
            item
            for item in resolution.payload["damage_results"]
            if item["target"] == self.hero.name
        )
        self.assertEqual(result["base_damage"], 35)
        self.assertEqual(result["actual_hp_loss"], 35)

    def test_devastation_opens_one_zero_hp_window_for_each_fallen_pc(self) -> None:
        second_hero = Character(
            name="洛岚",
            level=5,
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=40,
            hp=10,
            max_mp=40,
            mp=40,
            defenses={"physical": 9, "magic": 9},
            traits=["pc"],
        )
        self.characters.add(second_hero)
        self.conflict.state.turn_order.append(second_hero.name)
        self.hero.hp = 10
        self.conflict.state.enemy_action_counts[self.caster.name] = 1

        self.cast("毁灭", chosen_damage_type="fire")

        windows = self.interceptor.decision_window_manager.pending(
            kind="zero_hp",
        )
        self.assertEqual(
            {window.owner for window in windows},
            {self.hero.name, second_hero.name},
        )

    def test_cursed_breath_applies_selected_status_after_damage(self) -> None:
        self.caster.spells.append("诅咒吐息")
        self.rules_engine._rng = FakeRandom([5, 4])

        resolution = self.cast(
            "诅咒吐息",
            target=self.hero.name,
            chosen_damage_type="fire",
            chosen_status="slow",
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertLess(self.hero.hp, self.hero.max_hp)
        self.assertIn(StatusEffect.SLOW, self.hero.statuses)

    def test_cursed_breath_status_does_not_leak_into_new_boss_phase(self) -> None:
        self.hero.spells.append("诅咒吐息")
        self.ally.hp = 5
        self.conflict.register_enemy(
            self.ally.name,
            EnemyRank.SOLDIER,
            is_villain=True,
            escalation_stages=[
                EscalationStage(
                    name="潮汐核心",
                    ultima_points=0,
                    transition_kind="boss_phase",
                    hp_restore=50,
                    public_cue="铁壳裂开，潮汐核心重新亮起。",
                )
            ],
        )
        self.rules_engine._rng = FakeRandom([8, 8])

        resolution = self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": self.hero.name,
                    "spell_name": "诅咒吐息",
                    "target": self.ally.name,
                    "chosen_damage_type": "fire",
                    "chosen_status": "slow",
                },
            )
        )

        self.assertEqual(self.ally.hp, 50)
        self.assertNotIn(StatusEffect.SLOW, self.ally.statuses)
        self.assertNotIn(
            self.ally.name,
            resolution.payload.get("status_applied_by_target", {}),
        )
        self.assertEqual(
            resolution.payload["conflict_event"].event_type,
            "boss_phase",
        )

    def test_steal_spirit_damages_hp_without_reducing_target_mp(self) -> None:
        self.caster.spells.append("偷取精神")
        self.caster.mp = 20
        target_mp = self.hero.mp
        self.rules_engine._rng = FakeRandom([5, 4])

        resolution = self.cast(
            "偷取精神",
            target=self.hero.name,
            chosen_damage_type="dark",
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertEqual(self.hero.mp, target_mp)
        self.assertEqual(self.hero.hp, self.hero.max_hp - 20)
        self.assertEqual(self.caster.mp, 20)
        self.assertEqual(
            resolution.payload["drain_change"].amount,
            10,
        )

    def test_rush_attack_creates_and_resolves_player_choice(self) -> None:
        player_caster = Character(
            name="赛璃",
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=60,
            mp=60,
            traits=["pc"],
            spells=["抢攻"],
            fabula_points=0,
        )
        self.characters.add(player_caster)
        self.conflict.state.turn_order.append(player_caster.name)
        spell = self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": player_caster.name,
                    "target": self.hero.name,
                    "spell_name": "抢攻",
                },
            )
        )
        window_id = spell.payload["decision_window_id"]

        self.rules_engine._rng = FakeRandom([6, 6])
        attack = self.interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": self.hero.name,
                    "target": self.caster.name,
                    "_immediate_attack_window_id": window_id,
                    "_reaction_followup": True,
                    "_enforce_turn_order": False,
                },
            )
        )

        self.assertTrue(attack.payload["immediate_attack_resolved"])
        self.assertIsNone(
            self.interceptor.decision_window_manager.find_pending(
                window_id=window_id
            )
        )

    def test_npc_rush_attack_resolves_the_named_ally_attack_once(self) -> None:
        self.caster.spells.append("抢攻")
        self.rules_engine._rng = FakeRandom([5, 4])
        before_hp = self.hero.hp

        resolution = self.cast(
            "抢攻",
            target=self.ally.name,
            attack_target=self.hero.name,
        )

        self.assertEqual(
            resolution.payload["immediate_attack_actor"],
            self.ally.name,
        )
        self.assertEqual(
            resolution.payload["immediate_attack_target"],
            self.hero.name,
        )
        self.assertLess(self.hero.hp, before_hp)
        self.assertFalse(
            self.interceptor.pending_check_transactions
        )

    def test_spell_specific_critical_effect_uses_same_opportunity(self) -> None:
        player_caster = Character(
            name="米菈",
            attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=60,
            mp=60,
            traits=["pc"],
            spells=["闪电击"],
            fabula_points=0,
        )
        self.characters.add(player_caster)
        self.conflict.state.turn_order.append(player_caster.name)
        self.rules_engine._rng = FakeRandom([6, 6])
        spell = self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": player_caster.name,
                    "target": self.ally.name,
                    "spell_name": "闪电击",
                },
            )
        )
        self.assertNotIn(StatusEffect.DAZED, self.ally.statuses)
        window = self.interceptor.decision_window_manager.find_pending(
            kind="critical_opportunity",
            owner=player_caster.name,
        )
        self.assertIsNotNone(window)
        self.assertIn(
            "法术附加效果",
            [option.get("effect") for option in window.options],
        )

        self.interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": player_caster.name,
                    "window_id": window.window_id,
                    "effect": "法术附加效果",
                },
            )
        )

        self.assertIn(StatusEffect.DAZED, self.ally.statuses)

    def test_earthquake_critical_opportunity_penalizes_next_turn(self) -> None:
        player_caster = Character(
            name="米菈",
            attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=60,
            mp=60,
            traits=["pc"],
            spells=["地动"],
            fabula_points=0,
        )
        self.characters.add(player_caster)
        self.conflict.state.turn_order.append(player_caster.name)
        self.rules_engine._rng = FakeRandom([6, 6])

        self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": player_caster.name,
                    "target": self.ally.name,
                    "spell_name": "地动",
                },
            )
        )
        window = self.interceptor.decision_window_manager.find_pending(
            kind="critical_opportunity",
            owner=player_caster.name,
        )
        self.assertIsNotNone(window)

        self.interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": player_caster.name,
                    "window_id": window.window_id,
                    "effect": "法术附加效果",
                },
            )
        )

        self.assertEqual(
            self.conflict.state.action_penalties[self.ally.name],
            1,
        )

    def test_gale_critical_opportunity_temporarily_grounds_flying_target(self) -> None:
        player_caster = Character(
            name="米菈",
            attributes={"DEX": 6, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=60,
            mp=60,
            traits=["pc"],
            spells=["罡风"],
            fabula_points=0,
        )
        self.characters.add(player_caster)
        self.conflict.state.turn_order.append(player_caster.name)
        self.ally.abilities.append("飞行")
        self.rules_engine._rng = FakeRandom([6, 6])

        self.interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": player_caster.name,
                    "target": self.ally.name,
                    "spell_name": "罡风",
                },
            )
        )
        window = self.interceptor.decision_window_manager.find_pending(
            kind="critical_opportunity",
            owner=player_caster.name,
        )
        self.interceptor.resolve(
            Action(
                ActionType.TRIGGER_OPPORTUNITY,
                {
                    "actor": player_caster.name,
                    "window_id": window.window_id,
                    "effect": "法术附加效果",
                },
            )
        )

        self.assertTrue(
            any(
                effect.effect_key == "flight_suppressed"
                and effect.target == self.ally.name
                for effect in self.conflict.state.active_effects
            )
        )


if __name__ == "__main__":
    unittest.main()
