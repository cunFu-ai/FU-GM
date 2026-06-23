from __future__ import annotations

import unittest
import tempfile

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionResolution, ActionType, Character, MemoryVisibility, SceneType
from fu_gm.scene_orchestrator import SceneOrchestrator


class FixedActionBrain:
    def decide(self, panel):
        return Action(
            action_type=ActionType.NARRATE,
            parameters={"in_mind_reply": "场景继续推进。"},
        )


class RichNarrateBrain:
    def decide(self, panel):
        return Action(
            action_type=ActionType.NARRATE,
            parameters={
                "summary": "镜头越过雨幕，落在被封锁的精灵村庄。",
                "public_facts": ["精灵村庄的入口被帝国封锁，但村中央古树仍然完好。"],
                "gm_private_notes": ["卡尔故意保留古树，因为树下封着更古老的灾厄。"],
                "subject_facts": [{"subject": "精灵村庄", "note": "村中央古树仍然完好。"}],
                "npc_updates": [
                    {
                        "name": "卡尔",
                        "public_identity": "黑日将军",
                        "core_drive": "以残酷手段阻止古老灾厄醒来",
                        "note": "卡尔没有摧毁村中央古树。",
                    }
                ],
                "relations": [{"source": "卡尔", "relation": "占领", "target": "精灵村庄"}],
            },
        )


class CapturingExpressor:
    def __init__(self) -> None:
        self.last_resolution: ActionResolution | None = None

    def render(self, resolution: ActionResolution) -> str:
        self.last_resolution = resolution
        return "ok"


def build_app(world_state: WorldState | None = None, expressor=None) -> SceneOrchestrator:
    characters = CharacterManager()
    characters.add(
        Character(
            name="瓦莉亚",
            attributes={"DEX": 8, "MIG": 10, "INS": 8, "WLP": 8},
            max_hp=45,
            hp=45,
            max_mp=35,
            mp=35,
            traits=["pc"],
        )
    )
    characters.add(
        Character(
            name="卡尔",
            attributes={"DEX": 8, "MIG": 8, "INS": 10, "WLP": 10},
            max_hp=70,
            hp=70,
            max_mp=50,
            mp=50,
            traits=["enemy", "villain"],
            identity="黑日将军",
        )
    )
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = world_state or WorldState()
    rules = RulesEngine(seed=0)
    return SceneOrchestrator(
        action_brain=FixedActionBrain(),
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world,
        interceptor=ActionInterceptor(rules, characters, clocks, conflict, world),
        expressor=expressor or CapturingExpressor(),
        scene_manager=SceneManager(),
    )


