import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.decision_window_manager import DecisionWindowManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.components.bestiary_runtime_profiles import ability_profiles_for_bestiary
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Bond,
    Character,
    EffectTiming,
    EnemyRank,
    EscalationStage,
    StatusEffect,
    TimedEffect,
)


class ConflictManagerTests(unittest.TestCase):
    @staticmethod
    def _resolution_fixture() -> tuple[ConflictManager, CharacterManager]:
        characters = CharacterManager()
        for name, traits in (
            ("瓦莉亚", ["pc"]),
            ("帝国机甲", ["enemy"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲"])
        return conflict, characters

    def test_resolution_status_keeps_conflict_open_while_both_sides_remain(self) -> None:
        conflict, _ = self._resolution_fixture()

        status = conflict.resolution_status()

        self.assertFalse(status["ready_for_natural_end"])
        self.assertEqual(status["natural_outcome"], "both_sides_active")
        self.assertEqual(status["active_player_characters"], ["瓦莉亚"])
        self.assertEqual(status["active_hostiles"], ["帝国机甲"])

    def test_white_howler_aura_blocks_shaken_for_allies_only_while_present(self) -> None:
        characters = CharacterManager()
        howler = Character(
            name="霜冠兽",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=20,
            mp=20,
            traits=["enemy"],
            npc_source_template="白嚎怪",
            npc_ability_profiles=ability_profiles_for_bestiary("白嚎怪"),
        )
        ally = Character(
            name="幼兽",
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=30,
            hp=30,
            max_mp=20,
            mp=20,
            traits=["enemy"],
        )
        hero = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        for character in (howler, ally, hero):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "雪岭",
            ["瓦莉亚", "霜冠兽", "幼兽"],
            player_side=["瓦莉亚"],
            enemy_side=["霜冠兽", "幼兽"],
        )

        self.assertFalse(conflict.apply_status("幼兽", StatusEffect.SHAKEN))
        self.assertTrue(conflict.apply_status("幼兽", StatusEffect.WEAKENED))
        self.assertTrue(conflict.apply_status("霜冠兽", StatusEffect.SHAKEN))

        characters.get("霜冠兽").hp = 0
        self.assertTrue(conflict.apply_status("幼兽", StatusEffect.SHAKEN))

    def test_guard_formation_bonus_tracks_another_available_guard(self) -> None:
        characters = CharacterManager()
        for name in ("北门卫", "南门卫"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    defenses={"physical": 11, "magic": 8},
                    traits=["enemy"],
                    npc_source_template="守卫",
                    npc_ability_profiles=ability_profiles_for_bestiary("守卫"),
                )
            )
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                traits=["pc"],
            )
        )
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "城门",
            ["瓦莉亚", "北门卫", "南门卫"],
            player_side=["瓦莉亚"],
            enemy_side=["北门卫", "南门卫"],
        )

        self.assertEqual(conflict.npc_passive_defense_bonus("北门卫", "physical"), 1)
        self.assertEqual(conflict.npc_passive_defense_bonus("北门卫", "magic"), 1)

        characters.get("南门卫").hp = 0
        self.assertEqual(conflict.npc_passive_defense_bonus("北门卫", "physical"), 0)

    def test_resolution_status_reports_when_last_hostile_leaves_conflict(self) -> None:
        conflict, _ = self._resolution_fixture()

        conflict.surrender_combatant("帝国机甲")
        status = conflict.resolution_status()

        self.assertTrue(status["ready_for_natural_end"])
        self.assertEqual(status["natural_outcome"], "hostile_side_removed")
        self.assertEqual(status["active_hostiles"], [])
        self.assertEqual(status["surrendered_combatants"], ["帝国机甲"])

    def test_resolution_status_reports_when_player_side_is_removed(self) -> None:
        conflict, _ = self._resolution_fixture()

        conflict.remove_combatant_from_scene("瓦莉亚", as_escaped=False)
        status = conflict.resolution_status()

        self.assertTrue(status["ready_for_natural_end"])
        self.assertEqual(status["natural_outcome"], "player_side_removed")
        self.assertEqual(status["active_player_characters"], [])
        self.assertEqual(status["defeated_combatants"], ["瓦莉亚"])

    def test_resolution_status_waits_for_zero_hp_player_choice(self) -> None:
        from fu_gm.components.decision_window_manager import DecisionWindowManager

        conflict, characters = self._resolution_fixture()
        conflict.bind_decision_window_manager(DecisionWindowManager(WorldState()))
        characters.get("瓦莉亚").hp = 0

        conflict.resolve_zero_hp("瓦莉亚")
        status = conflict.resolution_status()

        self.assertFalse(status["ready_for_natural_end"])
        self.assertEqual(
            status["natural_outcome"],
            "pending_player_defeat_choice",
        )
        self.assertEqual(status["pending_zero_hp_characters"], ["瓦莉亚"])

    def test_guard_expires_on_owner_next_turn_start(self) -> None:
        characters = CharacterManager()
        guardian = Character(
            name="钢盾骑士",
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=10,
            mp=10,
            traits=["pc"],
        )
        ally = Character(
            name="露琪亚",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 8},
            max_hp=30,
            hp=30,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        enemy = Character(
            name="帝国暗骑士",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=0,
            mp=0,
            traits=["enemy"],
        )
        for character in (guardian, ally, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("桥头鏖战", ["钢盾骑士", "帝国暗骑士"])
        interceptor = ActionInterceptor(
            RulesEngine(),
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        interceptor.resolve(
            Action(
                action_type=ActionType.GUARD,
                parameters={"actor": "钢盾骑士", "guarded_target": "露琪亚"},
            )
        )
        self.assertTrue(characters.get("钢盾骑士").guarding)

        conflict.next_turn()
        self.assertEqual(conflict.state.current_actor(), "帝国暗骑士")
        self.assertTrue(characters.get("钢盾骑士").guarding)

        conflict.next_turn()
        self.assertEqual(conflict.state.current_actor(), "钢盾骑士")
        self.assertTrue(characters.get("钢盾骑士").guarding)

        conflict.begin_current_turn()
        self.assertFalse(characters.get("钢盾骑士").guarding)
        self.assertEqual(conflict.state.active_effects, [])

    def test_bonus_turn_is_inserted_before_normal_progression(self) -> None:
        characters = CharacterManager()
        for name, traits in (("瓦莉亚", ["pc"]), ("帝国机甲", ["enemy"]), ("莱因", ["pc"])):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲", "莱因"])

        conflict.grant_bonus_turn("瓦莉亚")

        next_actor = conflict.next_turn()
        self.assertEqual(next_actor, "瓦莉亚")
        self.assertEqual(conflict.state.current_bonus_actor, "瓦莉亚")
        self.assertIn("奖励回合", conflict.format_phase())

        next_actor = conflict.next_turn()
        self.assertEqual(next_actor, "帝国机甲")
        self.assertIsNone(conflict.state.current_bonus_actor)

    def test_elite_rank_action_alternates_with_an_unacted_pc(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("瓦莉亚", ["pc"]),
            ("精英机甲", ["enemy"]),
            ("莱因", ["pc"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.register_enemy("精英机甲", EnemyRank.ELITE)
        conflict.start_scene("断桥激战", ["瓦莉亚", "精英机甲", "莱因"])

        self.assertEqual(conflict.state.current_actor(), "瓦莉亚")
        self.assertEqual(conflict.next_turn(), "精英机甲")
        self.assertEqual(conflict.next_turn(), "莱因")
        self.assertEqual(conflict.next_turn(), "精英机甲")
        self.assertEqual(conflict.state.current_bonus_actor, "精英机甲")
        self.assertEqual(conflict.next_turn(), "瓦莉亚")
        self.assertEqual(conflict.state.round_number, 2)

    def test_champion_rank_actions_alternate_until_all_pcs_acted(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("悍将机甲", ["enemy"]),
            ("瓦莉亚", ["pc"]),
            ("莱因", ["pc"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "悍将机甲",
            EnemyRank.CHAMPION,
            action_count=3,
        )
        conflict.start_scene("断桥激战", ["悍将机甲", "瓦莉亚", "莱因"])

        observed = [conflict.state.current_actor()]
        for _ in range(5):
            observed.append(conflict.next_turn())

        self.assertEqual(
            observed,
            ["悍将机甲", "瓦莉亚", "悍将机甲", "莱因", "悍将机甲", "瓦莉亚"],
        )
        self.assertEqual(conflict.state.round_number, 2)
        self.assertEqual(conflict.next_turn(), "悍将机甲")

    def test_removing_current_base_actor_does_not_skip_successor(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("瓦莉亚", ["pc"]),
            ("帝国机甲", ["enemy"]),
            ("莱因", ["pc"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲", "莱因"])
        self.assertEqual(conflict.next_turn(), "帝国机甲")

        conflict.remove_combatant_from_scene("帝国机甲", as_escaped=False)

        self.assertEqual(conflict.next_turn(), "莱因")

    def test_removing_last_base_actor_still_completes_the_round(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("瓦莉亚", ["pc"]),
            ("帝国机甲", ["enemy"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲"])
        self.assertEqual(conflict.next_turn(), "帝国机甲")

        conflict.remove_combatant_from_scene("帝国机甲")

        self.assertEqual(conflict.next_turn(), "瓦莉亚")
        self.assertEqual(conflict.state.round_number, 2)

    def test_removing_an_already_acted_combatant_keeps_current_actor_stable(self) -> None:
        characters = CharacterManager()
        for name, traits in (
            ("瓦莉亚", ["pc"]),
            ("帝国机甲", ["enemy"]),
            ("莱因", ["pc"]),
        ):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲", "莱因"])
        self.assertEqual(conflict.next_turn(), "帝国机甲")

        conflict.remove_combatant_from_scene("瓦莉亚")

        self.assertEqual(conflict.state.current_actor(), "帝国机甲")
        self.assertEqual(conflict.next_turn(), "莱因")

    def test_new_out_of_turn_declaration_replaces_the_same_actor_draft(self) -> None:
        characters = CharacterManager()
        for name, traits in (("瓦莉亚", ["pc"]), ("莱因", ["pc"])):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "莱因"])

        conflict.register_held_action("莱因", "Guard", "先举盾防御")
        conflict.register_held_action("莱因", "Attack", "改为攻击机甲")

        held = conflict.held_actions_for_actor("莱因")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["action_type"], "Attack")
        self.assertEqual(held[0]["summary"], "改为攻击机甲")

    def test_leaving_conflict_cancels_cached_action_and_confirmation_window(self) -> None:
        characters = CharacterManager()
        for name, traits in (("瓦莉亚", ["pc"]), ("莱因", ["pc"])):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        decisions = DecisionWindowManager(WorldState())
        conflict.bind_decision_window_manager(decisions)
        conflict.start_scene("断桥激战", ["瓦莉亚", "莱因"])
        conflict.register_held_action("莱因", "Guard", "先举盾防御")

        conflict.remove_combatant_from_scene("莱因")

        self.assertEqual(conflict.held_actions_for_actor("莱因"), [])
        self.assertEqual(
            decisions.pending(kind="held_action", owner="莱因"),
            [],
        )

    def test_round_end_effect_expires_when_new_round_begins(self) -> None:
        characters = CharacterManager()
        for name, traits in (("瓦莉亚", ["pc"]), ("帝国机甲", ["enemy"])):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=10,
                    mp=10,
                    traits=traits,
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲"])
        conflict.register_effect(
            TimedEffect(
                owner="瓦莉亚",
                effect_type="round_marker",
                expires_on=EffectTiming.ROUND_END,
                note="测试轮末失效。",
            )
        )

        conflict.next_turn()
        self.assertEqual(len(conflict.state.active_effects), 1)

        conflict.next_turn()
        self.assertEqual(conflict.state.round_number, 2)
        self.assertEqual(conflict.state.active_effects, [])

    def test_ultima_recovery_clears_statuses_and_restores_mp(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=80,
            max_mp=60,
            mp=5,
            traits=["enemy", "villain"],
            statuses=[StatusEffect.DAZED, StatusEffect.SHAKEN],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=2)
        conflict.state.active_statuses["黑日将军"] = [StatusEffect.DAZED, StatusEffect.SHAKEN]

        event = conflict.spend_ultima_to_recover("黑日将军")

        self.assertEqual(conflict.state.ultima_points["黑日将军"], 1)
        self.assertEqual(characters.get("黑日将军").mp, 55)
        self.assertEqual(characters.get("黑日将军").statuses, [])
        self.assertTrue(event.statuses_cleared)

    def test_villain_zero_hp_escapes_before_escalation_when_ultima_remains(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=100,
            hp=0,
            max_mp=30,
            mp=0,
            traits=["enemy", "villain"],
            statuses=[StatusEffect.WEAKENED],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "帝国机甲",
            EnemyRank.VILLAIN,
            ultima_points=1,
            escalation_stages=[
                EscalationStage(
                    name="二阶段·过载核心",
                    ultima_points=3,
                    hp_restore=70,
                    mp_restore=30,
                    added_statuses=[StatusEffect.ENRAGED],
                )
            ],
        )

        event = conflict.resolve_zero_hp("帝国机甲")

        self.assertEqual(event.event_type, "villain_escape")
        self.assertEqual(conflict.state.ultima_points["帝国机甲"], 0)
        self.assertNotIn(StatusEffect.ENRAGED, characters.get("帝国机甲").statuses)

    def test_villain_zero_hp_can_escalate_after_ultima_is_spent(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=100,
            hp=0,
            max_mp=30,
            mp=0,
            traits=["enemy", "villain"],
            statuses=[StatusEffect.WEAKENED],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "帝国机甲",
            EnemyRank.VILLAIN,
            ultima_points=0,
            escalation_stages=[
                EscalationStage(
                    name="二阶段·过载核心",
                    ultima_points=3,
                    hp_restore=70,
                    mp_restore=30,
                    added_statuses=[StatusEffect.ENRAGED],
                )
            ],
        )

        event = conflict.resolve_zero_hp("帝国机甲")

        self.assertEqual(event.event_type, "escalation")
        self.assertEqual(event.stage_name, "二阶段·过载核心")
        self.assertEqual(characters.get("帝国机甲").hp, 70)
        self.assertEqual(characters.get("帝国机甲").mp, 30)
        self.assertEqual(conflict.state.ultima_points["帝国机甲"], 3)
        self.assertIn(StatusEffect.ENRAGED, characters.get("帝国机甲").statuses)
        self.assertNotIn(StatusEffect.WEAKENED, characters.get("帝国机甲").statuses)

    def test_villain_story_role_is_separate_from_combat_rank(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=0,
            max_mp=30,
            mp=10,
            traits=["enemy"],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy("黑日将军", EnemyRank.ELITE, ultima_points=1, is_villain=True)

        self.assertTrue(conflict.is_villain("黑日将军"))
        self.assertEqual(conflict.state.enemy_ranks["黑日将军"], EnemyRank.ELITE)
        self.assertEqual(conflict.state.enemy_action_counts["黑日将军"], 2)

        event = conflict.resolve_zero_hp("黑日将军")

        self.assertEqual(event.event_type, "villain_escape")

    def test_villain_zero_hp_escapes_by_spending_ultima_without_escalation(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=0,
            max_mp=30,
            mp=10,
            traits=["enemy", "villain"],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=2)

        event = conflict.resolve_zero_hp("黑日将军", allow_escalation=False)

        self.assertEqual(event.event_type, "villain_escape")
        self.assertEqual(conflict.state.ultima_points["黑日将军"], 1)
        self.assertIn("黑日将军", conflict.state.escaped_combatants)

    def test_pc_zero_hp_give_up_resistance_awards_two_fabula_points(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=0,
            max_mp=30,
            mp=20,
            fabula_points=1,
            traits=["pc"],
        )
        characters.add(pc)
        conflict = ConflictManager(characters)

        event = conflict.resolve_zero_hp(
            "瓦莉亚",
            pc_choice="give_up_resistance",
            pc_consequence="被帝国俘虏并夺走魔剑",
        )

        self.assertEqual(event.event_type, "pc_give_up_resistance")
        self.assertEqual(event.fabula_awarded, 2)
        self.assertEqual(characters.get("瓦莉亚").fabula_points, 3)
        self.assertEqual(conflict.state.fallen_pcs["瓦莉亚"], "被帝国俘虏并夺走魔剑")

    def test_pc_zero_hp_without_choice_creates_pending_decision(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=0,
                max_mp=30,
                mp=20,
                fabula_points=1,
                traits=["pc"],
            )
        )
        conflict = ConflictManager(characters)

        pending = conflict.resolve_zero_hp("瓦莉亚")

        self.assertEqual(pending.event_type, "pc_zero_hp_choice_required")
        self.assertNotIn("瓦莉亚", conflict.state.defeated_combatants)
        self.assertIsNotNone(conflict.pending_zero_hp_decision("瓦莉亚"))

        resolved = conflict.resolve_pending_zero_hp(
            "瓦莉亚",
            choice="give_up_resistance",
            consequence="被帝国俘虏",
        )
        self.assertEqual(resolved.event_type, "pc_give_up_resistance")
        self.assertIsNone(conflict.pending_zero_hp_decision("瓦莉亚"))

    def test_unified_zero_hp_resolution_uses_only_persisted_window(self) -> None:
        from fu_gm.components.decision_window_manager import DecisionWindowManager
        from fu_gm.components.world_state import WorldState

        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=0,
                max_mp=30,
                mp=20,
                fabula_points=1,
                traits=["pc"],
            )
        )
        conflict = ConflictManager(characters)
        windows = DecisionWindowManager(WorldState())
        conflict.bind_decision_window_manager(windows)

        conflict.resolve_zero_hp("瓦莉亚")
        self.assertEqual(conflict.state.pending_decisions, [])
        self.assertTrue(windows.has_blocking(owner="瓦莉亚"))

        conflict.resolve_pending_zero_hp(
            "瓦莉亚",
            choice="give_up_resistance",
            consequence="被帝国俘虏",
        )

        self.assertEqual(conflict.state.pending_decisions, [])
        self.assertFalse(windows.has_blocking(owner="瓦莉亚"))

    def test_pc_zero_hp_cannot_be_committed_without_its_persisted_window(self) -> None:
        from fu_gm.components.decision_window_manager import DecisionWindowManager
        from fu_gm.components.world_state import WorldState

        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=0,
                max_mp=30,
                mp=20,
                fabula_points=1,
                traits=["pc"],
            )
        )
        conflict = ConflictManager(characters)
        conflict.bind_decision_window_manager(DecisionWindowManager(WorldState()))

        with self.assertRaisesRegex(ValueError, "必须先建立"):
            conflict.resolve_zero_hp(
                "瓦莉亚",
                pc_choice="give_up_resistance",
                pc_consequence="绝望：敌人夺走桥头",
            )

        self.assertEqual(characters.get("瓦莉亚").fabula_points, 1)
        self.assertNotIn("瓦莉亚", conflict.state.fallen_pcs)

    def test_starting_another_conflict_preserves_offscreen_fallen_pc(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=0,
                max_mp=30,
                mp=20,
                traits=["pc"],
            )
        )
        characters.add(
            Character(
                name="露琪亚",
                attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=40,
                mp=40,
                traits=["pc"],
            )
        )
        conflict = ConflictManager(characters)
        conflict.state.fallen_pcs["瓦莉亚"] = "失散：被洪流冲走"
        conflict.state.pc_defeat_consequences["瓦莉亚"] = ["失散：被洪流冲走"]

        conflict.start_scene("另一侧的追击", ["露琪亚"])

        self.assertEqual(conflict.state.fallen_pcs["瓦莉亚"], "失散：被洪流冲走")
        self.assertEqual(
            conflict.state.pc_defeat_consequences["瓦莉亚"],
            ["失散：被洪流冲走"],
        )

    def test_pc_sacrifice_requires_two_rule_conditions(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=0,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        villain = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=80,
            max_mp=30,
            mp=10,
            traits=["enemy", "villain"],
        )
        characters.add(pc)
        characters.add(villain)
        conflict = ConflictManager(characters)

        with self.assertRaisesRegex(ValueError, "至少满足"):
            conflict.resolve_zero_hp("瓦莉亚", pc_choice="sacrifice")

        pc.theme = "希望"
        pc.bonds.append(Bond(target="黑日将军", emotions=["憎恨"]))
        conflict.start_scene("终局之桥", ["瓦莉亚", "黑日将军"])
        conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=0)

        event = conflict.resolve_zero_hp("瓦莉亚", pc_choice="sacrifice")

        self.assertEqual(event.event_type, "pc_sacrifice")
        self.assertIn("瓦莉亚", conflict.state.sacrifices)

    def test_pc_sacrifice_respects_explicit_rule_condition_flags(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=0,
            max_mp=30,
            mp=20,
            traits=["pc"],
            theme="希望",
            bonds=[Bond(target="露琪亚", emotions=["喜爱"])],
        )
        villain = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=80,
            max_mp=30,
            mp=10,
            traits=["enemy"],
        )
        characters.add(pc)
        characters.add(villain)
        conflict = ConflictManager(characters)
        conflict.start_scene("终局之桥", ["瓦莉亚", "黑日将军"])
        conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=0)

        with self.assertRaisesRegex(ValueError, "至少满足"):
            conflict.resolve_zero_hp(
                "瓦莉亚",
                pc_choice="sacrifice",
                sacrifice_benefits_bond=False,
                sacrifice_betters_world=False,
            )

    def test_pc_sacrifice_organized_chronicles_requires_all_conditions(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=0,
            max_mp=30,
            mp=20,
            traits=["pc"],
            theme="希望",
            bonds=[Bond(target="露琪亚", emotions=["喜爱"])],
        )
        villain = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=80,
            max_mp=30,
            mp=10,
            traits=["enemy"],
        )
        characters.add(pc)
        characters.add(villain)
        conflict = ConflictManager(characters)
        conflict.start_scene("终局之桥", ["瓦莉亚", "黑日将军"])
        conflict.register_enemy("黑日将军", EnemyRank.VILLAIN, ultima_points=0)

        with self.assertRaisesRegex(ValueError, "组织化章节模式"):
            conflict.resolve_zero_hp(
                "瓦莉亚",
                pc_choice="sacrifice",
                sacrifice_benefits_bond=True,
                sacrifice_betters_world=False,
                require_all_sacrifice_conditions=True,
            )

        event = conflict.resolve_zero_hp(
            "瓦莉亚",
            pc_choice="sacrifice",
            sacrifice_benefits_bond=True,
            sacrifice_betters_world=True,
            require_all_sacrifice_conditions=True,
        )

        self.assertEqual(event.event_type, "pc_sacrifice")
        self.assertIn("瓦莉亚", conflict.state.sacrifices)

    def test_escalation_awards_fabula_to_all_pcs(self) -> None:
        characters = CharacterManager()
        for name in ("瓦莉亚", "露琪亚"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    fabula_points=0,
                    traits=["pc"],
                )
            )
        boss = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=100,
            hp=0,
            max_mp=30,
            mp=0,
            traits=["enemy"],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "帝国机甲",
            EnemyRank.VILLAIN,
            ultima_points=0,
            escalation_stages=[EscalationStage(name="二阶段·过载核心", ultima_points=3, hp_restore=70)],
        )

        event = conflict.resolve_zero_hp("帝国机甲")

        self.assertEqual(event.event_type, "escalation")
        self.assertEqual(event.fabula_awarded, 2)
        self.assertEqual(characters.get("瓦莉亚").fabula_points, 1)
        self.assertEqual(characters.get("露琪亚").fabula_points, 1)

    def test_boss_phase_happens_before_ultima_escape_and_grants_preparation_turns(self) -> None:
        characters = CharacterManager()
        for name in ("瓦莉亚", "露琪亚"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    fabula_points=0,
                    traits=["pc"],
                )
            )
        boss = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=100,
            hp=0,
            max_mp=30,
            mp=0,
            traits=["enemy", "villain"],
        )
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "帝国机甲",
            EnemyRank.VILLAIN,
            ultima_points=2,
            escalation_stages=[
                EscalationStage(
                    name="二阶段·过载核心",
                    ultima_points=0,
                    transition_kind="boss_phase",
                    preparation_round=True,
                )
            ],
        )
        conflict.start_scene(
            "核心决战",
            ["瓦莉亚", "帝国机甲", "露琪亚"],
        )

        event = conflict.resolve_zero_hp("帝国机甲")

        self.assertEqual(event.event_type, "boss_phase")
        self.assertEqual(event.fabula_awarded, 0)
        self.assertEqual(conflict.state.ultima_points["帝国机甲"], 2)
        self.assertNotIn("帝国机甲", conflict.state.escaped_combatants)
        self.assertEqual(characters.get("瓦莉亚").fabula_points, 0)
        self.assertEqual(characters.get("露琪亚").fabula_points, 0)
        self.assertEqual(
            conflict.state.queued_turns,
            ["瓦莉亚", "露琪亚"],
        )

    def test_interceptor_emits_conflict_event_when_boss_is_dropped_to_zero(self) -> None:
        characters = CharacterManager()
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=30,
            mp=20,
            weapon_damage=5,
            weapon_type="lightning",
            traits=["pc"],
        )
        boss = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=20,
            hp=10,
            max_mp=20,
            mp=0,
            traits=["enemy", "villain"],
        )
        characters.add(pc)
        characters.add(boss)
        conflict = ConflictManager(characters)
        conflict.register_enemy(
            "帝国机甲",
            EnemyRank.VILLAIN,
            ultima_points=0,
            escalation_stages=[EscalationStage(name="失控核心", ultima_points=2, hp_restore=20)],
        )
        engine = RulesEngine(seed=0)
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "瓦莉亚",
                    "attributes": ["DEX", "MIG"],
                    "target": "帝国机甲",
                    "target_number": 12,
                    "damage_type": "lightning",
                },
            )
        )

        self.assertIn("conflict_event", resolution.payload)
        self.assertEqual(resolution.payload["conflict_event"].event_type, "escalation")
        self.assertEqual(characters.get("帝国机甲").hp, 20)


if __name__ == "__main__":
    unittest.main()
