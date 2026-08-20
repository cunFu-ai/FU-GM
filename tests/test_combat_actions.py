import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.bestiary_runtime_profiles import (
    ability_profiles_for_bestiary,
)
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    Action,
    ActionType,
    Affinity,
    Character,
    Clock,
    EffectTiming,
    NPCAbilityProfile,
    NPCAttackProfile,
    StatusEffect,
)
from fu_gm.spellbook import get_spell_definition


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        value = self.values.pop(0)
        if value < low or value > high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value


class CombatActionTests(unittest.TestCase):
    def test_npc_act_infers_attack_when_subaction_missing(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="伊莉雅",
                attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 6},
                max_hp=50,
                hp=50,
                max_mp=35,
                mp=35,
                traits=["pc"],
            )
        )
        characters.add(
            Character(
                name="财团机兵",
                attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
                max_hp=60,
                hp=60,
                max_mp=35,
                mp=35,
                traits=["npc"],
                weapon_damage=5,
            )
        )
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6])
        clocks = ClockManager()
        conflict = ConflictManager(characters)
        interceptor = ActionInterceptor(rules, characters, clocks, conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "财团机兵",
                    "target": "伊莉雅",
                    "attributes": ["DEX", "MIG"],
                    "damage_type": "physical",
                },
            )
        )

        self.assertIn("成功", resolution.rules_text)
        self.assertLess(characters.get("伊莉雅").hp, 50)

    def test_npc_random_damage_attack_rolls_type_in_rules_layer(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 5, "magic": 5},
            affinities={"fire": Affinity.WEAK},
            traits=["pc"],
        )
        lamp = Character(
            name="魔法提灯",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=55,
            mp=55,
            weapon_damage=5,
            traits=["enemy", "构装体"],
        )
        characters.add(target)
        characters.add(lamp)
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 6, 5])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "魔法提灯",
                    "npc_action_type": "Attack",
                    "target": "伊莉雅",
                    "attributes": ["DEX", "INS"],
                    "weapon_damage": 5,
                    "random_damage_types": [
                        "lightning",
                        "lightning",
                        "fire",
                        "fire",
                        "ice",
                        "ice",
                    ],
                },
            )
        )

        self.assertEqual(resolution.payload["random_damage_type_roll"], 4)
        self.assertEqual(resolution.payload["random_damage_type"], "fire")
        self.assertEqual(resolution.payload["roll"].damage_type, "fire")
        self.assertEqual(characters.get("伊莉雅").hp, 28)

    def test_npc_conditional_damage_is_evaluated_per_target(self) -> None:
        characters = CharacterManager()
        slowed = Character(
            name="迟缓目标",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 5, "magic": 5},
            statuses={StatusEffect.SLOW},
            traits=["pc"],
        )
        steady = Character(
            name="正常目标",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 5, "magic": 5},
            traits=["pc"],
        )
        attacker = Character(
            name="碎响小丑",
            attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=50,
            mp=50,
            weapon_damage=5,
            traits=["enemy"],
        )
        for character in (slowed, steady, attacker):
            characters.add(character)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "碎响小丑",
                    "npc_action_type": "Attack",
                    "target": "迟缓目标",
                    "targets": ["迟缓目标", "正常目标"],
                    "attributes": ["DEX", "INS"],
                    "weapon_damage": 5,
                    "conditional_damage_bonus": 5,
                    "conditional_target_statuses": ["slow"],
                },
            )
        )

        self.assertEqual(characters.get("迟缓目标").hp, 32)
        self.assertEqual(characters.get("正常目标").hp, 37)

    def test_npc_attack_hit_effects_use_actual_damage_and_update_resources(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="目标",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=30,
            max_inventory_points=6,
            inventory_points=6,
            defenses={"physical": 5, "magic": 5},
            traits=["pc"],
        )
        attacker = Character(
            name="汲取者",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=20,
            max_mp=50,
            mp=40,
            weapon_damage=5,
            traits=["enemy"],
        )
        characters.add(target)
        characters.add(attacker)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "汲取者",
                    "npc_action_type": "Attack",
                    "target": "目标",
                    "attributes": ["DEX", "MIG"],
                    "weapon_damage": 5,
                    "recover_hp_fraction": 0.5,
                    "recover_mp_on_hit": 5,
                    "target_mp_loss": 10,
                    "target_ip_loss": 2,
                },
            )
        )

        self.assertEqual(characters.get("目标").hp, 37)
        self.assertEqual(characters.get("汲取者").hp, 26)
        self.assertEqual(characters.get("汲取者").mp, 45)
        self.assertEqual(characters.get("目标").mp, 20)
        self.assertEqual(characters.get("目标").inventory_points, 4)
        self.assertEqual(len(resolution.payload["npc_attack_hit_effects"]), 4)

    def test_npc_all_miss_recoil_only_triggers_when_every_target_misses(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="高防目标",
            attributes={"DEX": 12, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 12, "magic": 8},
            traits=["pc"],
        )
        attacker = Character(
            name="岩躯野猪",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 6},
            max_hp=70,
            hp=70,
            max_mp=40,
            mp=40,
            weapon_damage=15,
            traits=["enemy"],
        )
        characters.add(target)
        characters.add(attacker)
        engine = RulesEngine()
        engine._rng = FakeRandom([1, 2])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "岩躯野猪",
                    "npc_action_type": "Attack",
                    "target": "高防目标",
                    "attributes": ["DEX", "MIG"],
                    "weapon_damage": 15,
                    "self_hp_loss_if_all_miss": 20,
                },
            )
        )

        self.assertFalse(resolution.payload["roll"].success)
        self.assertEqual(characters.get("岩躯野猪").hp, 50)
        self.assertEqual(
            resolution.payload["npc_all_miss_self_damage"].amount,
            -20,
        )

    def test_npc_fire_attack_temporarily_suppresses_existing_resistance(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="火抗目标",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 5, "magic": 5},
            affinities={"fire": Affinity.RESIST},
            traits=["pc"],
        )
        attacker = Character(
            name="爆炎元素",
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 10},
            max_hp=60,
            hp=60,
            max_mp=60,
            mp=60,
            traits=["enemy"],
        )
        characters.add(target)
        characters.add(attacker)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "熔炉",
            ["爆炎元素", "火抗目标"],
            player_side=["火抗目标"],
            enemy_side=["爆炎元素"],
        )
        conflict.begin_current_turn()
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "爆炎元素",
                    "npc_action_type": "Attack",
                    "target": "火抗目标",
                    "attributes": ["DEX", "WLP"],
                    "weapon_damage": 10,
                    "damage_type": "fire",
                    "attack_id": "fire-stream",
                    "attack_name": "火焰射流",
                    "npc_attack_effects": [
                        {
                            "effect_type": "suppress_resistance",
                            "trigger": "on_hit",
                            "damage_type": "fire",
                            "expires_on": EffectTiming.OWNER_TURN_END.value,
                        }
                    ],
                },
            )
        )

        self.assertEqual(
            characters.effective_affinity("火抗目标", "fire"),
            Affinity.NORMAL,
        )
        conflict.next_turn()
        self.assertEqual(
            characters.effective_affinity("火抗目标", "fire"),
            Affinity.NORMAL,
        )
        conflict.begin_current_turn()
        conflict.next_turn()
        conflict.begin_current_turn()
        conflict.next_turn()
        self.assertEqual(
            characters.effective_affinity("火抗目标", "fire"),
            Affinity.RESIST,
        )

    def test_npc_status_bound_attack_effects_clear_with_their_status(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="探险者",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 5, "magic": 5},
            traits=["pc"],
        )
        attacker = Character(
            name="木乃伊",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 10},
            max_hp=90,
            hp=90,
            max_mp=70,
            mp=70,
            traits=["enemy"],
        )
        characters.add(target)
        characters.add(attacker)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "古墓",
            ["木乃伊", "探险者"],
            player_side=["探险者"],
            enemy_side=["木乃伊"],
        )
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 7])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "木乃伊",
                    "npc_action_type": "Attack",
                    "target": "探险者",
                    "attributes": ["MIG", "WLP"],
                    "weapon_damage": 10,
                    "status_effect_on_hit": "slow",
                    "npc_attack_effects": [
                        {
                            "effect_type": "affinity_while_status",
                            "trigger": "on_hit",
                            "required_status": "slow",
                            "damage_types": ["physical", "fire"],
                            "affinity": "weak",
                            "expires_on": "scene_end",
                        },
                        {
                            "effect_type": "action_restriction_while_status",
                            "trigger": "on_hit",
                            "required_status": "slow",
                            "action_types": ["Guard"],
                            "expires_on": "scene_end",
                        },
                    ],
                },
            )
        )

        self.assertIn(StatusEffect.SLOW, characters.get("探险者").statuses)
        self.assertEqual(
            characters.effective_affinity("探险者", "physical"),
            Affinity.WEAK,
        )
        with self.assertRaisesRegex(ValueError, "禁止"):
            interceptor.resolve(Action(ActionType.GUARD, {"actor": "探险者"}))

        self.assertTrue(conflict.remove_status("探险者", StatusEffect.SLOW))
        self.assertEqual(
            characters.effective_affinity("探险者", "physical"),
            Affinity.NORMAL,
        )
        interceptor.resolve(Action(ActionType.GUARD, {"actor": "探险者"}))

    def test_npc_conditional_action_penalty_and_after_attack_flight_loss(self) -> None:
        characters = CharacterManager()
        target = Character(
            name="眩晕目标",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 12, "magic": 5},
            statuses=[StatusEffect.DAZED],
            traits=["pc"],
        )
        attacker = Character(
            name="锋翼魔眼",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=60,
            hp=60,
            max_mp=40,
            mp=40,
            traits=["enemy", "飞行"],
        )
        characters.add(target)
        characters.add(attacker)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "高塔",
            ["锋翼魔眼", "眩晕目标"],
            player_side=["眩晕目标"],
            enemy_side=["锋翼魔眼"],
        )
        conflict.begin_current_turn()
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        interceptor.resolve(
            Action(
                ActionType.NPCACT,
                {
                    "actor": "锋翼魔眼",
                    "npc_action_type": "Attack",
                    "target": "眩晕目标",
                    "attributes": ["DEX", "WLP"],
                    "weapon_damage": 5,
                    "npc_attack_effects": [
                        {
                            "effect_type": "action_penalty",
                            "trigger": "on_hit",
                            "required_status": "dazed",
                            "amount": 1,
                        },
                        {
                            "effect_type": "suppress_trait",
                            "trigger": "after_attack",
                            "target_scope": "self",
                            "trait": "飞行",
                            "expires_on": "owner_turn_start",
                        },
                    ],
                },
            )
        )

        self.assertEqual(conflict.state.action_penalties["眩晕目标"], 1)
        self.assertFalse(interceptor._flight_is_active(attacker))

    def test_npc_guard_marks_next_attack_bonus_for_typed_card(self) -> None:
        characters = CharacterManager()
        centipede = Character(
            name="巨齿百足虫",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=60,
            hp=60,
            max_mp=45,
            mp=45,
            traits=["enemy"],
            npc_attacks=[
                NPCAttackProfile(
                    attack_id="curved-cut",
                    name="曲面切割",
                    attributes=["DEX", "MIG"],
                    damage_bonus=5,
                    bonus_if_previous_guard=5,
                )
            ],
        )
        characters.add(centipede)
        interceptor = ActionInterceptor(
            RulesEngine(),
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        interceptor.resolve(
            Action(ActionType.GUARD, {"actor": "巨齿百足虫"})
        )

        self.assertTrue(
            centipede.npc_skill_effects["previous_action_guarded"]
        )

    def test_investigate_can_target_scene_object_without_character_sheet(self) -> None:
        characters = CharacterManager()
        investigator = Character(
            name="露米娅",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 8},
            max_hp=35,
            hp=35,
            max_mp=50,
            mp=50,
            traits=["pc"],
        )
        characters.add(investigator)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        world_state = WorldState()
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), world_state)

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.INVESTIGATE,
                parameters={
                    "actor": "露米娅",
                    "target": "可疑宝箱",
                    "attributes": ["INS", "INS"],
                    "clues": ["锁孔里有星辰记忆的紫光。"],
                },
            )
        )

        self.assertEqual(resolution.payload["scene_object"], "可疑宝箱")
        self.assertTrue(resolution.payload["roll"].success)
        self.assertTrue(any("可疑宝箱" in item for item in world_state.memories))

    def test_scene_object_investigation_does_not_invent_a_generic_clue(self) -> None:
        characters = CharacterManager()
        investigator = Character(
            name="露米娅",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 8},
            max_hp=35,
            hp=35,
            max_mp=50,
            mp=50,
            traits=["pc"],
        )
        characters.add(investigator)
        engine = RulesEngine()
        engine._rng = FakeRandom([7, 7])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.INVESTIGATE,
                parameters={
                    "actor": "露米娅",
                    "target": "财团车辙",
                    "attributes": ["INS", "INS"],
                },
            )
        )

        self.assertEqual(resolution.payload["information"], [])
        self.assertFalse(
            any("调查场景线索 财团车辙" in item for item in interceptor.world_state.memories)
        )

    def test_investigate_missing_target_defaults_to_current_clue(self) -> None:
        characters = CharacterManager()
        investigator = Character(
            name="露米娅",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 8},
            max_hp=35,
            hp=35,
            max_mp=50,
            mp=50,
            traits=["pc"],
        )
        characters.add(investigator)
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 4])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.INVESTIGATE,
                parameters={"actor": "露米娅", "attributes": ["INS", "INS"]},
            )
        )

        self.assertEqual(resolution.payload["scene_object"], "当前线索")
        self.assertTrue(resolution.payload["roll"].success)

    def test_investigate_character_reveals_rulebook_thresholds(self) -> None:
        characters = CharacterManager()
        investigator = Character(
            name="露米娅",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 8},
            max_hp=35,
            hp=35,
            max_mp=50,
            mp=50,
            traits=["pc"],
        )
        enemy = Character(
            name="青铜魔像",
            level=15,
            attributes={"DEX": 6, "MIG": 12, "INS": 8, "WLP": 6},
            max_hp=100,
            hp=80,
            max_mp=45,
            mp=30,
            defenses={"physical": 12, "magic": 9},
            affinities={"lightning": Affinity.WEAK, "poison": Affinity.IMMUNE},
            traits=["enemy", "构装体", "沉重"],
            abilities=["旋风"],
            spells=["碎石弹幕"],
            weapon_damage=10,
            weapon_type="physical",
        )
        characters.add(investigator)
        characters.add(enemy)
        engine = RulesEngine()
        engine._rng = FakeRandom([7, 7])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.INVESTIGATE,
                parameters={"actor": "露米娅", "target": "青铜魔像", "attributes": ["INS", "INS"]},
            )
        )

        information = "；".join(resolution.payload["information"])
        self.assertIn("等级/物种：15级，构装体", information)
        self.assertIn("最大 HP/MP：100，45", information)
        self.assertIn("特质：enemy、构装体、沉重", information)
        self.assertIn("物防/魔防：12/9", information)
        self.assertIn("雷系:弱点", information)
        self.assertIn("毒系:免疫", information)
        self.assertIn("基础攻击：", information)
        self.assertIn("技能：旋风", information)
        self.assertIn("法术：碎石弹幕", information)

    def test_hinder_missing_target_treats_current_threat_as_scene_object(self) -> None:
        characters = CharacterManager()
        guardian = Character(
            name="瑟伦",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(guardian)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        world_state = WorldState()
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), world_state)

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.HINDER,
                parameters={
                    "actor": "瑟伦",
                    "attributes": ["MIG", "WLP"],
                    "reasoning": "举盾吸引帝国侦察队注意力。",
                },
            )
        )

        self.assertEqual(resolution.payload["scene_object"], "当前威胁")
        self.assertTrue(resolution.payload["roll"].success)
        self.assertTrue(any("当前威胁" in item for item in world_state.memories))

    def test_npcact_hinder_missing_target_uses_scene_object_fallback(self) -> None:
        characters = CharacterManager()
        npc = Character(
            name="星匣守卫",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["enemy"],
        )
        characters.add(npc)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        world_state = WorldState()
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), world_state)

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": "星匣守卫",
                    "npc_action_type": "Hinder",
                    "attributes": ["INS", "WLP"],
                },
            )
        )

        self.assertEqual(resolution.payload["scene_object"], "当前威胁")
        self.assertTrue(resolution.payload["roll"].success)

    def test_heal_and_barrier_scale_by_actor_level(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            level=20,
            attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=100,
            mp=100,
            traits=["pc"],
        )
        target = Character(
            name="露米娅",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 8},
            max_hp=100,
            hp=20,
            max_mp=40,
            mp=40,
            defenses={"physical": 9, "magic": 9},
            traits=["pc"],
        )
        characters.add(caster)
        characters.add(target)
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), ConflictManager(characters), WorldState())

        heal = interceptor.resolve(
            Action(action_type=ActionType.SPELL, parameters={"actor": "米菈", "target": "露米娅", "spell_name": "治愈术"})
        )
        barrier = interceptor.resolve(
            Action(action_type=ActionType.SPELL, parameters={"actor": "米菈", "target": "露米娅", "spell_name": "屏障"})
        )

        self.assertEqual(heal.payload["healing_change"].amount, 50)
        self.assertEqual(characters.get("露米娅").hp, 70)
        self.assertEqual(characters.effective_defense("露米娅", "physical"), 13)
        self.assertEqual(barrier.payload["spell_effect"].data["defense_floor"]["physical"], 13)

    def test_weapon_enchant_can_make_immediate_opportunity_attack(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=60,
            mp=60,
            weapon_damage=4,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
        )
        enemy = Character(
            name="暗影兽",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            defenses={"physical": 8, "magic": 8},
            affinities={"light": Affinity.WEAK},
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(enemy)
        rules = RulesEngine()
        rules._rng = FakeRandom([5, 5])
        interceptor = ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "米菈",
                    "target": "米菈",
                    "spell_name": "魂能武器",
                    "opportunity_target": "暗影兽",
                },
            )
        )

        self.assertEqual(characters.effective_weapon_damage_type("米菈"), "light")
        self.assertIn("opportunity_attack", resolution.payload)
        self.assertEqual(characters.get("暗影兽").hp, 22)

    def test_entropy_fixed_loss_and_time_stasis_are_hard_rules(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=100,
            mp=100,
            traits=["pc"],
        )
        enemy = Character(
            name="帝国机甲",
            level=20,
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=80,
            hp=80,
            max_mp=20,
            mp=20,
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(enemy)
        conflict = ConflictManager(characters)
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        omega = interceptor.resolve(
            Action(action_type=ActionType.SPELL, parameters={"actor": "米菈", "target": "帝国机甲", "spell_name": "终焉降临"})
        )
        stasis = interceptor.resolve(
            Action(action_type=ActionType.SPELL, parameters={"actor": "米菈", "target": "帝国机甲", "spell_name": "时空静滞"})
        )

        self.assertEqual(omega.payload["fixed_hp_loss"].amount, -30)
        self.assertEqual(characters.get("帝国机甲").hp, 50)
        self.assertEqual(conflict.state.action_penalties["帝国机甲"], 1)
        self.assertIn("少执行 1 次行动", stasis.rules_text)

    def test_drain_spell_does_not_restore_when_target_resource_hits_zero(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=40,
            hp=10,
            max_mp=60,
            mp=60,
            defenses={"physical": 10, "magic": 10},
            traits=["pc"],
        )
        enemy = Character(
            name="影兽",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=5,
            max_mp=20,
            mp=20,
            defenses={"physical": 8, "magic": 8},
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(enemy)
        rules = RulesEngine()
        rules._rng = FakeRandom([6, 6])
        interceptor = ActionInterceptor(rules, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(action_type=ActionType.SPELL, parameters={"actor": "米菈", "target": "影兽", "spell_name": "活力汲取"})
        )

        self.assertEqual(characters.get("影兽").hp, 0)
        self.assertEqual(characters.get("米菈").hp, 10)
        self.assertNotIn("drain_change", resolution.payload)

    def test_request_roll_against_scene_object_does_not_damage_actor(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="诺雅",
            attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        characters.add(actor)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        world_state = WorldState()
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), world_state)

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "诺雅",
                    "target": "入口歌声",
                    "attributes": ["INS", "WLP"],
                    "target_number": 10,
                    "weapon_damage": 20,
                },
            )
        )

        self.assertEqual(characters.get("诺雅").hp, 35)
        self.assertEqual(resolution.payload["scene_object"], "入口歌声")
        self.assertTrue(any("入口歌声" in item for item in world_state.memories))

    def test_spell_can_target_scene_object_without_character_sheet(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="诺雅",
            attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        characters.add(caster)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 6])
        world_state = WorldState()
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), world_state)

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "诺雅",
                    "target": "迷宫入口的星纹封印",
                    "mp_cost": 5,
                    "attributes": ["INS", "WLP"],
                    "effect": "安抚入口残留的灵魂回路。",
                },
            )
        )

        self.assertEqual(characters.get("诺雅").mp, 35)
        self.assertEqual(resolution.payload["scene_object"], "迷宫入口的星纹封印")
        self.assertTrue(resolution.payload["ad_hoc_scene_spell"])
        self.assertTrue(resolution.payload["roll"].success)

    def test_guard_sets_cover_and_makes_ally_an_illegal_melee_target(self) -> None:
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
            weapon_damage=5,
            traits=["enemy"],
        )
        for character in (guardian, ally, enemy):
            characters.add(character)

        conflict = ConflictManager(characters)
        conflict.start_scene(
            "桥头鏖战",
            ["钢盾骑士", "露琪亚", "帝国暗骑士"],
            player_side=["钢盾骑士", "露琪亚"],
            enemy_side=["帝国暗骑士"],
        )
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())
        guard_resolution = interceptor.resolve(
            Action(
                action_type=ActionType.GUARD,
                parameters={"actor": "钢盾骑士", "guarded_target": "露琪亚"},
            )
        )
        self.assertTrue(characters.get("钢盾骑士").guarding)
        self.assertEqual(guard_resolution.payload["guarded_target"], "露琪亚")

        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())
        with self.assertRaisesRegex(ValueError, "不能成为近战攻击的目标"):
            interceptor.resolve(
                Action(
                    action_type=ActionType.ATTACK,
                    parameters={
                        "actor": "帝国暗骑士",
                        "target": "露琪亚",
                        "attributes": ["DEX", "MIG"],
                        "is_melee": True,
                    },
                )
            )

        attack_resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={
                    "actor": "帝国暗骑士",
                    "target": "钢盾骑士",
                    "attributes": ["DEX", "MIG"],
                    "is_melee": True,
                },
            )
        )
        self.assertEqual(attack_resolution.payload["roll"].target, "钢盾骑士")
        self.assertLess(characters.get("钢盾骑士").hp, 40)
        self.assertEqual(characters.get("露琪亚").hp, 30)

    def test_bestiary_curl_up_grants_temporary_physical_immunity(self) -> None:
        characters = CharacterManager()
        centipede = Character(
            name="巨齿百足虫",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=60,
            hp=60,
            max_mp=45,
            mp=45,
            traits=["enemy", "野兽"],
            npc_ability_profiles=ability_profiles_for_bestiary("巨齿百足虫"),
        )
        foe = Character(
            name="冒险者",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        characters.add(centipede)
        characters.add(foe)
        conflict = ConflictManager(characters)
        conflict.start_scene("地穴", ["巨齿百足虫", "冒险者"])
        interceptor = ActionInterceptor(
            RulesEngine(), characters, ClockManager(), conflict, WorldState()
        )

        resolution = interceptor.resolve(
            Action(ActionType.GUARD, {"actor": "巨齿百足虫"})
        )

        self.assertEqual(
            characters.get("巨齿百足虫").temporary_affinities["physical"],
            Affinity.IMMUNE,
        )
        self.assertTrue(
            any(effect.source == "蜷缩" for effect in conflict.state.active_effects)
        )
        self.assertEqual(
            resolution.payload["npc_ability_results"][0]["applied_targets"],
            ["巨齿百足虫"],
        )

        conflict.next_turn()
        conflict.next_turn()
        conflict.begin_current_turn()

        self.assertNotIn(
            "physical", characters.get("巨齿百足虫").temporary_affinities
        )

    def test_bestiary_broad_cap_resists_all_damage_for_guarded_ally(self) -> None:
        characters = CharacterManager()
        fungus = Character(
            name="幻菇人",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=50,
            hp=50,
            max_mp=55,
            mp=55,
            traits=["enemy", "植物"],
            npc_ability_profiles=ability_profiles_for_bestiary("幻菇人"),
        )
        ally = Character(
            name="幼菇",
            attributes={"DEX": 6, "MIG": 6, "INS": 6, "WLP": 6},
            max_hp=20,
            hp=20,
            max_mp=20,
            mp=20,
            traits=["enemy", "植物"],
        )
        for character in (fungus, ally):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "菌窟冲突",
            ["幻菇人", "幼菇"],
            player_side=[],
            enemy_side=["幻菇人", "幼菇"],
        )
        interceptor = ActionInterceptor(
            RulesEngine(), characters, ClockManager(), conflict, WorldState()
        )

        interceptor.resolve(
            Action(
                ActionType.GUARD,
                {"actor": "幻菇人", "guarded_target": "幼菇"},
            )
        )

        self.assertEqual(len(ally.temporary_affinities), 9)
        self.assertTrue(
            all(affinity == Affinity.RESIST for affinity in ally.temporary_affinities.values())
        )

    def test_bestiary_burrow_applies_and_expires_typed_guard_effects(self) -> None:
        characters = CharacterManager()
        ant = Character(
            name="轰炮蚁",
            attributes={"DEX": 10, "MIG": 10, "INS": 6, "WLP": 6},
            max_hp=70,
            hp=70,
            max_mp=40,
            mp=40,
            defenses={"physical": 10, "magic": 6},
            traits=["enemy", "野兽"],
            npc_ability_profiles=ability_profiles_for_bestiary("轰炮蚁"),
        )
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        characters.add(ant)
        characters.add(hero)
        conflict = ConflictManager(characters)
        conflict.start_scene(
            "岩层巢穴",
            ["轰炮蚁", "伊莉雅"],
            player_side=["伊莉雅"],
            enemy_side=["轰炮蚁"],
        )
        interceptor = ActionInterceptor(
            RulesEngine(), characters, ClockManager(), conflict, WorldState()
        )

        interceptor.resolve(
            Action(
                ActionType.GUARD,
                {"actor": "轰炮蚁", "terrain": "岩石"},
            )
        )

        self.assertEqual(characters.effective_defense("轰炮蚁", "physical"), 12)
        self.assertEqual(ant.temporary_affinities["earth"], Affinity.WEAK)

        conflict.next_turn()
        conflict.next_turn()
        conflict.begin_current_turn()

        self.assertEqual(characters.effective_defense("轰炮蚁", "physical"), 10)
        self.assertNotIn("earth", ant.temporary_affinities)

    def test_spell_consumes_mp_and_uses_magic_defense(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            defenses={"physical": 12, "magic": 13},
            affinities={"lightning": Affinity.WEAK},
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "帝国机甲",
                    "attributes": ["INS", "WLP"],
                    "mp_cost": 5,
                    "fixed_damage": 4,
                    "damage_type": "lightning",
                },
            )
        )

        self.assertEqual(characters.get("瓦莉亚").mp, 15)
        self.assertTrue(resolution.payload["roll"].success)
        self.assertEqual(resolution.payload["roll"].target_number, 13)
        self.assertLess(characters.get("帝国机甲").hp, 40)

    def test_named_attack_spell_uses_spellbook_definition(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            defenses={"physical": 12, "magic": 13},
            affinities={"lightning": Affinity.WEAK},
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "帝国机甲",
                    "spell_name": "落雷",
                },
            )
        )

        self.assertEqual(resolution.payload["spell_name"], "落雷")
        self.assertEqual(characters.get("瓦莉亚").mp, 15)
        self.assertEqual(resolution.payload["roll"].target_number, 13)
        self.assertEqual(resolution.payload["roll"].damage_type, "lightning")

    def test_npc_spell_uses_persisted_check_and_damage_bonuses(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="雷鸣术士",
            level=20,
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=80,
            max_mp=80,
            mp=80,
            traits=["enemy", "humanoid"],
            spells=["落雷"],
            npc_spell_check_bonus=5,
            npc_spell_damage_bonus=5,
            npc_spell_specific_damage_bonuses={"落雷": 5},
        )
        target = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 10, "magic": 13},
            traits=["pc"],
        )
        characters.add(caster)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 5])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "雷鸣术士",
                    "target": "伊莉雅",
                    "spell_name": "落雷",
                },
            )
        )

        roll = resolution.payload["roll"]
        self.assertEqual(roll.modifier, 5)
        self.assertEqual(roll.total, 14)
        self.assertEqual(roll.damage, 20)
        self.assertEqual(characters.get("伊莉雅").hp, 30)

    def test_ground_melee_cannot_target_active_flying_enemy(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 10, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            traits=["pc"],
            weapon_damage=5,
        )
        flyer = Character(
            name="棘刺鱼",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 7, "magic": 10},
            traits=["enemy", "飞行"],
            skills={"飞行": 1},
        )
        characters.add(hero)
        characters.add(flyer)
        interceptor = ActionInterceptor(
            RulesEngine(),
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        with self.assertRaisesRegex(ValueError, "无法用普通近战攻击"):
            interceptor.resolve(
                Action(
                    ActionType.ATTACK,
                    {
                        "actor": "伊莉雅",
                        "target": "棘刺鱼",
                        "is_melee": True,
                    },
                )
            )

    def test_flying_attacker_can_melee_active_flying_enemy(self) -> None:
        characters = CharacterManager()
        attacker = Character(
            name="翼骑士",
            attributes={"DEX": 10, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            traits=["pc", "飞行"],
            skills={"飞行": 1},
            weapon_damage=5,
        )
        target = Character(
            name="棘刺鱼",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 7, "magic": 10},
            traits=["enemy", "飞行"],
            skills={"飞行": 1},
        )
        characters.add(attacker)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "翼骑士",
                    "target": "棘刺鱼",
                    "is_melee": True,
                },
            )
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertLess(characters.get("棘刺鱼").hp, 50)

    def test_ground_melee_can_target_flying_enemy_in_crisis(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 10, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            traits=["pc"],
            weapon_damage=5,
        )
        flyer = Character(
            name="棘刺鱼",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 6},
            max_hp=50,
            hp=25,
            max_mp=40,
            mp=40,
            defenses={"physical": 7, "magic": 10},
            traits=["enemy", "飞行"],
            skills={"飞行": 1},
        )
        characters.add(hero)
        characters.add(flyer)
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "伊莉雅",
                    "target": "棘刺鱼",
                    "is_melee": True,
                },
            )
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertLess(characters.get("棘刺鱼").hp, 25)

    def test_weakness_damage_suppresses_flying_trait_until_round_end(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        flyer = Character(
            name="棘刺鱼",
            attributes={"DEX": 10, "MIG": 6, "INS": 10, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=40,
            mp=40,
            defenses={"physical": 10, "magic": 10},
            affinities={"lightning": Affinity.WEAK},
            traits=["enemy", "飞行"],
            skills={"飞行": 1},
        )
        characters.add(caster)
        characters.add(flyer)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        conflict = ConflictManager(characters)
        conflict.start_scene("海上伏击", ["瓦莉亚", "棘刺鱼"])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "棘刺鱼",
                    "spell_name": "落雷",
                },
            )
        )

        events = resolution.payload["combat_trait_events"]
        event_types = [event.event_type for event in events]
        self.assertIn("crisis_entered", event_types)
        self.assertIn("flight_suppressed", event_types)
        flight_event = next(event for event in events if event.event_type == "flight_suppressed")
        self.assertIn("飞行优势暂时失效", flight_event.summary)
        active = conflict.state.active_effects[0]
        self.assertEqual(active.effect_type, "trait_suppression")
        self.assertEqual(active.effect_key, "flight_suppressed")
        self.assertIn("飞行", active.data["suppressed_trait"])
        self.assertIn("flight_suppressed", "\n".join(conflict.format_combat_log(limit=4)))

    def test_last_stand_trait_opens_audit_window_before_defeat(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=30,
            mp=30,
            traits=["pc"],
            weapon_damage=14,
        )
        enemy = Character(
            name="爆燃魔偶",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=20,
            hp=12,
            max_mp=20,
            mp=20,
            defenses={"physical": 7, "magic": 7},
            traits=["enemy"],
            skills={"最后一搏": 1},
        )
        characters.add(hero)
        characters.add(enemy)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])
        conflict = ConflictManager(characters)
        conflict.start_scene("魔偶库房", ["伊莉雅", "爆燃魔偶"])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "伊莉雅", "target": "爆燃魔偶"},
            )
        )

        events = resolution.payload["combat_trait_events"]
        self.assertIn("last_stand_window", [event.event_type for event in events])
        self.assertIn("最后一搏窗口", next(event.summary for event in events if event.event_type == "last_stand_window"))
        self.assertIn("爆燃魔偶", conflict.state.defeated_combatants)

    def test_typed_last_stand_deals_fixed_damage_before_npc_defeat(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=30,
            mp=30,
            traits=["pc"],
            weapon_damage=14,
        )
        enemy = Character(
            name="爆燃魔偶",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=20,
            hp=12,
            max_mp=20,
            mp=20,
            defenses={"physical": 7, "magic": 7},
            traits=["enemy"],
            npc_ability_profiles=[
                NPCAbilityProfile(
                    ability_id="explosive-last-stand",
                    name="爆燃终曲",
                    source_skill="最后一搏",
                    trigger="zero_hp",
                    effect_type="fixed_damage",
                    target_scope="all_enemies",
                    amount=10,
                    damage_type="fire",
                    once_per_scene=True,
                )
            ],
        )
        characters.add(hero)
        characters.add(enemy)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])
        conflict = ConflictManager(characters)
        conflict.start_scene("魔偶库房", ["伊莉雅", "爆燃魔偶"])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "伊莉雅", "target": "爆燃魔偶"},
            )
        )

        self.assertEqual(characters.get("伊莉雅").hp, 40)
        self.assertIn("爆燃魔偶", conflict.state.defeated_combatants)
        result = next(
            item
            for item in resolution.payload["npc_ability_results"]
            if item["ability_id"] == "explosive-last-stand"
        )
        self.assertEqual(result["damage_results"][0]["target"], "伊莉雅")
        self.assertEqual(result["damage_results"][0]["amount"], 10)

    def test_bestiary_detonation_is_suppressed_by_ice_killing_damage(self) -> None:
        def resolve_with(damage_type: str):
            characters = CharacterManager()
            hero = Character(
                name="伊莉雅",
                attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
                max_hp=50,
                hp=50,
                max_mp=30,
                mp=30,
                traits=["pc"],
                weapon_damage=14,
            )
            elemental = Character(
                name="爆炎元素",
                attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 10},
                max_hp=20,
                hp=12,
                max_mp=20,
                mp=20,
                defenses={"physical": 7, "magic": 7},
                traits=["enemy", "elemental"],
                npc_ability_profiles=ability_profiles_for_bestiary("爆炎元素"),
            )
            characters.add(hero)
            characters.add(elemental)
            engine = RulesEngine()
            engine._rng = FakeRandom([6, 6])
            conflict = ConflictManager(characters)
            conflict.start_scene("熔炉", ["伊莉雅", "爆炎元素"])
            interceptor = ActionInterceptor(
                engine,
                characters,
                ClockManager(),
                conflict,
                WorldState(),
            )
            resolution = interceptor.resolve(
                Action(
                    action_type=ActionType.ATTACK,
                    parameters={
                        "actor": "伊莉雅",
                        "target": "爆炎元素",
                        "damage_type": damage_type,
                    },
                )
            )
            return characters, resolution

        physical_characters, physical = resolve_with("physical")
        ice_characters, ice = resolve_with("ice")

        self.assertEqual(physical_characters.get("伊莉雅").hp, 40)
        self.assertTrue(physical.payload.get("npc_ability_results"))
        self.assertEqual(ice_characters.get("伊莉雅").hp, 50)
        self.assertFalse(ice.payload.get("npc_ability_results"))

    def test_bestiary_crisis_cleanse_and_ancient_curse_resolve_in_runtime(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=30,
            mp=30,
            traits=["pc"],
            weapon_damage=10,
        )
        bandit = Character(
            name="强盗",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=60,
            hp=31,
            max_mp=45,
            mp=45,
            defenses={"physical": 6, "magic": 8},
            traits=["enemy", "humanoid"],
            statuses=[StatusEffect.SLOW, StatusEffect.ENRAGED],
            npc_ability_profiles=ability_profiles_for_bestiary("强盗"),
        )
        characters.add(hero)
        characters.add(bandit)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 5])
        conflict = ConflictManager(characters)
        conflict.start_scene("驿道", ["伊莉雅", "强盗"])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(ActionType.ATTACK, {"actor": "伊莉雅", "target": "强盗"})
        )

        self.assertNotIn(StatusEffect.SLOW, characters.get("强盗").statuses)
        self.assertIn(StatusEffect.ENRAGED, characters.get("强盗").statuses)
        cleansed = next(
            item
            for item in resolution.payload["npc_ability_results"]
            if item["ability_name"] == "暴躁脾气"
        )
        self.assertEqual(cleansed["cleared_statuses"]["强盗"], ["slow"])

        mummy = Character(
            name="木乃伊",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 10},
            max_hp=20,
            hp=12,
            max_mp=40,
            mp=40,
            defenses={"physical": 6, "magic": 8},
            traits=["enemy", "undead"],
            npc_ability_profiles=ability_profiles_for_bestiary("木乃伊"),
        )
        construct = Character(
            name="铜魔像",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["ally", "construct"],
        )
        characters.add(mummy)
        characters.add(construct)
        conflict.start_scene(
            "墓室",
            ["伊莉雅", "木乃伊", "铜魔像"],
            player_side=["伊莉雅", "铜魔像"],
            enemy_side=["木乃伊"],
        )
        engine._rng = FakeRandom([6, 5])

        interceptor.resolve(
            Action(ActionType.ATTACK, {"actor": "伊莉雅", "target": "木乃伊"})
        )

        self.assertIn(StatusEffect.SHAKEN, characters.get("伊莉雅").statuses)
        self.assertIn(StatusEffect.WEAKENED, characters.get("伊莉雅").statuses)
        self.assertNotIn(StatusEffect.SHAKEN, characters.get("铜魔像").statuses)

    def test_typed_reaction_damages_attacker_after_missed_attack(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="伊莉雅",
            attributes={"DEX": 6, "MIG": 6, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=30,
            mp=30,
            traits=["pc"],
        )
        enemy = Character(
            name="反击石像",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=50,
            hp=50,
            max_mp=20,
            mp=20,
            defenses={"physical": 12, "magic": 8},
            traits=["enemy"],
            npc_ability_profiles=[
                NPCAbilityProfile(
                    ability_id="miss-counter",
                    name="碎石反冲",
                    source_skill="反应",
                    trigger="attack_missed",
                    effect_type="fixed_damage",
                    target_scope="triggering_actor",
                    amount=5,
                    damage_type="physical",
                )
            ],
        )
        characters.add(hero)
        characters.add(enemy)
        engine = RulesEngine()
        engine._rng = FakeRandom([2, 3])
        conflict = ConflictManager(characters)
        conflict.start_scene("石像回廊", ["伊莉雅", "反击石像"])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "伊莉雅", "target": "反击石像"},
            )
        )

        self.assertFalse(resolution.payload["roll"].success)
        self.assertEqual(characters.get("伊莉雅").hp, 45)
        self.assertEqual(
            resolution.payload["npc_ability_results"][0]["ability_id"],
            "miss-counter",
        )

    def test_typed_spell_reaction_recovers_mp_after_spell_hit(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        enemy = Character(
            name="吞魔提灯",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=60,
            hp=60,
            max_mp=40,
            mp=10,
            defenses={"physical": 8, "magic": 8},
            traits=["enemy"],
            npc_ability_profiles=[
                NPCAbilityProfile(
                    ability_id="spell-siphon",
                    name="吞纳魔力",
                    source_skill="反应",
                    trigger="hit_by_spell",
                    effect_type="recover_mp",
                    target_scope="self",
                    amount=5,
                )
            ],
        )
        characters.add(caster)
        characters.add(enemy)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])
        interceptor = ActionInterceptor(
            engine,
            characters,
            ClockManager(),
            ConflictManager(characters),
            WorldState(),
        )

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "米菈",
                    "target": "吞魔提灯",
                    "spell_name": "落雷",
                },
            )
        )

        self.assertTrue(resolution.payload["roll"].success)
        self.assertEqual(characters.get("吞魔提灯").mp, 15)
        event = next(
            item
            for item in resolution.payload["combat_trait_events"]
            if item.data and item.data.get("ability_id") == "spell-siphon"
        )
        self.assertEqual(event.data["resource_change"]["after"], 15)

    def test_multi_target_attack_spell_rolls_once_and_charges_per_target(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        golem = Character(
            name="青铜魔像",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 6},
            max_hp=50,
            hp=50,
            max_mp=0,
            mp=0,
            defenses={"physical": 10, "magic": 10},
            affinities={"lightning": Affinity.WEAK},
            traits=["enemy"],
        )
        lantern = Character(
            name="魔法提灯",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=40,
            hp=40,
            max_mp=30,
            mp=30,
            defenses={"physical": 10, "magic": 12},
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(golem)
        characters.add(lantern)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 6])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "米菈",
                    "targets": ["青铜魔像", "魔法提灯"],
                    "spell_name": "闪电击",
                },
            )
        )

        self.assertEqual(characters.get("米菈").mp, 20)
        self.assertEqual(resolution.payload["hit_targets"], ["青铜魔像", "魔法提灯"])
        self.assertEqual(len(resolution.payload["damage_results"]), 2)
        self.assertEqual(characters.get("青铜魔像").hp, 8)
        self.assertEqual(characters.get("魔法提灯").hp, 19)
        self.assertTrue(resolution.payload["roll"].critical_success)

    def test_spellbook_includes_ju_yan_with_correct_definition(self) -> None:
        spell = get_spell_definition("巨岩")

        self.assertEqual(spell.name, "巨岩")
        self.assertEqual(spell.mp_cost, 20)
        self.assertEqual(spell.damage_type, "earth")
        self.assertTrue(spell.ignore_resist)
        self.assertIn("高值+15", spell.description)

    def test_spell_without_enough_mp_does_not_consume_mp(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=3,
            traits=["pc"],
        )
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            traits=["enemy"],
        )
        characters.add(caster)
        characters.add(target)
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "瓦莉亚", "target": "帝国机甲", "mp_cost": 5},
            )
        )

        self.assertTrue(resolution.payload["spell_failed"])
        self.assertEqual(characters.get("瓦莉亚").mp, 3)

    def test_scene_end_spell_effect_applies_and_clears(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(caster)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "瓦莉亚",
                    "spell_name": "魔导屏障",
                },
            )
        )

        self.assertIn("spell_effect", resolution.payload)
        self.assertEqual(characters.get("瓦莉亚").defense_bonuses["physical"], 2)
        self.assertEqual(characters.get("瓦莉亚").defense_bonuses["magic"], 2)

        conflict.end_scene()

        self.assertEqual(characters.get("瓦莉亚").defense_bonuses["physical"], 0)
        self.assertEqual(characters.get("瓦莉亚").defense_bonuses["magic"], 0)
        self.assertEqual(conflict.state.active_effects, [])

    def test_owner_turn_start_spell_effect_expires_on_caster_next_turn_start(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        ally = Character(
            name="莱因",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            traits=["enemy"],
        )
        for character in (caster, ally, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "莱因",
                    "spell_name": "守护咏唱",
                },
            )
        )

        self.assertEqual(characters.get("莱因").defense_bonuses["physical"], 2)
        conflict.next_turn()
        self.assertEqual(characters.get("莱因").defense_bonuses["physical"], 2)
        conflict.next_turn()
        self.assertEqual(characters.get("莱因").defense_bonuses["physical"], 2)

        conflict.begin_current_turn()
        self.assertEqual(characters.get("莱因").defense_bonuses["physical"], 0)

    def test_defense_buff_spell_changes_attack_target_number(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=10,
            mp=10,
            weapon_damage=5,
            traits=["enemy"],
        )
        for character in (caster, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "帝国机甲"])
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "瓦莉亚", "target": "瓦莉亚", "spell_name": "魔导屏障"},
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "帝国机甲", "target": "瓦莉亚", "attributes": ["DEX", "MIG"]},
            )
        )

        self.assertEqual(resolution.payload["roll"].target_number, 12)

    def test_affinity_buff_from_other_source_survives_when_one_effect_is_removed(self) -> None:
        characters = CharacterManager()
        for name in ("瓦莉亚", "索菲", "莱因"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=40,
                    hp=40,
                    max_mp=20,
                    mp=20,
                    traits=["pc"],
                )
            )
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["瓦莉亚", "索菲", "莱因"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "瓦莉亚", "target": "莱因", "spell_name": "元素护体"},
            )
        )
        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "索菲", "target": "莱因", "spell_name": "元素护体"},
            )
        )

        conflict.clear_effects("瓦莉亚", "affinity_buff", "spell:元素护体:莱因", "莱因")

        self.assertEqual(characters.get("莱因").temporary_affinities["lightning"], Affinity.RESIST)

    def test_heal_spell_restores_hp_without_exceeding_maximum(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=30,
            mp=30,
            traits=["pc"],
        )
        ally = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=12,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(caster)
        characters.add(ally)
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "瓦莉亚", "spell_name": "治愈"},
            )
        )

        self.assertEqual(characters.get("米菈").mp, 20)
        self.assertEqual(characters.get("瓦莉亚").hp, 40)
        self.assertEqual(resolution.payload["healing_change"].amount, 28)
        self.assertEqual(resolution.payload["spell_fixed_effect"]["base_amount"], 40)
        self.assertIn("规则恢复量 40", resolution.rules_text)

    def test_known_heal_spell_ignores_llm_supplied_heal_amount(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="赛璃",
            attributes={"DEX": 8, "MIG": 6, "INS": 10, "WLP": 8},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        ally = Character(
            name="伊莉雅",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 6},
            max_hp=80,
            hp=30,
            max_mp=35,
            mp=35,
            traits=["pc"],
        )
        characters.add(caster)
        characters.add(ally)
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "赛璃",
                    "target": "伊莉雅",
                    "spell_name": "治愈术",
                    "heal_amount": 17,
                },
            )
        )

        self.assertEqual(characters.get("伊莉雅").hp, 70)
        self.assertEqual(resolution.payload["healing_change"].amount, 40)
        self.assertEqual(resolution.payload["spell_fixed_effect"]["base_amount"], 40)

    def test_defense_floor_spell_changes_effective_defense(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=30,
            mp=30,
            defenses={"physical": 9, "magic": 11},
            traits=["pc"],
        )
        enemy = Character(
            name="帝国兵",
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=35,
            hp=35,
            max_mp=0,
            mp=0,
            weapon_damage=5,
            traits=["enemy"],
        )
        for character in (caster, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["米菈", "帝国兵"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "米菈", "spell_name": "护盾"},
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "帝国兵", "target": "米菈", "attributes": ["DEX", "MIG"]},
            )
        )

        self.assertEqual(resolution.payload["roll"].target_number, 12)

    def test_weapon_enchant_changes_attack_damage_type_and_clears_on_scene_end(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=30,
            mp=30,
            weapon_damage=6,
            weapon_type="physical",
            traits=["pc"],
        )
        target = Character(
            name="亡灵骑士",
            attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=35,
            hp=35,
            max_mp=0,
            mp=0,
            affinities={"light": Affinity.WEAK},
            traits=["enemy"],
        )
        for character in (caster, target):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("废都墓园", ["米菈", "亡灵骑士"])
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "米菈", "spell_name": "灵魂武器"},
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "米菈", "target": "亡灵骑士", "attributes": ["DEX", "MIG"]},
            )
        )

        self.assertEqual(characters.get("米菈").weapon_damage_type_override, "light")
        self.assertEqual(resolution.payload["roll"].damage_type, "light")
        conflict.end_scene()
        self.assertIsNone(characters.get("米菈").weapon_damage_type_override)

    def test_dispel_removes_existing_spell_effects(self) -> None:
        characters = CharacterManager()
        for character in (
            Character(
                name="米菈",
                attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=40,
                mp=40,
                traits=["pc"],
            ),
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
                max_hp=40,
                hp=40,
                max_mp=20,
                mp=20,
                traits=["pc"],
            ),
        ):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["米菈", "瓦莉亚"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "瓦莉亚", "spell_name": "守护咏唱"},
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "瓦莉亚", "spell_name": "消除"},
            )
        )

        self.assertEqual(characters.get("瓦莉亚").defense_bonuses["physical"], 0)
        self.assertIn("守护咏唱", resolution.payload["dispelled_effects"])

    def test_status_immunity_spell_blocks_matching_status(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        ally = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        enemy = Character(
            name="梦魇巫师",
            attributes={"DEX": 6, "MIG": 6, "INS": 10, "WLP": 10},
            max_hp=30,
            hp=30,
            max_mp=30,
            mp=30,
            traits=["enemy"],
        )
        for character in (caster, ally, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("幻梦剧场", ["米菈", "梦魇巫师"])
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8, 8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={
                    "actor": "米菈",
                    "target": "瓦莉亚",
                    "spell_name": "加强",
                    "chosen_status": "shaken",
                },
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.HINDER,
                parameters={
                    "actor": "梦魇巫师",
                    "target": "瓦莉亚",
                    "attributes": ["INS", "WLP"],
                    "status_effect": "shaken",
                },
            )
        )

        self.assertFalse(resolution.payload["status_applied"])
        self.assertNotIn(StatusEffect.SHAKEN, characters.get("瓦莉亚").statuses)

    def test_mercy_prevents_first_zero_hp(self) -> None:
        characters = CharacterManager()
        caster = Character(
            name="米菈",
            attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
            max_hp=35,
            hp=35,
            max_mp=40,
            mp=40,
            traits=["pc"],
        )
        ally = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=10,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        enemy = Character(
            name="黑骑士",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            weapon_damage=12,
            traits=["enemy"],
        )
        for character in (caster, ally, enemy):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("王城城门", ["米菈", "黑骑士"])
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "瓦莉亚", "spell_name": "慈悲"},
            )
        )
        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "黑骑士", "target": "瓦莉亚", "attributes": ["DEX", "MIG"]},
            )
        )

        self.assertEqual(characters.get("瓦莉亚").hp, 1)
        self.assertEqual(resolution.payload["conflict_event"].event_type, "survive_once")

    def test_acceleration_spell_opens_end_turn_choice_without_granting_generic_turn(self) -> None:
        characters = CharacterManager()
        for character in (
            Character(
                name="米菈",
                attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=40,
                mp=40,
                traits=["pc"],
            ),
            Character(
                name="帝国兵",
                attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
                max_hp=35,
                hp=35,
                max_mp=0,
                mp=0,
                traits=["enemy"],
            ),
        ):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["米菈", "帝国兵"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.SPELL,
                parameters={"actor": "米菈", "target": "米菈", "spell_name": "加速术"},
            )
        )

        self.assertEqual(characters.get("米菈").mp, 20)
        self.assertEqual(resolution.payload["spell_effect"].effect_type, "acceleration")
        self.assertEqual(resolution.payload["spell_name"], "加速术")
        self.assertIn("每个回合结束时", resolution.rules_text)
        self.assertEqual(conflict.next_turn(), "米菈")
        window = interceptor.decision_window_manager.find_pending(
            kind="acceleration_benefit",
            owner="米菈",
        )
        self.assertIsNotNone(window)
        self.assertTrue(window.blocking)
        self.assertEqual(conflict.state.pending_turn_end_actor, "米菈")

        declined = interceptor.resolve(
            Action(
                ActionType.RESOLVE_DECISION,
                {
                    "actor": "米菈",
                    "window_id": window.window_id,
                    "choice": "decline",
                    "selected_option": {"choice": "decline"},
                },
            )
        )

        self.assertIn("本回合不发动", declined.rules_text)
        self.assertIsNone(conflict.state.pending_turn_end_actor)
        self.assertEqual(conflict.next_turn(), "帝国兵")
        effect = next(effect for effect in conflict.state.active_effects if effect.source == "加速术")
        self.assertEqual(effect.data["benefits_used"], 0)

    def test_acceleration_ends_after_target_uses_two_end_turn_benefits(self) -> None:
        characters = CharacterManager()
        for character in (
            Character(
                name="米菈",
                attributes={"DEX": 8, "MIG": 6, "INS": 8, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=40,
                mp=40,
                traits=["pc"],
            ),
            Character(
                name="帝国兵",
                attributes={"DEX": 8, "MIG": 8, "INS": 6, "WLP": 6},
                max_hp=35,
                hp=35,
                max_mp=0,
                mp=0,
                traits=["enemy"],
            ),
        ):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["米菈", "帝国兵"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())
        interceptor.resolve(
            Action(
                ActionType.SPELL,
                {"actor": "米菈", "target": "米菈", "spell_name": "加速术"},
            )
        )

        for expected_count in (1, 2):
            self.assertEqual(conflict.next_turn(), "米菈")
            window = interceptor.decision_window_manager.find_pending(
                kind="acceleration_benefit",
                owner="米菈",
            )
            self.assertIsNotNone(window)
            interceptor.decision_window_manager.resolve(
                window_id=window.window_id,
                responder="米菈",
                resolution={"choice": "attack"},
            )
            completed = conflict.complete_acceleration_turn_end(
                "米菈",
                benefit_used=True,
                effect_key=str(window.payload["effect_key"]),
            )
            self.assertEqual(completed["benefits_used"], expected_count)
            self.assertEqual(completed["effect_expired"], expected_count == 2)
            if expected_count == 1:
                self.assertEqual(conflict.next_turn(), "帝国兵")
                self.assertEqual(conflict.next_turn(), "米菈")

        self.assertFalse(any(effect.source == "加速术" for effect in conflict.state.active_effects))

    def test_acceleration_followup_uses_normal_attack_rules_and_enforces_spell_cost_cap(self) -> None:
        characters = CharacterManager()
        for character in (
            Character(
                name="米菈",
                attributes={"DEX": 12, "MIG": 12, "INS": 10, "WLP": 10},
                max_hp=35,
                hp=35,
                max_mp=80,
                mp=80,
            ),
            Character(
                name="帝国兵",
                attributes={"DEX": 6, "MIG": 6, "INS": 6, "WLP": 6},
                max_hp=35,
                hp=35,
                max_mp=0,
                mp=0,
            ),
        ):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥激战", ["米菈", "帝国兵"])
        interceptor = ActionInterceptor(RulesEngine(), characters, ClockManager(), conflict, WorldState())
        interceptor.resolve(
            Action(
                ActionType.SPELL,
                {"actor": "米菈", "target": "米菈", "spell_name": "加速术"},
            )
        )
        conflict.next_turn()
        window = interceptor.decision_window_manager.find_pending(
            kind="acceleration_benefit",
            owner="米菈",
        )
        self.assertIsNotNone(window)

        rejected = interceptor.resolve(
            Action(
                ActionType.SPELL,
                {
                    "actor": "米菈",
                    "target": "帝国兵",
                    "target_explicit": True,
                    "spell_name": "焰流",
                    "_acceleration_window_id": window.window_id,
                    "opportunity_action": True,
                },
            )
        )
        self.assertTrue(rejected.payload["action_uncommitted"])
        self.assertEqual(characters.get("米菈").mp, 60)
        self.assertEqual(conflict.state.pending_turn_end_actor, "米菈")
        self.assertIsNotNone(
            interceptor.decision_window_manager.find_pending(window_id=window.window_id)
        )

        attack = interceptor.resolve(
            Action(
                ActionType.ATTACK,
                {
                    "actor": "米菈",
                    "target": "帝国兵",
                    "_acceleration_window_id": window.window_id,
                    "opportunity_action": True,
                },
            )
        )
        self.assertTrue(attack.payload["acceleration_benefit_used"])
        self.assertIn("【加速术】触发", attack.rules_text)
        self.assertIsNone(conflict.state.pending_turn_end_actor)
        self.assertEqual(conflict.next_turn(), "帝国兵")

    def test_hinder_applies_status_effect(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=0,
            mp=0,
            traits=["enemy"],
        )
        characters.add(actor)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        conflict = ConflictManager(characters)
        interceptor = ActionInterceptor(engine, characters, ClockManager(), conflict, WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.HINDER,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "帝国机甲",
                    "attributes": ["INS", "WLP"],
                    "status_effect": "shaken",
                },
            )
        )

        self.assertTrue(resolution.payload["status_applied"])
        self.assertIn(StatusEffect.SHAKEN, characters.get("帝国机甲").statuses)

    def test_investigate_reveals_detailed_information(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        target = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=40,
            hp=35,
            max_mp=10,
            mp=4,
            affinities={"lightning": Affinity.WEAK},
            abilities=["火箭拳"],
            spells=["等离子脉冲"],
            traits=["enemy"],
        )
        characters.add(actor)
        characters.add(target)
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.INVESTIGATE,
                parameters={"actor": "瓦莉亚", "target": "帝国机甲"},
            )
        )

        info_text = "\n".join(resolution.payload["information"])
        self.assertIn("HP/MP", info_text)
        self.assertIn("属性骰", info_text)
        self.assertIn("火箭拳", info_text)
        self.assertIn("等离子脉冲", info_text)

    def test_objective_advances_clock(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(actor)
        clocks = ClockManager()
        clocks.add(Clock(name="拆除炸弹", max_segments=6, current=1))
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.OBJECTIVE,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "拆除炸弹",
                    "attributes": ["DEX", "INS"],
                    "clock_name": "拆除炸弹",
                    "clock_direction": 1,
                    "target_number": 10,
                },
            )
        )

        self.assertIn("clock_change", resolution.payload)
        self.assertEqual(clocks.get("拆除炸弹").current, 4)

    def test_bestiary_swift_only_adds_progress_to_escape_or_chase_clocks(self) -> None:
        characters = CharacterManager()
        rat = Character(
            name="硕鼠",
            attributes={"DEX": 12, "MIG": 6, "INS": 8, "WLP": 6},
            max_hp=40,
            hp=40,
            max_mp=35,
            mp=35,
            traits=["enemy", "野兽"],
            npc_ability_profiles=ability_profiles_for_bestiary("硕鼠"),
        )
        characters.add(rat)
        clocks = ClockManager()
        clocks.add(Clock(name="逃跑追击", max_segments=6))
        clocks.add(Clock(name="拆除门锁", max_segments=6))
        engine = RulesEngine()
        engine._rng = FakeRandom([5, 5, 5, 5])
        interceptor = ActionInterceptor(
            engine, characters, clocks, ConflictManager(characters), WorldState()
        )

        chase = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "硕鼠",
                    "target": "逃跑追击",
                    "clock_name": "逃跑追击",
                    "clock_direction": 1,
                    "attributes": ["DEX", "INS"],
                    "target_number": 10,
                },
            )
        )
        ordinary = interceptor.resolve(
            Action(
                ActionType.OBJECTIVE,
                {
                    "actor": "硕鼠",
                    "target": "拆除门锁",
                    "clock_name": "拆除门锁",
                    "clock_direction": 1,
                    "attributes": ["DEX", "INS"],
                    "target_number": 10,
                },
            )
        )

        self.assertEqual(chase.payload["clock_change"].delta, 2)
        self.assertEqual(chase.payload["npc_clock_bonus"]["amount"], 1)
        self.assertEqual(ordinary.payload["clock_change"].delta, 1)
        self.assertNotIn("npc_clock_bonus", ordinary.payload)

    def test_named_objective_clock_is_visible_even_when_roll_fails(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(actor)
        clocks = ClockManager()
        engine = RulesEngine()
        engine._rng = FakeRandom([1, 1])
        interceptor = ActionInterceptor(engine, characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.OBJECTIVE,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "拆除炸弹",
                    "attributes": ["DEX", "INS"],
                    "clock_name": "拆除炸弹",
                    "clock_direction": 1,
                    "target_number": 10,
                    "max_segments": 6,
                },
            )
        )

        self.assertNotIn("clock_change", resolution.payload)
        self.assertEqual(clocks.get("拆除炸弹").current, 0)
        self.assertIn("命刻 [拆除炸弹] 仍为 0/6", resolution.rules_text)

    def test_objective_can_spend_critical_opportunity_on_clock(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=40,
            hp=40,
            max_mp=20,
            mp=20,
            traits=["pc"],
        )
        characters.add(actor)
        clocks = ClockManager()
        clocks.add(Clock(name="拆除炸弹", max_segments=6, current=1))
        engine = RulesEngine()
        engine._rng = FakeRandom([8, 8])
        interceptor = ActionInterceptor(engine, characters, clocks, ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.OBJECTIVE,
                parameters={
                    "actor": "瓦莉亚",
                    "target": "拆除炸弹",
                    "attributes": ["DEX", "INS"],
                    "clock_name": "拆除炸弹",
                    "clock_direction": 1,
                    "target_number": 10,
                    "spend_critical_opportunity_on_clock": True,
                },
            )
        )

        self.assertIn("clock_change", resolution.payload)
        self.assertEqual(clocks.get("拆除炸弹").current, 6)

    def test_request_roll_defaults_missing_target_number(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="白河",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=55,
            hp=55,
            max_mp=45,
            mp=45,
            traits=["pc"],
        )
        characters.add(actor)
        engine = RulesEngine()
        engine._rng = FakeRandom([4, 5])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "白河",
                    "target": "帝国侦察队逼近",
                    "attributes": ["MIG", "WLP"],
                    "non_damage": True,
                },
            )
        )

        self.assertEqual(resolution.payload["roll"].target_number, 10)
        self.assertFalse(resolution.payload["roll"].success)

    def test_request_roll_tolerates_null_target_number_and_modifier(self) -> None:
        characters = CharacterManager()
        actor = Character(
            name="白河",
            attributes={"DEX": 6, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=55,
            hp=55,
            max_mp=45,
            mp=45,
            traits=["pc"],
        )
        characters.add(actor)
        engine = RulesEngine()
        engine._rng = FakeRandom([6, 4])
        interceptor = ActionInterceptor(engine, characters, ClockManager(), ConflictManager(characters), WorldState())

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.REQUEST_ROLL,
                parameters={
                    "actor": "白河",
                    "target": "失控镜面机关",
                    "attributes": ["MIG", "WLP"],
                    "target_number": None,
                    "modifier": None,
                    "non_damage": True,
                },
            )
        )

        self.assertEqual(resolution.payload["roll"].target_number, 10)
        self.assertEqual(resolution.payload["roll"].modifier, 0)
        self.assertTrue(resolution.payload["roll"].success)


if __name__ == "__main__":
    unittest.main()
