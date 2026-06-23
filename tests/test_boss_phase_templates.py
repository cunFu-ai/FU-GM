import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.expressor import Expressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, Affinity, Character, EnemyRank, GamePanel
from fu_gm.npc_director import HeuristicNPCDirector


class BossPhaseTemplateTests(unittest.TestCase):
    def test_boss_phase_template_applies_phase_changes_and_guides_npcact(self) -> None:
        characters = CharacterManager()
        boss = Character(
            name="宝箱王",
            attributes={"DEX": 8, "MIG": 10, "INS": 10, "WLP": 10},
            max_hp=80,
            hp=0,
            max_mp=50,
            mp=30,
            traits=["enemy", "villain"],
            weapon_damage=8,
            spells=["半影"],
        )
        hero = Character(
            name="阿凛",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=35,
            mp=35,
            traits=["pc"],
        )
        characters.add(boss)
        characters.add(hero)
        conflict = ConflictManager(characters)
        encounter = EncounterManager(characters, conflict)
        stages = encounter.boss_stage_templates("宝箱王", theme="相性", champion_value=3)
        conflict.register_enemy("宝箱王", EnemyRank.VILLAIN, ultima_points=0, escalation_stages=stages)

        event = conflict.resolve_zero_hp("宝箱王")

        self.assertEqual(event.event_type, "escalation")
        self.assertEqual(conflict.state.enemy_action_counts["宝箱王"], 3)
        self.assertEqual(characters.get("宝箱王").temporary_affinities["fire"], Affinity.RESIST)
        self.assertEqual(characters.get("宝箱王").temporary_affinities["ice"], Affinity.WEAK)
        self.assertTrue(any("相性反转" in line for line in conflict.format_combat_log()))

        director = HeuristicNPCDirector(characters, conflict, WorldState())
        panel = GamePanel(
            game_phase=conflict.format_phase(),
            active_clocks=[],
            pc_status=[characters.format_status(hero)],
            enemy_status=[characters.format_status(characters.get("宝箱王"))],
            recent_chat="轮到宝箱王行动。",
            current_actor="宝箱王",
        )
        snapshot = director.build_tactical_snapshot(panel, "宝箱王")
        self.assertIn("Spell", snapshot["stage_preferred_actions"])
        self.assertIn("ice", snapshot["stage_affinity_changes"])

        action = director.decide(panel, "宝箱王")

        self.assertEqual(action.action_type, ActionType.NPCACT)
        self.assertEqual(action.parameters["npc_action_type"], "Spell")

    def test_boss_design_offers_single_body_before_multipart_option(self) -> None:
        characters = CharacterManager()
        conflict = ConflictManager(characters)
        encounter = EncounterManager(characters, conflict)
        for name in ("阿凛", "白河", "晴"):
            characters.add(
                Character(
                    name=name,
                    attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
                    max_hp=45,
                    hp=45,
                    max_mp=35,
                    mp=35,
                    traits=["pc"],
                    level=5,
                )
            )

        design = encounter.design_encounter(["阿凛", "白河", "晴"], boss=True)

        self.assertTrue(any("单体 Boss" in option for option in design.enemy_mix))
        self.assertTrue(any("不要默认使用多部件" in note for note in design.special_mechanics))

    def test_next_turn_resolution_exposes_turn_board_and_recent_combat_log(self) -> None:
        characters = CharacterManager()
        hero = Character(
            name="阿凛",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=35,
            mp=35,
            traits=["pc"],
            weapon_damage=5,
        )
        boss = Character(
            name="宝箱王",
            attributes={"DEX": 8, "MIG": 10, "INS": 10, "WLP": 10},
            max_hp=60,
            hp=60,
            max_mp=40,
            mp=20,
            traits=["enemy", "villain"],
        )
        support = Character(
            name="白河",
            attributes={"DEX": 8, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=35,
            mp=35,
            traits=["pc"],
        )
        for character in (hero, boss, support):
            characters.add(character)
        conflict = ConflictManager(characters)
        conflict.start_scene("星尘宝箱深处", ["阿凛", "宝箱王", "白河"])
        interceptor = ActionInterceptor(
            RulesEngine(seed=2),
            characters,
            ClockManager(),
            conflict,
            WorldState(),
        )
        interceptor.resolve(
            Action(
                action_type=ActionType.ATTACK,
                parameters={"actor": "阿凛", "target": "宝箱王"},
            )
        )

        resolution = interceptor.resolve(Action(action_type=ActionType.NEXT_TURN, parameters={}))
        rendered = Expressor().render(resolution)

        self.assertEqual(resolution.payload["turn_board"]["current_actor"], "宝箱王")
        self.assertIn("recent_log", resolution.payload["turn_board"])
        self.assertTrue(resolution.payload["combat_log"])
        self.assertIn("待行动", rendered)
        self.assertIn("最近战斗日志", rendered)


if __name__ == "__main__":
    unittest.main()
