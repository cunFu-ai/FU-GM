import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Character, Clock, StatusEffect
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
        self.assertIn("雷系:weak", information)
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

    def test_guard_sets_cover_and_redirects_melee_attack(self) -> None:
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
        attack_resolution = interceptor.resolve(
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

        self.assertIn("cover_text", attack_resolution.payload)
        self.assertLess(characters.get("钢盾骑士").hp, 40)
        self.assertEqual(characters.get("露琪亚").hp, 30)

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

    def test_acceleration_spell_records_narrative_followup(self) -> None:
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
        self.assertEqual(resolution.payload["spell_effect"].effect_type, "extra_action")
        self.assertEqual(resolution.payload["spell_name"], "加速术")
        self.assertEqual(conflict.next_turn(), "米菈")
        self.assertIn("奖励回合", conflict.format_phase())
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
                    "target_number": 10,
                },
            )
        )

        self.assertIn("clock_change", resolution.payload)
        self.assertEqual(clocks.get("拆除炸弹").current, 4)

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