class MemoryInjectionTests(unittest.TestCase):
    def test_game_panel_receives_public_and_private_memory_separately(self) -> None:
        world = WorldState()
        world.record_memory_event(
            "卡尔占领了精灵村庄。",
            kind="scene_summary",
            visibility=MemoryVisibility.PUBLIC,
            entities=["卡尔", "精灵村庄"],
        )
        world.record_memory_event(
            "卡尔秘密保护精灵王血脉。",
            kind="gm_secret",
            visibility=MemoryVisibility.PRIVATE,
            entities=["卡尔", "精灵村庄"],
        )
        world.upsert_gm_secret(
            "karl_mercy",
            title="卡尔的隐藏动机",
            content="卡尔表面侵略精灵村庄，其实在阻止更古老的灾厄醒来。",
            related_entities=["卡尔", "精灵村庄"],
            public_clues=["他没有毁掉村中央的古树"],
        )
        app = build_app(world)
        app.scene_manager.start_scene("归乡", SceneType.STANDARD, location="精灵村庄")

        panel = app.build_panel("我要去精灵村庄质问卡尔。")

        self.assertTrue(any("占领了精灵村庄" in memory for memory in panel.retrieved_public_memory))
        self.assertFalse(any("秘密保护" in memory for memory in panel.retrieved_public_memory))
        self.assertTrue(any("秘密保护" in memory for memory in panel.gm_private_memory))
        self.assertTrue(any("隐藏动机" in memory for memory in panel.gm_private_memory))
        self.assertIn("不得", panel.memory_guidance)

    def test_game_panel_includes_backstage_gm_creative_guidance(self) -> None:
        world = WorldState()
        world.world_profile.magic_tech_role = "企业垄断灵魂能源，上层城市享受阳光，下层街区承受污染。"
        world.world_profile.major_locations["永雨工业城"] = "公司和魔导工厂共同统治的双层城市。"
        app = build_app(world)

        panel = app.build_panel("我想调查下层街区为什么停电。")

        self.assertIn("GM创作指导", panel.memory_guidance)
        self.assertIn("techno_pressure", panel.memory_guidance)
        self.assertIn("预备地点不是公开事实", panel.memory_guidance)
        self.assertIn("世界创建仍有缺项", panel.memory_guidance)
        self.assertIn("world_profile_updates", panel.memory_guidance)

    def test_expressor_payload_gets_public_memory_only(self) -> None:
        world = WorldState()
        world.record_memory_event("卡尔占领了精灵村庄。", entities=["卡尔", "精灵村庄"])
        world.record_memory_event(
            "卡尔秘密保护精灵王血脉。",
            visibility=MemoryVisibility.PRIVATE,
            entities=["卡尔", "精灵村庄"],
        )
        expressor = CapturingExpressor()
        app = build_app(world, expressor=expressor)
        app.scene_manager.start_scene("归乡", SceneType.STANDARD, location="精灵村庄")

        result = app.run_turn("我要去精灵村庄质问卡尔。")

        self.assertEqual(result, "ok")
        self.assertIsNotNone(expressor.last_resolution)
        payload = expressor.last_resolution.payload
        self.assertIn("retrieved_public_memory", payload)
        self.assertNotIn("gm_private_memory", payload)
        self.assertTrue(any("占领了精灵村庄" in memory for memory in payload["retrieved_public_memory"]))
        self.assertFalse(any("秘密保护" in memory for memory in payload["retrieved_public_memory"]))

    def test_narrate_action_can_persist_llm_creative_context_without_hard_rules(self) -> None:
        world = WorldState()
        app = build_app(world)

        resolution = app.interceptor.resolve(
            Action(
                action_type=ActionType.NARRATE,
                parameters={
                    "summary": "镜头越过雨幕，落在被封锁的精灵村庄。",
                    "public_facts": ["精灵村庄的入口被帝国封锁，但村中央古树仍然完好。"],
                    "gm_private_notes": ["卡尔故意保留古树，因为树下封着更古老的灾厄。"],
                    "subject_facts": [
                        {"subject": "精灵村庄", "note": "村中央古树仍然完好。"},
                    ],
                    "npc_updates": [
                        {
                            "name": "卡尔",
                            "core_drive": "以残酷手段阻止古老灾厄醒来",
                            "note": "卡尔没有摧毁村中央古树。",
                        }
                    ],
                    "relations": [
                        {"source": "卡尔", "relation": "占领", "target": "精灵村庄"},
                    ],
                    "world_profile_updates": {
                        "major_locations": {"精灵村庄": "被帝国封锁但古树仍完好的森林聚落。"},
                        "map_locations": [
                            {
                                "name": "精灵村庄",
                                "description": "被帝国封锁但古树仍完好的森林聚落。",
                                "feature_type": "settlement",
                                "position_hint": "west",
                                "faction": "黑日军团",
                                "draw_icon": True,
                            }
                        ],
                        "factions": {"黑日军团": "卡尔率领的占领部队。"},
                        "mysteries": ["古树下方封印着什么？"],
                        "world_threats": ["古树下的灾厄正在苏醒。"],
                    },
                    "persistent_changes": [
                        {
                            "type": "facility",
                            "name": "帝国封锁线",
                            "description": "帝国士兵和魔导路障封住了村庄入口。",
                            "location": "精灵村庄",
                        }
                    ],
                },
            )
        )

        self.assertTrue(resolution.payload["narrative_authority"])
        self.assertIn("未执行任何硬数值结算", resolution.rules_text)
        self.assertTrue(any("精灵村庄的入口" in memory for memory in world.memories))
        self.assertFalse(any("古老的灾厄" in memory for memory in world.memories))
        self.assertTrue(
            any(event.visibility == MemoryVisibility.PRIVATE and "古老的灾厄" in event.summary for event in world.memory_events)
        )
        self.assertIn("村中央古树仍然完好。", world.subject_facts["精灵村庄"])
        self.assertTrue(any("卡尔没有摧毁村中央古树" in note for note in world.npc_personas["卡尔"].memories))
        self.assertTrue(any(change.name == "帝国封锁线" for change in world.persistent_changes))
        self.assertIn("精灵村庄", world.world_profile.major_locations)
        self.assertEqual(world.map_locations["精灵村庄"].feature_type, "settlement")
        self.assertEqual(world.map_locations["精灵村庄"].position_hint, "west")
        self.assertTrue(world.map_locations["精灵村庄"].draw_icon)
        self.assertIn("黑日军团", world.world_profile.factions)
        self.assertIn("古树下方封印着什么？", world.world_profile.mysteries)
        self.assertTrue(any(event.kind == "world_profile_update" for event in world.memory_events))

    def test_world_profile_updates_clean_table_talk_and_audit_rejections(self) -> None:
        world = WorldState()

        changes = world.apply_world_profile_updates(
            {
                "kingdoms": {
                    "的大钟能安抚灵魂": "错误碎片",
                    "奥涅里亚": "边境起始王国，守护白花碑驿站。",
                },
                "mysteries": [
                    "每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写；我投这个第一幕。额外补一个反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民。"
                ],
                "factions": {
                    "我的角色洛岚是钟鸣": "角色创建噪声",
                },
            },
            source="test",
        )

        self.assertIn("kingdoms.奥涅里亚: 边境起始王国，守护白花碑驿站", changes)
        self.assertIn("奥涅里亚", world.world_profile.kingdoms)
        self.assertNotIn("的大钟能安抚灵魂", world.world_profile.kingdoms)
        self.assertNotIn("我的角色洛岚是钟鸣", world.world_profile.factions)
        self.assertEqual(
            world.world_profile.mysteries,
            ["每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写"],
        )
        audit = world.world_profile_update_audit(limit=1)[0]
        self.assertEqual(audit.kind, "world_profile_update_audit")
        self.assertTrue(audit.payload["rejected"])

    def test_narrate_soft_writeback_persists_to_topic_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            world = WorldState()
            app = build_app(world)
            app.action_brain = RichNarrateBrain()
            app.topic_memory_store = TopicMemoryStore(tmpdir)
            app.set_campaign_id("星匣迷宫")
            app.scene_manager.start_scene("归乡", SceneType.STANDARD, location="精灵村庄")

            result = app.run_turn("我要去精灵村庄质问卡尔。")

            self.assertEqual(result, "ok")
            public_records = app.topic_memory_store.recall("星匣迷宫", "精灵村庄 卡尔", include_private=False)
            self.assertTrue(any("村中央古树仍然完好" in record.format_for_prompt() for record in public_records))
            self.assertFalse(any("古老的灾厄" in record.format_for_prompt() for record in public_records))

            private_records = app.topic_memory_store.recall("星匣迷宫", "古老灾厄 卡尔", include_private=True)
            self.assertTrue(any(record.visibility == MemoryVisibility.PRIVATE for record in private_records))
            self.assertTrue(any("古老的灾厄" in record.format_for_prompt() for record in private_records))


if __name__ == "__main__":
    unittest.main()
