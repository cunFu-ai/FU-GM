from __future__ import annotations

import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    ActionType,
    Character,
    DecisionWindow,
    EnemyRank,
    GamePanel,
)


class NPCCombatRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.characters = CharacterManager()
        self.enemy = Character(
            name="王城卫兵长",
            level=10,
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=70,
            hp=70,
            max_mp=50,
            mp=50,
            defenses={"physical": 11, "magic": 8},
            weapon_accuracy_attributes=["MIG", "MIG"],
            weapon_accuracy_modifier=1,
            weapon_damage=10,
            weapon_type="physical",
            weapon_range="melee",
            traits=["enemy", "humanoid"],
            spells=["战吼"],
        )
        self.hero = Character(
            name="伊莉雅",
            level=5,
            attributes={"DEX": 8, "INS": 10, "MIG": 8, "WLP": 8},
            max_hp=45,
            hp=32,
            max_mp=45,
            mp=45,
            defenses={"physical": 11, "magic": 10},
            traits=["pc"],
        )
        self.characters.add(self.enemy)
        self.characters.add(self.hero)
        self.conflict = ConflictManager(self.characters)
        self.conflict.register_enemy(
            "王城卫兵长",
            EnemyRank.ELITE,
            ultima_points=1,
        )
        self.conflict.start_scene(
            "王座厅",
            ["王城卫兵长", "伊莉雅"],
        )
        self.rules = NPCCombatRules(
            self.characters,
            self.conflict,
            WorldState(),
        )

    @staticmethod
    def panel(*, clocks: list[str] | None = None) -> GamePanel:
        return GamePanel(
            game_phase="冲突场景",
            active_clocks=list(clocks or []),
            pc_status=["伊莉雅: HP 32/45"],
            enemy_status=["王城卫兵长: HP 70/70"],
            recent_chat="轮到王城卫兵长行动。",
            current_actor="王城卫兵长",
        )

    def test_snapshot_exposes_legal_choices_but_never_decides_for_core_gm(self) -> None:
        snapshot = self.rules.build_tactical_snapshot(
            self.panel(),
            "王城卫兵长",
        )

        action_types = {
            item["npc_action_type"] for item in snapshot["legal_actions"]
        }
        self.assertIn("Attack", action_types)
        self.assertIn("Guard", action_types)
        self.assertIn("UltimaRecover", action_types)
        self.assertFalse(hasattr(self.rules, "decide"))

    def test_validate_attack_uses_authoritative_profile_not_model_numbers(self) -> None:
        action = self.rules.validate_action(
            self.panel(),
            "王城卫兵长",
            {
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "attributes": ["DEX", "DEX"],
                "damage_bonus": 999,
                "action_description": (
                    "卫兵长压低重心，长枪沿盾缘直刺伊莉雅的持剑手。"
                ),
            },
        )

        self.assertEqual(action.action_type, ActionType.NPCACT)
        self.assertEqual(action.parameters["attributes"], ["MIG", "MIG"])
        self.assertNotIn("damage_bonus", action.parameters)
        self.assertEqual(
            action.parameters["in_mind_reply"],
            "卫兵长压低重心，长枪沿盾缘直刺伊莉雅的持剑手。",
        )

    def test_validate_action_requires_core_gm_public_description(self) -> None:
        with self.assertRaisesRegex(ValueError, "公开行动描述"):
            self.rules.validate_action(
                self.panel(),
                "王城卫兵长",
                {
                    "npc_action_type": "Attack",
                    "target": "伊莉雅",
                },
            )

    def test_objective_direction_comes_from_clock_type(self) -> None:
        panel = self.panel(
            clocks=[
                "[打开王座厅侧门] 2/6；目标命刻",
                "[卫队封锁王宫] 3/6；威胁命刻",
            ]
        )

        hero_clock = self.rules.validate_action(
            panel,
            "王城卫兵长",
            {
                "npc_action_type": "Objective",
                "clock_name": "打开王座厅侧门",
                "action_description": "卫兵长一脚踢回门闩，试图把刚松开的侧门重新锁死。",
            },
        )
        threat_clock = self.rules.validate_action(
            panel,
            "王城卫兵长",
            {
                "npc_action_type": "Objective",
                "clock_name": "卫队封锁王宫",
                "action_description": "卫兵长举枪发出短促号令，催促外廊卫队收紧包围。",
            },
        )

        self.assertEqual(hero_clock.parameters["clock_direction"], -1)
        self.assertEqual(threat_clock.parameters["clock_direction"], 1)

    def test_objective_uses_gm_submitted_target_number(self) -> None:
        action = self.rules.validate_action(
            self.panel(clocks=["[打开王座厅侧门] 2/6；目标命刻"]),
            "王城卫兵长",
            {
                "npc_action_type": "Objective",
                "clock_name": "打开王座厅侧门",
                "target_number": 13,
                "action_description": "卫兵长把门闩重新压回卡槽，阻止侧门继续开启。",
            },
        )

        self.assertEqual(action.parameters["target_number"], 13)

    def test_objective_rejects_invalid_low_target_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少为 7"):
            self.rules.validate_action(
                self.panel(clocks=["[打开王座厅侧门] 2/6；目标命刻"]),
                "王城卫兵长",
                {
                    "npc_action_type": "Objective",
                    "clock_name": "打开王座厅侧门",
                    "target_number": 0,
                    "action_description": "卫兵长试图把侧门重新锁死。",
                },
            )

    def test_invalid_target_is_rejected_without_guessing(self) -> None:
        with self.assertRaisesRegex(ValueError, "不在当前可选目标"):
            self.rules.validate_action(
                self.panel(),
                "王城卫兵长",
                {
                    "npc_action_type": "Attack",
                    "target": "不存在的英雄",
                    "action_description": "卫兵长举枪刺向并不存在的人。",
                },
            )

    def test_legal_targets_are_limited_to_active_conflict_combatants(self) -> None:
        absent_hero = Character(
            name="留在城里的同伴",
            attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=40,
            mp=40,
            defenses={"physical": 8, "magic": 8},
            traits=["pc"],
        )
        self.characters.add(absent_hero)
        self.conflict.start_scene("王座厅", ["王城卫兵长", "伊莉雅"])

        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "王城卫兵长",
        )
        attack = next(
            item for item in catalog if item["npc_action_type"] == "Attack"
        )

        self.assertEqual(attack["targets"], ["伊莉雅"])

    def test_villain_only_trait_counts_as_an_active_npc_ally(self) -> None:
        ally = Character(
            name="无面大臣",
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=50,
            hp=50,
            max_mp=60,
            mp=60,
            defenses={"physical": 8, "magic": 10},
            traits=["villain", "humanoid"],
        )
        self.characters.add(ally)
        self.conflict.start_scene(
            "王座厅",
            ["王城卫兵长", "伊莉雅", "无面大臣"],
        )

        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "王城卫兵长",
        )
        guard = next(
            item for item in catalog if item["npc_action_type"] == "Guard"
        )

        self.assertEqual(guard["guarded_targets"], ["无面大臣"])

    def test_full_turn_ally_targets_enemy_and_can_guard_pc(self) -> None:
        ally = Character(
            name="白花巡守",
            attributes={"DEX": 10, "INS": 8, "MIG": 8, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 10, "magic": 8},
            weapon_accuracy_attributes=["DEX", "MIG"],
            weapon_damage=10,
            weapon_type="physical",
            traits=["ally", "humanoid"],
        )
        self.characters.add(ally)
        self.conflict.register_enemy(
            "白花巡守",
            EnemyRank.SOLDIER,
        )
        self.conflict.start_scene(
            "王座厅",
            ["白花巡守", "王城卫兵长", "伊莉雅"],
            player_side=["伊莉雅", "白花巡守"],
            enemy_side=["王城卫兵长"],
        )

        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "白花巡守",
        )
        attack = next(
            item for item in catalog if item["npc_action_type"] == "Attack"
        )
        guard = next(
            item for item in catalog if item["npc_action_type"] == "Guard"
        )

        self.assertEqual(attack["targets"], ["王城卫兵长"])
        self.assertEqual(guard["guarded_targets"], ["伊莉雅"])

    def test_ranged_npc_attack_is_not_translated_as_melee(self) -> None:
        self.characters.get("王城卫兵长").weapon_range = "ranged"
        self.conflict.start_scene("王座厅", ["王城卫兵长", "伊莉雅"])

        action = self.rules.validate_action(
            self.panel(),
            "王城卫兵长",
            {
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "action_description": "卫兵长抬起弩机，瞄向伊莉雅。",
            },
        )

        self.assertFalse(action.parameters["is_melee"])

    def test_npc_owned_opportunity_uses_deterministic_safe_option(self) -> None:
        window = DecisionWindow(
            window_id="window-1",
            kind="critical_opportunity",
            owner="王城卫兵长",
            prompt="选择大成功机会。",
            options=[
                {"effect": "进展"},
                {"effect": "优势"},
            ],
            payload={},
        )

        action = self.rules.resolve_window(
            self.panel(),
            "王城卫兵长",
            window,
        )

        self.assertEqual(action.action_type, ActionType.TRIGGER_OPPORTUNITY)
        self.assertEqual(action.parameters["effect"], "优势")
        self.assertEqual(action.parameters["target"], "王城卫兵长")


if __name__ == "__main__":
    unittest.main()
