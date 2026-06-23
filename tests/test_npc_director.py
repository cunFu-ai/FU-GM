import json
import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.config import LLMConfig
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.main import build_demo_app
from fu_gm.models import Action, ActionType, Character, Clock, EnemyRank, GamePanel, StatusEffect
from fu_gm.npc_director import HeuristicNPCDirector, LLMNPCDirector


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        content = self.responses.pop(0)
        return {"choices": [{"message": {"content": content}}]}


class NPCDirectorTests(unittest.TestCase):
    def test_world_state_stores_and_renders_npc_persona_prompt(self) -> None:
        world_state = WorldState()
        world_state.ensure_npc_persona(
            "帝国机甲",
            public_identity="帝国第七魔导镇压机甲",
            role_in_story="重要反派兵器",
            core_drive="碾碎反抗",
            combat_style="高压进攻",
            goals=["击败英雄"],
            taboos=["绝不示弱"],
        )
        world_state.remember_npc_event("帝国机甲", "在断桥上压制了瓦莉亚。")
        world_state.remember_subject_fact("帝国机甲", "对雷系存在弱点。")

        prompt = world_state.render_npc_prompt("帝国机甲")

        self.assertIn("帝国第七魔导镇压机甲", prompt)
        self.assertIn("击败英雄", prompt)
        self.assertIn("在断桥上压制了瓦莉亚", prompt)
        self.assertIn("对雷系存在弱点", prompt)

    def test_heuristic_npc_director_creates_persona_on_first_use(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=10,
            mp=10,
            weapon_damage=8,
            weapon_type="physical",
            traits=["enemy", "villain"],
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=12,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 12/45"],
                enemy_status=["帝国机甲: HP 60/60"],
                recent_chat="轮到帝国机甲行动。",
                current_actor="帝国机甲",
            ),
            "帝国机甲",
        )

        self.assertIn("帝国机甲", world_state.npc_personas)
        self.assertEqual(action.action_type, ActionType.NPCACT)
        self.assertEqual(action.parameters["npc_action_type"], "Attack")

    def test_heuristic_npc_director_prefers_ultima_recover_when_afflicted(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=50,
            max_mp=40,
            mp=5,
            traits=["enemy", "villain"],
            statuses=[StatusEffect.DAZED],
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=20,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)
        conflict.register_enemy("黑日将军", rank=EnemyRank.VILLAIN, ultima_points=2)

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 20/45"],
                enemy_status=["黑日将军: HP 50/80"],
                recent_chat="轮到黑日将军行动。",
                current_actor="黑日将军",
            ),
            "黑日将军",
        )

        self.assertEqual(action.parameters["npc_action_type"], "UltimaRecover")

    def test_heuristic_npc_director_pushes_objective_when_clock_is_close(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 10, "WLP": 10},
            max_hp=80,
            hp=80,
            max_mp=20,
            mp=20,
            weapon_damage=8,
            traits=["enemy", "villain"],
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=30,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)
        conflict.register_enemy("黑日将军", rank=EnemyRank.VILLAIN, ultima_points=1)

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=["[天启仪式] 7/8"],
                pc_status=["瓦莉亚: HP 30/45"],
                enemy_status=["黑日将军: HP 80/80"],
                recent_chat="轮到黑日将军行动。",
                current_actor="黑日将军",
            ),
            "黑日将军",
        )

        self.assertEqual(action.parameters["npc_action_type"], "Objective")
        self.assertEqual(action.parameters["clock_name"], "天启仪式")

    def test_heuristic_npc_director_guards_when_cornered(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="黑日将军",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=20,
            max_mp=10,
            mp=0,
            crisis_threshold=40,
            weapon_damage=8,
            traits=["enemy", "villain"],
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=30,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)
        conflict.register_enemy("黑日将军", rank=EnemyRank.VILLAIN, ultima_points=0)

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 30/45"],
                enemy_status=["黑日将军: HP 20/80"],
                recent_chat="轮到黑日将军行动。",
                current_actor="黑日将军",
            ),
            "黑日将军",
        )

        self.assertEqual(action.parameters["npc_action_type"], "Guard")

    def test_heuristic_npc_director_uses_defensive_spell_when_cornered(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=80,
            hp=20,
            max_mp=20,
            mp=12,
            crisis_threshold=40,
            weapon_damage=8,
            traits=["enemy", "villain"],
            spells=["魔导屏障"],
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=30,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)
        conflict.register_enemy("帝国机甲", rank=EnemyRank.VILLAIN, ultima_points=0)

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 30/45"],
                enemy_status=["帝国机甲: HP 20/80"],
                recent_chat="轮到帝国机甲行动。",
                current_actor="帝国机甲",
            ),
            "帝国机甲",
        )

        self.assertEqual(action.parameters["npc_action_type"], "Spell")
        self.assertEqual(action.parameters["spell_name"], "魔导屏障")

    def test_heuristic_npc_director_prefers_spell_when_target_is_guarded(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 8, "WLP": 8},
            max_hp=60,
            hp=60,
            max_mp=20,
            mp=10,
            weapon_damage=8,
            traits=["enemy", "villain"],
            spells=["雷暴放射"],
        )
        target = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=30,
            max_mp=30,
            mp=20,
            traits=["pc"],
        )
        guardian = Character(
            name="莱因",
            attributes={"DEX": 6, "MIG": 12, "INS": 6, "WLP": 8},
            max_hp=55,
            hp=50,
            max_mp=10,
            mp=10,
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(target)
        characters.add(guardian)
        characters.set_guarding("莱因", True, guarded_target="瓦莉亚")

        director = HeuristicNPCDirector(characters, conflict, world_state)
        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 30/45", "莱因: HP 50/55"],
                enemy_status=["帝国机甲: HP 60/60"],
                recent_chat="轮到帝国机甲行动。",
                current_actor="帝国机甲",
            ),
            "帝国机甲",
        )

        self.assertEqual(action.parameters["npc_action_type"], "Spell")
        self.assertEqual(action.parameters["damage_type"], "lightning")

    def test_llm_npc_director_uses_persona_prompt(self) -> None:
        characters = CharacterManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
            max_hp=60,
            hp=60,
            max_mp=10,
            mp=10,
            traits=["enemy", "villain"],
        )
        characters.add(enemy)
        world_state.ensure_npc_persona(
            "帝国机甲",
            public_identity="帝国第七魔导镇压机甲",
            role_in_story="重要反派兵器",
            core_drive="碾碎反抗",
        )
        transport = FakeTransport(
            [
                json.dumps(
                    {
                        "action_type": "NPCAct",
                        "parameters": {
                            "actor": "帝国机甲",
                            "npc_action_type": "Attack",
                            "target": "瓦莉亚",
                            "attributes": ["DEX", "MIG"],
                            "damage_type": "physical",
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://api.apiyi.com",
                api_key="test-key",
                action_model="gpt-5.4-nano",
                expressor_model="gpt-5.4-nano",
            ),
            transport=transport,
        )
        director = LLMNPCDirector(
            client=client,
            model="gpt-5.4-nano",
            character_manager=characters,
            conflict_manager=conflict,
            world_state=world_state,
        )

        action = director.decide(
            GamePanel(
                game_phase="冲突场景",
                active_clocks=[],
                pc_status=["瓦莉亚: HP 20/45"],
                enemy_status=["帝国机甲: HP 60/60"],
                recent_chat="轮到帝国机甲行动。",
                current_actor="帝国机甲",
            ),
            "帝国机甲",
        )

        system_prompt = transport.calls[0]["payload"]["messages"][0]["content"]
        user_prompt = transport.calls[0]["payload"]["messages"][1]["content"]
        self.assertEqual(action.action_type, ActionType.NPCACT)
        self.assertNotIn("帝国第七魔导镇压机甲", system_prompt)
        self.assertIn("帝国第七魔导镇压机甲", user_prompt)
        self.assertIn("碾碎反抗", user_prompt)
        self.assertIn("硬规则战术摘要", user_prompt)

    def test_npc_investigation_writes_back_memory(self) -> None:
        characters = CharacterManager()
        clocks = ClockManager()
        world_state = WorldState()
        conflict = ConflictManager(characters)
        rules = RulesEngine(seed=0)
        interceptor = ActionInterceptor(rules, characters, clocks, conflict, world_state)

        enemy = Character(
            name="帝国机甲",
            attributes={"DEX": 6, "MIG": 8, "INS": 12, "WLP": 12},
            max_hp=60,
            hp=60,
            max_mp=10,
            mp=10,
            traits=["enemy", "villain"],
            identity="帝国第七魔导镇压机甲",
        )
        pc = Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
            max_hp=45,
            hp=30,
            max_mp=30,
            mp=20,
            abilities=["雷斩"],
            spells=["落雷"],
            traits=["pc"],
        )
        characters.add(enemy)
        characters.add(pc)
        world_state.ensure_npc_persona("帝国机甲", public_identity="帝国第七魔导镇压机甲")

        resolution = interceptor.resolve(
            Action(
                action_type=ActionType.NPCACT,
                parameters={
                    "actor": "帝国机甲",
                    "npc_action_type": "Investigate",
                    "target": "瓦莉亚",
                    "attributes": ["INS", "INS"],
                    "in_mind_reply": "扫描目标。",
                },
            )
        )

        self.assertTrue(resolution.payload["information"])
        memories = world_state.npc_personas["帝国机甲"].memories
        self.assertTrue(any("侦知 瓦莉亚" in memory for memory in memories))
        self.assertIn("瓦莉亚", world_state.subject_facts)

    def test_run_npc_turn_executes_npc_action(self) -> None:
        app = build_demo_app(use_llm=False)
        app.conflict_manager.start_scene("断桥上的魔导机甲战", ["帝国机甲", "瓦莉亚"])

        text = app.run_npc_turn("英雄刚刚结束上一回合，轮到帝国机甲反击。")

        self.assertTrue("帝国机甲" in text or "【叙事】" in text or "【战斗结算】" in text)


if __name__ == "__main__":
    unittest.main()
