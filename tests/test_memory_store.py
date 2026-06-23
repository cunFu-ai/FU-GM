import tempfile
import unittest

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.project_manager import ProjectManager
from fu_gm.components.ritual_manager import RitualManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Character,
    Clock,
    MemoryVisibility,
    ProjectState,
    ProjectUse,
    RitualDiscipline,
    RitualPlan,
    RitualPotency,
    RitualScope,
    SceneType,
    SecretLockLevel,
    StatusEffect,
)


class MemoryStoreTests(unittest.TestCase):
    def test_save_and_load_campaign_snapshot_roundtrip(self) -> None:
        characters = CharacterManager()
        characters.add(
            Character(
                name="瓦莉亚",
                attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
                max_hp=45,
                hp=21,
                max_mp=30,
                mp=12,
                traits=["pc"],
                statuses=[StatusEffect.SLOW],
                bound_arcana=["霜"],
            )
        )
        clocks = ClockManager()
        clocks.add(Clock(name="帝国追兵逼近", max_segments=6, current=3))
        conflict = ConflictManager(characters)
        conflict.start_scene("断桥之战", ["瓦莉亚"])
        scene = SceneManager()
        scene.start_scene("断桥之战", SceneType.CONFLICT, location="旧王国边境断桥")
        world = WorldState()
        world.world_profile.villain_seeds.append("银羽骑士试图唤醒旧王国机甲。")
        world.record_memory_event("瓦莉亚在断桥挡住了帝国机甲。", kind="scene_summary", entities=["瓦莉亚", "帝国机甲"])
        world.record_relation("瓦莉亚", "憎恨", "帝国", evidence="故乡被帝国焚毁")
        world.upsert_gm_secret(
            "silver_feather",
            title="银羽骑士真实身份",
            content="银羽骑士是反派卡尔被封印的善性人格。",
            lock_level=SecretLockLevel.DRAFT,
            related_entities=["银羽骑士", "卡尔"],
            public_clues=["银色羽毛"],
        )
        world.ensure_npc_persona("银羽骑士", public_identity="戴银羽面具的流浪骑士", secrets=["与卡尔共享灵魂"])

        rules = RulesEngine(seed=0)
        ritual = RitualManager(rules, characters, clocks)
        plan = RitualPlan(
            name="封住裂隙",
            caster="瓦莉亚",
            discipline=RitualDiscipline.RITUALISM,
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            effect="暂时封住魔界裂隙。",
            mp_cost=20,
            target_number=7,
            attributes=["INS", "WLP"],
            clock_segments=4,
            clock_name="仪式：封住裂隙",
        )
        ritual.active_rituals[plan.clock_name] = plan
        project = ProjectManager(characters)
        project.projects["水晶罗盘"] = ProjectState(
            name="水晶罗盘",
            inventor="瓦莉亚",
            potency=RitualPotency.MINOR,
            scope=RitualScope.INDIVIDUAL,
            use=ProjectUse.CONSUMABLE,
            effect="定位最近的古代遗迹入口。",
            material_cost=100,
            required_progress=1,
            current_progress=1,
            completed=True,
        )
        story_arc = StoryArcManager(world, clocks)
        story_arc.sync_from_world_profile()
        story_arc.advance_villain_pressure(
            story_arc.state.villain_pressure[0].track_id,
            amount=2,
            reason="银羽骑士夺走了机甲钥匙。",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignMemoryStore(tmpdir)
            snapshot_path = store.save_campaign(
                "永雨之下",
                world_state=world,
                character_manager=characters,
                clock_manager=clocks,
                conflict_manager=conflict,
                scene_manager=scene,
                ritual_manager=ritual,
                project_manager=project,
                story_arc_manager=story_arc,
            )

            new_world = WorldState()
            new_characters = CharacterManager()
            new_clocks = ClockManager()
            new_conflict = ConflictManager(new_characters)
            new_scene = SceneManager()
            new_ritual = RitualManager(rules, new_characters, new_clocks)
            new_project = ProjectManager(new_characters)
            new_story_arc = StoryArcManager(new_world, new_clocks)

            store.load_campaign(
                "永雨之下",
                world_state=new_world,
                character_manager=new_characters,
                clock_manager=new_clocks,
                conflict_manager=new_conflict,
                scene_manager=new_scene,
                ritual_manager=new_ritual,
                project_manager=new_project,
                story_arc_manager=new_story_arc,
            )

            self.assertTrue(snapshot_path.exists())
            self.assertTrue((snapshot_path.parent / "events.jsonl").exists())
            self.assertEqual(new_characters.get("瓦莉亚").statuses, [StatusEffect.SLOW])
            self.assertEqual(new_characters.get("瓦莉亚").bound_arcana, ["霜"])
            self.assertEqual(new_clocks.get("帝国追兵逼近").current, 3)
            self.assertTrue(new_conflict.state.active)
            self.assertEqual(new_scene.current_scene.location, "旧王国边境断桥")
            self.assertIn("仪式：封住裂隙", new_ritual.active_rituals)
            self.assertTrue(new_project.projects["水晶罗盘"].completed)
            self.assertEqual(new_world.gm_secrets["silver_feather"].public_clues, ["银色羽毛"])
            self.assertEqual(new_world.npc_personas["银羽骑士"].secrets, ["与卡尔共享灵魂"])
            self.assertEqual(new_story_arc.state.villain_pressure[0].current, 2)
            self.assertIn("机甲钥匙", new_story_arc.state.villain_pressure[0].last_action)

    def test_gm_secret_revisions_are_versioned_and_public_facts_are_locked(self) -> None:
        world = WorldState()
        world.upsert_gm_secret(
            "silver_feather",
            title="银羽骑士真实身份",
            content="银羽骑士是卡尔的兄长。",
            lock_level=SecretLockLevel.SEEDED,
            public_clues=["银色羽毛"],
        )

        revised = world.revise_gm_secret(
            "silver_feather",
            new_content="银羽骑士是卡尔被封印的善性人格。",
            reason="更能映照主角的自我救赎主题。",
            preserve_clues=["银色羽毛"],
        )

        self.assertEqual(revised.content, "银羽骑士是卡尔被封印的善性人格。")
        self.assertEqual(len(revised.revisions), 1)
        self.assertEqual(revised.revisions[0].previous_content, "银羽骑士是卡尔的兄长。")

        world.set_gm_secret_lock("silver_feather", SecretLockLevel.PUBLIC)
        with self.assertRaisesRegex(ValueError, "公开事实"):
            world.revise_gm_secret("silver_feather", new_content="银羽骑士其实不存在。")

    def test_retrieve_relevant_memory_respects_private_visibility(self) -> None:
        world = WorldState()
        world.record_memory_event("卡尔占领了精灵村庄。", kind="scene_summary", entities=["卡尔", "精灵村庄"])
        world.record_memory_event(
            "卡尔真正害怕的是自己的善性人格。",
            kind="gm_secret",
            visibility=MemoryVisibility.PRIVATE,
            entities=["卡尔"],
        )
        world.record_relation("瓦莉亚", "憎恨", "卡尔", evidence="卡尔摧毁了她的故乡")

        public_results = world.retrieve_relevant_memory("卡尔 精灵村庄")
        private_results = world.retrieve_relevant_memory("卡尔 善性人格", include_private=True)

        self.assertTrue(any("精灵村庄" in item for item in public_results))
        self.assertFalse(any("善性人格" in item for item in public_results))
        self.assertTrue(any("善性人格" in item for item in private_results))


if __name__ == "__main__":
    unittest.main()
