import unittest

from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import HeroDraft, StoryArcPhase, StorySessionSummary


class StoryArcManagerTests(unittest.TestCase):
    def test_world_threat_pressure_uses_named_faction_when_available(self) -> None:
        world_state = WorldState()
        world_state.world_profile.world_threats.extend(
            [
                "辉钢财团正在把灰晶病患者的记忆作为可买卖燃料",
                "一颗流星即将撞击这个星球",
            ]
        )

        state = StoryArcManager(world_state).sync_from_world_profile()

        by_goal = {item.goal: item.villain for item in state.villain_pressure}
        self.assertEqual(
            by_goal["辉钢财团正在把灰晶病患者的记忆作为可买卖燃料"],
            "辉钢财团",
        )
        self.assertEqual(by_goal["一颗流星即将撞击这个星球"], "世界威胁")

    def test_sync_from_world_profile_builds_backstage_arc_state(self) -> None:
        world_state = WorldState()
        world = world_state.world_profile
        world.campaign_title = "星尘炉心"
        world.starting_region = "永雨工业城"
        world.major_locations["白塔港"] = "悬挂风帆和水晶灯的空港。"
        world.villain_seeds.append("辉钢财团想用灵魂炉接管下层城市。")
        world.world_threats.append("污染云层正在遮蔽太阳。")
        world.mysteries.append("沉睡水晶为何会在夜里唱歌？")
        world.gm_secret_notes.append("沉睡水晶其实保存着旧王国最后一位公主的记忆。")
        world.hero_drafts["阿凛"] = HeroDraft(
            player_name="阿凛",
            hero_name="琳",
            identity="宝箱猎人",
            theme="好奇心",
            origin="白塔港",
            notes=["想证明自己不是麻烦制造者。"],
        )

        manager = StoryArcManager(world_state)
        state = manager.sync_from_world_profile()

        self.assertEqual(state.phase, StoryArcPhase.OPENING)
        self.assertTrue(any(thread.thread_type == "villain_seed" for thread in state.threads))
        self.assertTrue(any(thread.thread_type == "world_threat" for thread in state.threads))
        self.assertTrue(any(thread.thread_type == "hero_theme" for thread in state.threads))
        self.assertTrue(any(track.villain.startswith("辉钢财团") for track in state.villain_pressure))
        self.assertTrue(any(reveal.title.startswith("沉睡水晶") for reveal in state.reveals))
        self.assertTrue(any(location.location == "永雨工业城" for location in state.locations))

        prompt_summary = manager.prompt_summary()
        self.assertIn("agenda", prompt_summary)
        self.assertTrue(prompt_summary["agenda"]["scene_closure"])
        self.assertTrue(prompt_summary["agenda"]["campaign_pacing"])
        self.assertTrue(prompt_summary["agenda"]["director_moves"])
        self.assertNotIn("secret", prompt_summary["reveal_candidates"][0])

        public_payload = manager.audit_payload(include_private=False)
        self.assertNotIn("secret", public_payload["reveals"][0])
        private_payload = manager.audit_payload(include_private=True)
        self.assertIn("secret", private_payload["reveals"][0])

    def test_session_summary_advances_phase_pressure_and_reveals(self) -> None:
        world_state = WorldState()
        world = world_state.world_profile
        world.villain_seeds.append("卡尔要夺走沉睡水晶。")
        world.mysteries.append("沉睡水晶为何会在夜里唱歌？")
        world.major_locations["白塔港"] = "起始空港。"
        manager = StoryArcManager(world_state)
        manager.sync_from_world_profile()
        manager.state.pacing_profile.target_sessions = 20
        manager.state.session_count = 8
        reveal = next(item for item in manager.state.reveals if item.title.startswith("沉睡水晶"))
        manager.mark_reveal(reveal.reveal_id, clue="水晶在王室纹章旁发光。")

        summary = StorySessionSummary(
            campaign_id="星尘炉心",
            session_id="s9",
            title="水晶的歌声",
            created_at="2026-06-17T00:00:00+00:00",
            public_summary="英雄们在白塔港发现沉睡水晶会回应卡尔的名字。",
            short_memory="沉睡水晶回应了卡尔，白塔港开始戒严。",
            timeline=["白塔港的守卫封锁仓库。"],
            important_npcs=["卡尔"],
            locations=["白塔港"],
            unresolved_threads=["卡尔要夺走沉睡水晶。"],
            private_notes=["水晶歌声来自旧王国公主的记忆。"],
            entities=["卡尔", "沉睡水晶", "白塔港"],
        )

        state = manager.update_from_session_summary(summary)

        self.assertEqual(state.phase, StoryArcPhase.MIDPOINT)
        self.assertEqual(state.session_count, 9)
        self.assertIn("s9", state.processed_session_ids)
        self.assertTrue(any(track.current == 1 for track in state.villain_pressure))
        reveal = next(item for item in state.reveals if item.title.startswith("沉睡水晶"))
        self.assertEqual(reveal.status, "ready")
        self.assertTrue(any(location.last_seen == "s9" for location in state.locations))

        manager.update_from_session_summary(summary)
        self.assertEqual(state.session_count, 9)

    def test_phase_progression_scales_with_campaign_length(self) -> None:
        manager = StoryArcManager(WorldState())
        manager.sync_from_world_profile()

        manager.state.pacing_profile.target_sessions = 20
        manager.state.session_count = 19
        manager._refresh_phase()
        self.assertEqual(manager.state.phase, StoryArcPhase.FINALE)

        manager.state.pacing_profile.target_sessions = 35
        manager.state.session_count = 18
        manager._refresh_phase()
        self.assertEqual(manager.state.phase, StoryArcPhase.MIDPOINT)

        manager.state.pacing_profile.target_sessions = 50
        manager.state.session_count = 20
        manager._refresh_phase()
        self.assertEqual(manager.state.phase, StoryArcPhase.RISING)

        manager.state.session_count = 44
        manager._refresh_phase()
        self.assertEqual(manager.state.phase, StoryArcPhase.FINALE)

    def test_json_villain_seeds_are_normalized_and_deduped(self) -> None:
        world_state = WorldState()
        world = world_state.world_profile
        world.villain_seeds.extend(
            [
                '{"name":"辉钢财团","description":"辉钢财团想用灵魂炉接管下层城市。"}',
                '{ "description" : "辉钢财团想用灵魂炉接管下层城市。", "name" : "辉钢财团" }',
            ]
        )

        manager = StoryArcManager(world_state)
        state = manager.sync_from_world_profile()
        state = manager.sync_from_world_profile()

        villain_threads = [thread for thread in state.threads if thread.thread_type == "villain_seed"]
        pressures = [track for track in state.villain_pressure if track.villain == "辉钢财团"]
        self.assertEqual(len(villain_threads), 1)
        self.assertEqual(villain_threads[0].title, "辉钢财团")
        self.assertFalse(villain_threads[0].title.startswith("{"))
        self.assertEqual(len(pressures), 1)


if __name__ == "__main__":
    unittest.main()
