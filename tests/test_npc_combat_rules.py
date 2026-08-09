from __future__ import annotations

import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.combat_trait_manager import CombatTraitManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
)
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    ActionType,
    Affinity,
    Character,
    DecisionWindow,
    EnemyRank,
    GamePanel,
    NPCAbilityProfile,
    NPCAttackEffect,
    NPCAttackProfile,
    StatusEffect,
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

    def test_snapshot_exposes_typed_abilities_only_inside_npc_tactical_context(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_ability_profiles = ability_profiles_for_bestiary("巨齿百足虫")

        snapshot = self.rules.build_tactical_snapshot(
            self.panel(),
            "王城卫兵长",
        )

        ability = snapshot["actor_rules_profile"]["typed_abilities"][0]
        self.assertEqual(ability["name"], "蜷缩")
        self.assertEqual(ability["trigger"], "after_guard")
        self.assertEqual(ability["expires_on"], "owner_turn_start")

    def test_crisis_ability_upgrades_named_attack_to_multiattack(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_attacks = [
            NPCAttackProfile(
                attack_id="spear-thrust",
                name="长枪突刺",
                attributes=["MIG", "MIG"],
                damage_bonus=10,
            )
        ]
        enemy.npc_ability_profiles = [
            NPCAbilityProfile(
                ability_id="crisis-volley",
                name="危机连击",
                source_skill="危机效果",
                trigger="enter_crisis",
                effect_type="grant_multiattack",
                target_scope="self",
                attack_name="长枪突刺",
                multi_attack=2,
            )
        ]
        enemy.hp = enemy.max_hp // 2

        events = CombatTraitManager().after_damage(
            enemy,
            affinity=Affinity.NORMAL,
            damage=1,
            hp_before=enemy.max_hp // 2 + 1,
            triggering_actor="伊莉雅",
        )

        attack = next(
            item
            for item in self.rules.build_legal_action_catalog(
                self.panel(),
                "王城卫兵长",
            )
            if item["npc_action_type"] == "Attack"
        )
        self.assertIn(
            "npc_ability_enter_crisis",
            [event.event_type for event in events],
        )
        self.assertEqual(attack["attack_name"], "长枪突刺")
        self.assertEqual(attack["multi_attack"], 2)

    def test_bestiary_crisis_passives_modify_checks_attacks_and_affinities(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.hp = enemy.max_hp // 2
        enemy.npc_attacks = [
            NPCAttackProfile(
                attack_id="bear-hug",
                name="熊抱",
                attributes=["DEX", "MIG"],
                damage_bonus=10,
                damage_type="physical",
            )
        ]
        enemy.npc_ability_profiles = [
            *ability_profiles_for_bestiary("硕鼠"),
            *ability_profiles_for_bestiary("日光熊"),
            *ability_profiles_for_bestiary("魔导机兵")[:1],
        ]
        enemy.affinities.update(
            {"fire": Affinity.RESIST, "ice": Affinity.RESIST}
        )

        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "王城卫兵长",
        )
        attack = next(
            item for item in catalog if item["npc_action_type"] == "Attack"
        )
        hinder = next(
            item for item in catalog if item["npc_action_type"] == "Hinder"
        )

        self.assertEqual(attack["accuracy_modifier"], 3)
        self.assertEqual(attack["weapon_damage"], 15)
        self.assertEqual(attack["damage_type"], "fire")
        self.assertEqual(hinder["modifier"], 3)
        self.assertEqual(
            self.characters.effective_affinity("王城卫兵长", "fire"),
            Affinity.NORMAL,
        )

        enemy.hp = enemy.max_hp
        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "王城卫兵长",
        )
        attack = next(
            item for item in catalog if item["npc_action_type"] == "Attack"
        )
        self.assertEqual(attack["accuracy_modifier"], 0)
        self.assertEqual(attack["weapon_damage"], 10)
        self.assertEqual(attack["damage_type"], "physical")
        self.assertEqual(
            self.characters.effective_affinity("王城卫兵长", "fire"),
            Affinity.RESIST,
        )

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

    def test_validate_attack_requires_authoritative_damage_and_status_choices(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_attacks = [
            NPCAttackProfile(
                attack_id="seasonal-flame",
                name="季焰",
                attributes=["INS", "WLP"],
                damage_bonus=5,
                damage_type="physical",
                damage_type_options=["fire", "ice"],
                status_options_on_hit=[
                    StatusEffect.DAZED,
                    StatusEffect.SLOW,
                ],
            )
        ]

        with self.assertRaisesRegex(ValueError, "选择合法的伤害类型"):
            self.rules.validate_action(
                self.panel(),
                "王城卫兵长",
                {
                    "npc_action_type": "Attack",
                    "attack_name": "季焰",
                    "target": "伊莉雅",
                    "action_description": "卫兵长将季节魔力压进枪尖。",
                },
            )

        action = self.rules.validate_action(
            self.panel(),
            "王城卫兵长",
            {
                "npc_action_type": "Attack",
                "attack_name": "季焰",
                "target": "伊莉雅",
                "chosen_damage_type": "ice",
                "chosen_status": "slow",
                "action_description": "卫兵长将寒意压进枪尖，刺向伊莉雅。",
            },
        )

        self.assertEqual(action.parameters["damage_type"], "ice")
        self.assertEqual(action.parameters["status_effect_on_hit"], "slow")

    def test_previous_guard_bonus_is_exposed_without_mutating_the_card(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_attacks = [
            NPCAttackProfile(
                attack_id="curved-cut",
                name="曲面切割",
                attributes=["DEX", "MIG"],
                damage_bonus=5,
                bonus_if_previous_guard=5,
            ),
            NPCAttackProfile(
                attack_id="bite",
                name="巨颚横斩",
                attributes=["DEX", "MIG"],
                damage_bonus=5,
            ),
        ]
        enemy.npc_skill_effects["previous_action_guarded"] = True

        catalog = self.rules.build_legal_action_catalog(
            self.panel(),
            "王城卫兵长",
        )
        attacks = {
            item["attack_name"]: item
            for item in catalog
            if item["npc_action_type"] == "Attack"
        }

        self.assertEqual(attacks["曲面切割"]["weapon_damage"], 10)
        self.assertEqual(attacks["巨颚横斩"]["weapon_damage"], 5)
        self.assertEqual(enemy.npc_attacks[0].damage_bonus, 5)

    def test_guard_terrain_must_come_from_typed_bestiary_profile(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_ability_profiles = ability_profiles_for_bestiary("轰炮蚁")

        guard = next(
            item
            for item in self.rules.build_legal_action_catalog(
                self.panel(),
                "王城卫兵长",
            )
            if item["npc_action_type"] == "Guard"
        )
        self.assertEqual(guard["terrain_options"], ["岩石", "沙地", "泥地"])

        with self.assertRaisesRegex(ValueError, "不能凭空使用地形"):
            self.rules.validate_action(
                self.panel(),
                "王城卫兵长",
                {
                    "npc_action_type": "Guard",
                    "terrain": "浅水",
                    "action_description": "卫兵长在浅水里蜷伏防御。",
                },
            )

        action = self.rules.validate_action(
            self.panel(),
            "王城卫兵长",
            {
                "npc_action_type": "Guard",
                "terrain": "岩石",
                "action_description": "卫兵长借岩层掘地防御。",
            },
        )
        self.assertEqual(action.parameters["terrain"], "岩石")

    def test_validate_attack_carries_structured_hit_effects_from_card(self) -> None:
        enemy = self.characters.get("王城卫兵长")
        enemy.npc_attacks = [
            NPCAttackProfile(
                attack_id="draining-cut",
                name="汲取斩",
                attributes=["DEX", "MIG"],
                damage_bonus=5,
                conditional_damage_bonus=5,
                conditional_target_statuses=[StatusEffect.SLOW],
                recover_hp_fraction=0.5,
                recover_mp_on_hit=5,
                target_mp_loss=10,
                target_ip_loss=1,
                self_hp_loss_if_all_miss=20,
                effects=[
                    NPCAttackEffect(
                        effect_type="action_restriction",
                        action_types=["Objective"],
                    )
                ],
            )
        ]

        action = self.rules.validate_action(
            self.panel(),
            "王城卫兵长",
            {
                "npc_action_type": "Attack",
                "attack_name": "汲取斩",
                "target": "伊莉雅",
                "action_description": "卫兵长以带有汲取力的刀锋扫向伊莉雅。",
            },
        )

        self.assertEqual(action.parameters["conditional_damage_bonus"], 5)
        self.assertEqual(action.parameters["conditional_target_statuses"], ["slow"])
        self.assertEqual(action.parameters["recover_hp_fraction"], 0.5)
        self.assertEqual(action.parameters["recover_mp_on_hit"], 5)
        self.assertEqual(action.parameters["target_mp_loss"], 10)
        self.assertEqual(action.parameters["target_ip_loss"], 1)
        self.assertEqual(action.parameters["self_hp_loss_if_all_miss"], 20)
        self.assertEqual(
            action.parameters["npc_attack_effects"][0]["effect_type"],
            "action_restriction",
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
