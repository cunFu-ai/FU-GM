import unittest

from fu_gm.components.campaign_pacing_manager import CampaignPacingManager
from fu_gm.components.campaign_feedback_controller import CampaignFeedbackControl
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.session_contract_planner import SessionContractPlanner
from fu_gm.components.story_arc_manager import StoryArcManager
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    CampaignLength,
    Character,
    ChapterPackage,
    ChapterPackageScene,
    Clock,
    LocationReturnState,
    RevealCandidate,
    SessionFeedbackSignals,
    SessionDramaticContract,
    SessionNPCRole,
    SessionEpisodeProgress,
    SessionPacingPlan,
    StoryArcPhase,
    StoryThread,
    VillainPressureTrack,
)


class CampaignPacingManagerTests(unittest.TestCase):
    def test_session_contract_never_persists_a_player_character_as_npc(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        story = StoryArcManager(world, clocks)
        characters = CharacterManager()
        characters.add(
            Character(
                name="艾薇娅",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                traits=["pc"],
            )
        )
        planner = SessionContractPlanner(
            story,
            world,
            character_manager=characters,
        )
        contract = SessionDramaticContract(
            important_npcs=[
                SessionNPCRole(name="艾薇娅", public_role="队伍成员"),
                SessionNPCRole(name="守望会会长", public_role="旧路守门人"),
            ]
        )

        planner._register_session_npcs(contract)

        self.assertNotIn("艾薇娅", world.npc_personas)
        self.assertIn("守望会会长", world.npc_personas)

    def test_story_proposition_is_not_treated_as_a_persistent_npc_identity(self) -> None:
        self.assertTrue(SessionContractPlanner._looks_like_person("监察官艾蕾娜"))
        self.assertEqual(
            SessionContractPlanner._named_actor_from_goal(
                "监察官艾蕾娜曾是赤羽遗民；她认为记忆必须被集中保管"
            ),
            "监察官艾蕾娜",
        )
        self.assertFalse(
            SessionContractPlanner._looks_like_person(
                "监察官艾蕾娜曾是赤羽遗民；她认为记忆必须被集中保管"
            )
        )

    def _manager_with_clocks(self) -> CampaignPacingManager:
        clocks = ClockManager()
        for name in ("财团巡逻队逼近", "潮水没顶", "艾蕾娜启动记忆集中协议"):
            clocks.add(
                Clock(
                    name=name,
                    max_segments=6,
                    current=1,
                    clock_type="threat",
                    auto_advance="每个行动轮结束时推进1格",
                )
            )
        world = WorldState()
        story = StoryArcManager(world, clocks)
        return CampaignPacingManager(story, clocks, world)

    def test_opening_budget_advances_only_one_threat_clock(self) -> None:
        manager = self._manager_with_clocks()

        changes = manager.auto_advance_after_turn(skip_names=set())

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].clock_name, "财团巡逻队逼近")
        public = manager.formatted_public_clocks()
        self.assertEqual(len(public), 1)
        self.assertIn("财团巡逻队逼近", public[0])

    def test_boss_budget_allows_multiple_auto_threats(self) -> None:
        manager = self._manager_with_clocks()

        changes = manager.auto_advance_after_turn(skip_names=set(), boss_scene=True)

        self.assertEqual(len(changes), 3)
        public = manager.formatted_public_clocks(boss_scene=True)
        self.assertGreaterEqual(len(public), 3)

    def test_opening_budget_does_not_auto_advance_second_threat_after_pressure_changed(self) -> None:
        manager = self._manager_with_clocks()

        changes = manager.auto_advance_after_turn(skip_names={"财团巡逻队逼近"})

        self.assertEqual(changes, [])

    def test_round_end_clock_only_ticks_when_round_really_ends(self) -> None:
        clocks = ClockManager()
        clocks.add(
            Clock(
                name="首领蓄力",
                max_segments=6,
                clock_type="boss",
                auto_advance="每轮结束推进1格",
                auto_advance_timing="round_end",
            )
        )
        world = WorldState()
        manager = CampaignPacingManager(StoryArcManager(world, clocks), clocks, world)

        self.assertEqual(manager.auto_advance_after_turn(event_timing="after_action"), [])
        self.assertEqual(clocks.get("首领蓄力").current, 0)
        changes = manager.auto_advance_after_turn(event_timing="action_round_end")

        self.assertEqual(len(changes), 1)
        self.assertEqual(clocks.get("首领蓄力").current, 1)

    def test_slow_countdown_ticks_after_two_complete_action_rounds(self) -> None:
        clocks = ClockManager()
        clocks.add(
            Clock(
                name="巡逻队逼近",
                max_segments=8,
                clock_type="threat",
                auto_advance="每2个行动轮结束后推进1格",
                auto_advance_every=2,
            )
        )
        world = WorldState()
        manager = CampaignPacingManager(StoryArcManager(world, clocks), clocks, world)

        self.assertEqual(manager.auto_advance_after_turn(event_timing="action_round_end"), [])
        self.assertEqual(clocks.get("巡逻队逼近").current, 0)
        changes = manager.auto_advance_after_turn(event_timing="action_round_end")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].after, 1)

    def test_single_action_event_never_advances_action_round_clock(self) -> None:
        manager = self._manager_with_clocks()

        changes = manager.auto_advance_after_turn(event_timing="after_action")

        self.assertEqual(changes, [])
        self.assertEqual(manager.clock_manager.get("财团巡逻队逼近").current, 1)

    def test_audit_payload_reads_committed_plan_without_refreshing(self) -> None:
        manager = self._manager_with_clocks()
        manager.story_arc_manager.state.current_pacing_plan.session_number = 7

        def fail_refresh(**_kwargs):
            raise AssertionError("审计读取不应刷新计划或调用模型")

        manager.refresh_plan = fail_refresh
        payload = manager.audit_payload()

        self.assertEqual(payload["current_plan"]["session_number"], 7)
        self.assertIn("foreground_clock_names", payload)

    def test_public_clock_hint_only_marks_changed_goal_clocks_during_turn(self) -> None:
        manager = self._manager_with_clocks()
        manager.clock_manager.add(
            Clock(name="仪式：风铃回声", max_segments=4, current=3, clock_type="ritual")
        )

        quiet = manager.formatted_public_clocks(highlight_names=set())
        highlighted = manager.formatted_public_clocks(highlight_names={"仪式：风铃回声"})

        assert any("【仪式：风铃回声】3/4" == line for line in quiet)
        assert any("只差最后一点" in line for line in highlighted)

        changed_only = manager.formatted_public_clocks(
            highlight_names={"仪式：风铃回声"},
            only_highlighted=True,
        )
        no_changes = manager.formatted_public_clocks(
            highlight_names=set(),
            only_highlighted=True,
        )
        assert all("仪式：风铃回声" in line for line in changed_only)
        assert no_changes == []

    def test_profile_maps_standard_campaign_to_five_arcs(self) -> None:
        manager = self._manager_with_clocks()

        profile = manager.configure(length=CampaignLength.STANDARD)
        plan = manager.refresh_plan(force_session_number=18)

        self.assertEqual(profile.target_sessions, 35)
        self.assertEqual(profile.target_arcs, 5)
        self.assertEqual(plan.phase, StoryArcPhase.MIDPOINT)
        guidance = manager.prompt_guidance()
        self.assertIn("反派节奏", guidance)
        self.assertIn("有意义桌面交换", guidance)
        self.assertIn("GM主动节拍", guidance)
        self.assertGreaterEqual(plan.expected_table_turns[0], 18)
        self.assertTrue(plan.gm_autonomy_cadence)

    def test_all_supported_campaign_lengths_keep_four_hour_session_shape(self) -> None:
        expectations = (
            (CampaignLength.SHORT, 20, 4),
            (CampaignLength.STANDARD, 35, 5),
            (CampaignLength.LONG, 50, 6),
        )
        for length, target_sessions, target_arcs in expectations:
            with self.subTest(length=length):
                manager = self._manager_with_clocks()
                profile = manager.configure(length=length)
                plan = manager.refresh_plan(force_session_number=1)

                self.assertEqual(profile.target_sessions, target_sessions)
                self.assertEqual(profile.target_arcs, target_arcs)
                self.assertGreaterEqual(plan.expected_scene_count[0], 3)
                self.assertGreaterEqual(plan.expected_table_turns[0], 28)
                self.assertIn("转折", "；".join(plan.session_structure))
                self.assertIn("收束", "；".join(plan.session_structure))

    def test_session_cannot_end_on_turn_count_without_resolved_question_or_earned_cliffhanger(self) -> None:
        manager = self._manager_with_clocks()
        incomplete = SessionFeedbackSignals(session_number=1, meaningful_turns=32, scene_count=3)

        can_end, reasons = manager.assess_session_completion(incomplete)

        self.assertFalse(can_end)
        self.assertTrue(any("核心问题" in reason for reason in reasons))

        complete = SessionFeedbackSignals(
            session_number=1,
            meaningful_turns=32,
            scene_count=3,
            local_question_resolved=True,
            choice_count=3,
            consequence_count=2,
            memory_anchor_complete=True,
            signature_image_evolved=True,
            local_payoff_present=True,
            villain_move_observed=True,
        )
        self.assertTrue(manager.assess_session_completion(complete)[0])

    def test_changed_but_unresolved_question_does_not_end_session(self) -> None:
        manager = self._manager_with_clocks()
        feedback = SessionFeedbackSignals(
            session_number=1,
            meaningful_turns=32,
            scene_count=3,
            local_question_changed=True,
            reversal_reached=True,
            choice_count=3,
            consequence_count=2,
            memory_anchor_complete=True,
            signature_image_evolved=True,
            local_payoff_present=True,
            villain_move_observed=True,
        )

        can_end, reasons = manager.assess_session_completion(feedback)

        self.assertFalse(can_end)
        self.assertTrue(any("核心问题尚未解决" in reason for reason in reasons))

    def test_deliberate_cliffhanger_after_reversal_can_end_session(self) -> None:
        manager = self._manager_with_clocks()
        feedback = SessionFeedbackSignals(
            session_number=1,
            meaningful_turns=32,
            scene_count=3,
            local_question_changed=True,
            deliberate_cliffhanger=True,
            reversal_reached=True,
            choice_count=3,
            consequence_count=2,
            memory_anchor_complete=True,
            signature_image_evolved=True,
            local_payoff_present=True,
            villain_move_observed=True,
        )

        self.assertTrue(manager.assess_session_completion(feedback)[0])

    def test_session_cannot_end_with_blocking_player_choice(self) -> None:
        manager = self._manager_with_clocks()
        feedback = SessionFeedbackSignals(
            session_number=1,
            meaningful_turns=32,
            scene_count=3,
            local_question_resolved=True,
            choice_count=2,
            consequence_count=2,
            villain_move_observed=True,
            memory_anchor_complete=True,
            signature_image_evolved=True,
            local_payoff_present=True,
            pending_blocking_decision_count=1,
        )

        can_end, reasons = manager.assess_session_completion(feedback)

        self.assertFalse(can_end)
        self.assertTrue(any("待决选择" in reason for reason in reasons))

    def test_session_cannot_end_with_unperformed_accepted_bargain(self) -> None:
        manager = self._manager_with_clocks()
        feedback = SessionFeedbackSignals(
            session_number=1,
            meaningful_turns=32,
            scene_count=3,
            local_question_resolved=True,
            choice_count=2,
            consequence_count=2,
            villain_move_observed=True,
            memory_anchor_complete=True,
            signature_image_evolved=True,
            local_payoff_present=True,
            pending_scene_commitment_count=1,
        )

        can_end, reasons = manager.assess_session_completion(feedback)

        self.assertFalse(can_end)
        self.assertTrue(any("尚未实际履行" in reason for reason in reasons))

    def test_roll_confirmation_does_not_replace_session_memory_choice(self) -> None:
        manager = self._manager_with_clocks()

        progress = manager.observe_turn(
            player_action=True,
            action_summary="艾丽妮决定冒险穿过排水旧道。",
        )
        manager.observe_turn(
            player_action=True,
            action_summary="投。",
            climax="艾丽妮越过崩塌的排水沟。",
        )

        self.assertEqual(
            progress.memory_choice,
            "艾丽妮决定冒险穿过排水旧道。",
        )

    def test_feedback_changes_next_session_plan_and_contract_requires_memory_anchor(self) -> None:
        manager = self._manager_with_clocks()
        manager.record_feedback(
            SessionFeedbackSignals(
                session_number=1,
                meaningful_turns=12,
                scene_count=1,
                resource_spend_events=0,
                villain_drought_sessions=2,
                unresolved_thread_count=8,
                stalled_beats=2,
            )
        )

        plan = manager.refresh_plan(force_session_number=2)

        joined = "；".join(plan.feedback_adjustments)
        self.assertIn("反派", joined)
        self.assertIn("过早收束", joined)
        self.assertIn("一个画面", plan.dramatic_contract.memory_anchor)
        self.assertIn("不能只发现线索", plan.dramatic_contract.closure_requirement)

    def test_first_act_answers_reach_session_concretizer_context(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        profile = world.world_profile
        profile.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        profile.starting_region = "卡里巴村"
        profile.first_act_questions = ["你们为什么被关起来？"]
        profile.first_act_question_answers = {
            "你们为什么被关起来？": ["诺艾尔因盗取男爵藏品被捕。"],
        }
        planner = SessionContractPlanner(StoryArcManager(world, clocks), world)

        context = planner._world_context(
            focus_title="雨夜越狱",
            focus_summary=profile.selected_first_act_summary,
            location="卡里巴村",
            opposition_goal="典狱方要恢复封印",
            spotlight="诺艾尔",
        )

        self.assertEqual(
            context["first_act_setup"]["summary"],
            "第一幕从卡里巴村监狱越狱开始。",
        )
        self.assertEqual(
            context["first_act_setup"]["answers"]["你们为什么被关起来？"],
            ["诺艾尔因盗取男爵藏品被捕。"],
        )

    def test_scene_stall_directive_is_not_mistaken_for_session_closure(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        profile = world.world_profile
        profile.selected_first_act_summary = "第一幕从卡里巴村监狱越狱开始。"
        profile.starting_region = "卡里巴村"
        manager = CampaignPacingManager(StoryArcManager(world, clocks), clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertNotIn("若当前场景停滞", contract.closure_requirement)
        self.assertIn("不能只发现线索", contract.closure_requirement)

    def test_feedback_demands_callback_payoff_and_distinct_memory_when_recent_session_was_flat(self) -> None:
        manager = self._manager_with_clocks()
        manager.record_feedback(
            SessionFeedbackSignals(
                session_number=1,
                meaningful_turns=30,
                scene_count=3,
                previous_consequence_recalled=False,
                session_identity_distinct=False,
                memory_similarity_to_recent=0.8,
                signature_image_evolved=False,
                local_payoff_present=False,
            )
        )

        plan = manager.refresh_plan(force_session_number=2)
        guidance = "；".join(plan.feedback_adjustments)

        self.assertIn("上一场选择后果", guidance)
        self.assertIn("记忆点过于相似", guidance)
        self.assertIn("标志画面", guidance)
        self.assertIn("只铺线没有兑现", guidance)

    def test_contract_stays_stable_during_same_session(self) -> None:
        manager = self._manager_with_clocks()
        first = manager.refresh_plan(force_session_number=3)
        first.dramatic_contract.signature_image = "风铃里冻结着一滴逆流的雨。"

        second = manager.refresh_plan(force_session_number=3)

        self.assertEqual(second.dramatic_contract.signature_image, "风铃里冻结着一滴逆流的雨。")

    def test_same_session_contract_is_recovered_from_history_when_plan_envelope_is_missing(self) -> None:
        manager = self._manager_with_clocks()
        recovered = SessionDramaticContract(
            session_number=1,
            title="白花碑驿站的迟响",
            location="白花碑驿站·风铃廊",
            dramatic_question="守望会是否会为失忆旅人开放旧路？",
            status="planned",
        )
        manager.story_arc_manager.state.session_contract_history = [recovered]
        manager.story_arc_manager.state.current_pacing_plan = SessionPacingPlan()

        plan = manager.refresh_plan(force_session_number=1)

        self.assertIs(plan.dramatic_contract, recovered)
        self.assertEqual(plan.dramatic_contract.title, "白花碑驿站的迟响")
        self.assertIn("失忆旅人", plan.dramatic_contract.dramatic_question)

    def test_first_session_prefers_confirmed_first_act_and_longest_public_location(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        profile = world.world_profile
        profile.selected_first_act_summary = "英雄被关在卡里巴村监狱，第一幕必须设法越狱。"
        profile.major_locations["星落尖塔"] = "遥远的旧时代遗迹。"
        profile.major_locations["卡里巴"] = "村落周边地区。"
        profile.major_locations["卡里巴村"] = "帝国控制下的边境村落。"
        story = StoryArcManager(world, clocks)
        story.state.threads.append(
            StoryThread(
                thread_id="legacy-world-threat",
                title="索朗帝国的魔法瘟疫",
                thread_type="world_threat",
                summary="索朗帝国试图重新掌控新形态的生命。",
                priority=3,
                progress=6,
                source="world.world_threats",
            )
        )
        story.state.villain_pressure.append(
            VillainPressureTrack(
                track_id="empire-plague",
                villain="索朗帝国",
                goal="重新掌控新形态的生命，并制造失控的魔法瘟疫",
                segments=6,
            )
        )
        story.state.reveals.append(
            RevealCandidate(
                reveal_id="soul-flow",
                title="被抽取的灵魂能源最终流向了哪里？",
                secret="能源被送往远方的星落尖塔。",
            )
        )
        manager = CampaignPacingManager(story, clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertIn("越狱", contract.focus_thread)
        self.assertNotIn("魔法瘟疫", contract.focus_thread)
        self.assertEqual(contract.location, "卡里巴村")
        local_contract_text = repr(
            (
                contract.opening_disruption,
                contract.opposition_goal,
                contract.reversal,
                contract.flexible_secrets,
                contract.escalation_ladder,
                contract.potential_scenes,
            )
        )
        self.assertNotIn("魔法瘟疫", local_contract_text)
        self.assertNotIn("灵魂能源", local_contract_text)
        self.assertIn("恢复封锁", contract.opposition_goal)

    def test_wrong_reused_first_session_contract_is_rebuilt_from_confirmed_setup(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        profile = world.world_profile
        profile.selected_first_act_summary = "英雄被关在卡里巴村监狱，第一幕必须设法越狱。"
        profile.major_locations["星落尖塔"] = "遥远的旧时代遗迹。"
        profile.major_locations["卡里巴村"] = "帝国控制下的边境村落。"
        story = StoryArcManager(world, clocks)
        wrong = SessionDramaticContract(
            session_number=1,
            title="第01场·索朗帝国的魔法瘟疫",
            location="星落尖塔",
            focus_thread="索朗帝国的魔法瘟疫",
            dramatic_question="英雄能否阻止帝国重掌生命？",
            status="planned",
        )
        story.state.current_pacing_plan = SessionPacingPlan(
            session_number=1,
            dramatic_contract=wrong,
        )
        story.state.session_contract_history = [wrong]
        manager = CampaignPacingManager(story, clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertIsNot(contract, wrong)
        self.assertIn("越狱", contract.focus_thread)
        self.assertEqual(contract.location, "卡里巴村")
        self.assertIs(story.state.session_contract_history[0], contract)

    def test_gm_beat_directive_repairs_wrong_first_session_contract_before_use(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        profile = world.world_profile
        profile.selected_first_act_summary = "英雄被关在卡里巴村监狱，第一幕必须设法越狱。"
        profile.major_locations["星落尖塔"] = "遥远的旧时代遗迹。"
        profile.major_locations["卡里巴村"] = "帝国控制下的边境村落。"
        story = StoryArcManager(world, clocks)
        wrong = SessionDramaticContract(
            session_number=1,
            title="第01场·索朗帝国的魔法瘟疫",
            location="星落尖塔",
            focus_thread="索朗帝国的魔法瘟疫",
            dramatic_question="英雄能否阻止帝国重掌生命？",
            status="planned",
        )
        story.state.current_pacing_plan = SessionPacingPlan(
            session_number=1,
            dramatic_contract=wrong,
        )
        story.state.session_contract_history = [wrong]
        manager = CampaignPacingManager(story, clocks, world)

        manager.gm_beat_directive()

        repaired = story.state.current_pacing_plan.dramatic_contract
        self.assertIsNot(repaired, wrong)
        self.assertIn("越狱", repaired.focus_thread)
        self.assertEqual(repaired.location, "卡里巴村")

    def test_planned_contract_does_not_consume_active_chapter_package(self) -> None:
        manager = self._manager_with_clocks()
        manager.world_state.register_chapter_package(
            ChapterPackage(
                chapter_title="白花碑驿站的迟响",
                intro_prompt="失忆旅人在风铃廊听见自己的名字。",
                scenes=[
                    ChapterPackageScene(
                        title="风铃廊问路",
                        location="白花碑驿站·风铃廊",
                        required_elements=["失忆旅人"],
                    )
                ],
                status="ready",
            )
        )
        manager.story_arc_manager.state.session_contract_history = [
            SessionDramaticContract(
                session_number=1,
                title="白花碑驿站的迟响",
                status="planned",
            )
        ]

        contract = manager.contract_planner.create(
            session_number=1,
            phase=StoryArcPhase.OPENING,
            profile=manager.story_arc_manager.state.pacing_profile,
            feedback=CampaignFeedbackControl(),
        )

        self.assertEqual(contract.title, "白花碑驿站的迟响")
        self.assertIn("失忆旅人", contract.opening_disruption)

    def test_completed_contract_consumes_active_chapter_package(self) -> None:
        manager = self._manager_with_clocks()
        manager.world_state.register_chapter_package(
            ChapterPackage(
                chapter_title="白花碑驿站的迟响",
                intro_prompt="失忆旅人在风铃廊听见自己的名字。",
                status="ready",
            )
        )
        manager.story_arc_manager.state.session_contract_history = [
            SessionDramaticContract(
                session_number=1,
                title="白花碑驿站的迟响",
                status="completed",
            )
        ]

        contract = manager.contract_planner.create(
            session_number=2,
            phase=StoryArcPhase.RISING,
            profile=manager.story_arc_manager.state.pacing_profile,
            feedback=CampaignFeedbackControl(),
        )

        self.assertNotEqual(contract.title, "白花碑驿站的迟响")

    def test_unfinished_table_session_continues_same_local_story(self) -> None:
        manager = self._manager_with_clocks()
        first = manager.refresh_plan(force_session_number=1)
        first.dramatic_contract.signature_image = "白花风铃没有影子。"
        manager.observe_scene_started("scene-1", opening_image="白花风铃没有影子。")
        manager.observe_turn(
            player_action=True,
            action_summary="英雄选择保护旅人。",
            consequence="守望会拒绝交出钥匙。",
        )

        progress = manager.finish_session_progress()
        second = manager.refresh_plan(force_session_number=2)

        self.assertFalse(progress.closure_ready)
        self.assertEqual(first.dramatic_contract.status, "continuing")
        self.assertEqual(
            second.dramatic_contract.local_question_key,
            first.dramatic_contract.local_question_key,
        )
        self.assertIn("续", second.dramatic_contract.title)
        self.assertIn("守望会拒绝交出钥匙", second.dramatic_contract.opening_disruption)
        self.assertTrue(
            all(
                item.scene_key.startswith("s02-")
                for item in second.dramatic_contract.potential_scenes
            )
        )
        self.assertNotEqual(
            [item.scene_key for item in second.dramatic_contract.potential_scenes],
            [item.scene_key for item in first.dramatic_contract.potential_scenes],
        )

    def test_contract_is_a_situation_brief_not_a_fixed_plot(self) -> None:
        manager = self._manager_with_clocks()

        plan = manager.refresh_plan(force_session_number=1)
        contract = plan.dramatic_contract

        self.assertTrue(contract.location)
        self.assertTrue(contract.opposition_goal)
        self.assertGreaterEqual(len(contract.escalation_ladder), 3)
        self.assertGreaterEqual(len(contract.possible_payoffs), 3)
        self.assertTrue(any("可附着" in item for item in contract.flexible_secrets))

    def test_contract_prepares_memorable_session_without_fixed_scene_order(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        world.world_profile.major_locations["白花碑驿站"] = (
            "建在白色钟碑旁的边境驿站，风铃会把遗失的名字送回廊下。"
        )
        world.world_profile.magic_tech_role = "灵魂能量会在辉钢与旧风铃之间共鸣"
        world.ensure_npc_persona(
            "守望会会长",
            public_identity="白花守望会会长",
            role_in_story="旧路的守门人",
            core_drive="不让财团夺走驿站保存的名字",
            goals=["在不牺牲巡守的前提下保护失忆旅人"],
            current_location="白花碑驿站",
        )
        story = StoryArcManager(world, clocks)
        story.state.threads = [
            StoryThread(
                thread_id="lost-name",
                title="失名旅人的归路",
                summary="护送旅人会暴露驿站旧路，但留下他会让财团先找到他。",
                entities=["守望会会长"],
                public_clues=["风铃内侧刻着旅人已经忘记的名字。"],
                priority=3,
            )
        ]
        story.state.locations = [
            LocationReturnState(
                location="白花碑驿站",
                changes=["财团封蜡出现在后门。"],
                next_prompt="旧路闸门只在第一阵归潮铃后开启。",
            )
        ]
        story.state.reveals = [
            RevealCandidate(
                reveal_id="bell-memory",
                title="风铃保存了被收购的名字",
                secret="守望会一直在用旧风铃藏匿记忆。",
            )
        ]
        story.state.villain_pressure = [
            VillainPressureTrack(
                track_id="consortium",
                villain="监察官艾蕾娜",
                goal="在归潮前封锁旧路并带走旅人",
                segments=6,
            )
        ]
        manager = CampaignPacingManager(story, clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertGreaterEqual(len(contract.potential_scenes), 4)
        self.assertLessEqual(len(contract.potential_scenes), 5)
        self.assertEqual(len({item.scene_key for item in contract.potential_scenes}), len(contract.potential_scenes))
        self.assertEqual(contract.potential_scenes[0].scene_role, "strong_start")
        self.assertFalse(contract.potential_scenes[0].optional)
        self.assertTrue(all(item.entry_points for item in contract.potential_scenes))
        self.assertEqual(len(contract.clue_routes), 3)
        self.assertEqual(len({item.approach for item in contract.clue_routes}), 3)
        self.assertEqual(len({item.conclusion for item in contract.clue_routes}), 1)
        self.assertTrue(any(item.name == "守望会会长" for item in contract.important_npcs))
        self.assertTrue(any("灵魂能量" in item for item in contract.fantastic_details))
        self.assertTrue(any("可换序" not in item.title for item in contract.potential_scenes))
        self.assertEqual(len({item.location for item in contract.potential_scenes}), 5)
        self.assertTrue(all(item.location.startswith("白花碑驿站·") for item in contract.potential_scenes))

    def test_first_session_uses_confirmed_starting_region_not_backstage_location(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        world.world_profile.starting_region = "白花碑驿站"
        world.world_profile.major_locations["白花碑驿站"] = "雾潮海岸的边境驿站。"
        world.world_profile.gm_prepared_locations["噬神古林"] = "尚未公开的后台灵感地点。"
        story = StoryArcManager(world, clocks)
        manager = CampaignPacingManager(story, clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertEqual(contract.location, "白花碑驿站")
        self.assertNotIn("噬神古林", contract.signature_image)

    def test_active_chapter_package_is_the_authoritative_first_session_skeleton(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        world.world_profile.starting_region = "白花碑驿站"
        world.world_profile.major_locations["白花碑驿站"] = "雾潮海岸的边境驿站。"
        world.register_chapter_package(
            ChapterPackage(
                chapter_title="迟响的白花铃",
                synopsis="英雄必须取得旧路通行、保护失忆旅人，并找出财团昨夜使用旧路的第一份证据。",
                intro_prompt="一枚染血的旧路铜钥匙被拍在驿站柜台上，第三盏路灯同时熄灭。",
                conclusion_prompt="本场结束前必须明确旧路是否开放、旅人由谁保护，以及财团入侵证据落在谁手中。",
                iconic_elements=["迟响一拍的白花风铃"],
                adversary_notes=["财团搜查队要在午夜前封住旧路并带走旅人。"],
                scenes=[
                    ChapterPackageScene(
                        title="风铃廊问路",
                        scene_type="social_conflict",
                        location="白花碑驿站·风铃廊",
                        purpose="让守望会公开旧路通行条件",
                        required_elements=["旧路铜钥匙", "失忆旅人"],
                    ),
                    ChapterPackageScene(
                        title="风铃回声仪式",
                        scene_type="ritual",
                        location="白花碑驿站·登记小室",
                        purpose="用仪式确认昨夜复制钥匙的回声",
                        required_elements=["复制钥匙", "第七道钟裂"],
                    ),
                    ChapterPackageScene(
                        title="旧路闸门与巡逻队",
                        scene_type="climax",
                        location="白花碑驿站·旧路闸门",
                        purpose="在巡逻队封门前决定驿站与旅人的去向",
                        required_elements=["旧路闸门", "财团巡逻队"],
                    ),
                ],
            )
        )
        manager = CampaignPacingManager(StoryArcManager(world, clocks), clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract

        self.assertEqual(contract.title, "迟响的白花铃")
        self.assertIn("染血", contract.opening_disruption)
        self.assertIn("旅人由谁保护", contract.closure_requirement)
        self.assertEqual(
            [scene.title for scene in contract.potential_scenes[:3]],
            ["风铃廊问路", "风铃回声仪式", "旧路闸门与巡逻队"],
        )
        self.assertEqual(len(contract.potential_scenes), 4)
        self.assertIn("旧路铜钥匙", contract.potential_scenes[0].situation)
        self.assertEqual(
            contract.potential_scenes[0].required_elements,
            ["旧路铜钥匙"],
        )
        self.assertEqual(
            contract.potential_scenes[0].required_npc_names,
            ["失忆旅人"],
        )
        self.assertIn("复制钥匙", contract.potential_scenes[1].situation)
        self.assertEqual(
            [scene.scene_role for scene in contract.potential_scenes],
            [
                "strong_start",
                "alternate_approach",
                "climax_candidate",
                "aftermath",
            ],
        )
        self.assertEqual(
            [scene.location for scene in contract.potential_scenes[:3]],
            [
                "白花碑驿站·风铃廊",
                "白花碑驿站·登记小室",
                "白花碑驿站·旧路闸门",
            ],
        )
        self.assertIn("出口", contract.potential_scenes[-1].location)
        self.assertIn("旧路闸门与巡逻队", contract.irreversible_change)
        self.assertIn("迟响一拍的白花风铃", contract.ending_echo)
        self.assertTrue(contract.memory_anchor.startswith("一个画面："))
        self.assertNotIn(
            "先让NPC立场、环境或时间压力发生一个可见变化。",
            contract.escalation_ladder,
        )

    def test_generic_world_threat_never_leaks_into_playable_chapter_contract(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        world.world_profile.starting_region = "白花碑驿站"
        world.world_profile.major_locations["白花碑驿站"] = (
            "白钟大陆南岸，保存失去名字与白花风铃的边境驿站。"
        )
        world.world_profile.world_threats = [
            "辉钢财团正在把灰晶病患者的记忆作为可买卖燃料"
        ]
        world.world_profile.mysteries = [
            "姐姐的名字为何刻在白花风铃内侧却无人记得她死亡"
        ]
        world.register_chapter_package(
            ChapterPackage(
                chapter_title="白花碑驿站的迟响",
                synopsis="护送失忆旅人，并在财团封路前争取旧路通行。",
                intro_prompt="失忆旅人听见自己的名字，门外传来财团巡逻的金属回声。",
                conclusion_prompt=(
                    "当队伍获得旧路通行、确认财团收购记忆的第一条证据，"
                    "并决定如何保护失忆旅人时，本章进入收束。"
                ),
                iconic_elements=["迟响一拍的白花风铃"],
                adversary_notes=[
                    "监察官艾蕾娜应主动推进财团目标，但不要替玩家决定角色行动。"
                ],
                scenes=[
                    ChapterPackageScene(
                        title="风铃廊问路",
                        scene_type="social_conflict",
                        location="白花碑驿站·风铃廊",
                        purpose="争取守望会开放旧路",
                        required_elements=["白花风铃", "失忆旅人", "白花守望会会长"],
                    ),
                    ChapterPackageScene(
                        title="旧路闸门与巡逻队",
                        scene_type="climax",
                        location="白花碑驿站·旧路闸门",
                        purpose="在巡逻队封锁前决定旅人的去向",
                        required_elements=["旧路闸门", "监察官艾蕾娜"],
                    ),
                ],
            )
        )
        manager = CampaignPacingManager(StoryArcManager(world, clocks), clocks, world)

        contract = manager.refresh_plan(force_session_number=1).dramatic_contract
        npc_names = [item.name for item in contract.important_npcs]

        self.assertIn("监察官艾蕾娜", npc_names)
        self.assertIn("白花守望会会长", npc_names)
        self.assertIn("失忆旅人", npc_names)
        self.assertNotIn("世界威胁", npc_names)
        inspector = next(
            item for item in contract.important_npcs if item.name == "监察官艾蕾娜"
        )
        self.assertEqual(inspector.public_role, "监察官")
        self.assertIn("辉钢财团", inspector.goal_now)
        self.assertNotIn("不要替玩家", contract.opposition_goal)
        self.assertIn("辉钢财团", contract.opposition_goal)
        self.assertIn("获得旧路通行", contract.dramatic_question)
        self.assertNotIn("实质改变", contract.dramatic_question)
        self.assertIn("白花风铃", contract.signature_image)
        self.assertNotIn("选定一件", contract.signature_image)
        self.assertTrue(
            all("世界威胁" not in item.source for item in contract.clue_routes)
        )
        self.assertTrue(
            all(
                "世界威胁" not in scene.npc_names
                for scene in contract.potential_scenes
            )
        )
        self.assertIn(
            "白花守望会会长",
            contract.potential_scenes[0].required_npc_names,
        )

    def test_later_session_follows_most_recently_visited_public_location(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        world.world_profile.starting_region = "白花碑驿站"
        world.world_profile.major_locations.update(
            {"白花碑驿站": "起点。", "钟鸣公国": "内海北岸。"}
        )
        story = StoryArcManager(world, clocks)
        story.sync_from_world_profile()
        story.state.processed_session_ids = ["session-1"]
        for item in story.state.locations:
            if item.location == "钟鸣公国":
                item.last_seen = "session-1"
        manager = CampaignPacingManager(story, clocks, world)

        contract = manager.refresh_plan(force_session_number=2).dramatic_contract

        self.assertEqual(contract.location, "钟鸣公国")

    def test_recent_resource_overload_reduces_auto_pressure_budget(self) -> None:
        manager = self._manager_with_clocks()
        manager.record_feedback(
            SessionFeedbackSignals(
                session_number=1,
                meaningful_turns=30,
                scene_count=3,
                resource_pressure_ratio=0.7,
                local_question_changed=True,
            )
        )

        plan = manager.refresh_plan(force_session_number=2)

        self.assertEqual(plan.pressure_budget.max_auto_advance_clocks, 0)
        self.assertTrue(any("恢复" in item for item in plan.feedback_adjustments))

    def test_production_episode_feedback_carries_resource_and_stall_telemetry(self) -> None:
        manager = self._manager_with_clocks()
        progress = SessionEpisodeProgress(
            session_number=1,
            meaningful_turns=28,
            resource_spend_events=4,
            resource_pressure_ratio=0.6,
            max_stagnant_player_turns=7,
            memory_image="白花风铃在断桥上逆风齐鸣。",
            memory_choice="英雄留下来保护失名旅人。",
            memory_consequence="守望会因此关闭正门并开放旧路。",
        )

        feedback = manager.feedback_from_episode(progress)

        self.assertEqual(feedback.resource_spend_events, 4)
        self.assertEqual(feedback.resource_pressure_ratio, 0.6)
        self.assertEqual(feedback.stalled_beats, 2)

    def test_production_feedback_detects_repeated_session_memory_anchor(self) -> None:
        manager = self._manager_with_clocks()
        prior = SessionEpisodeProgress(
            session_number=1,
            memory_image="白花风铃在断桥上逆风齐鸣。",
            memory_choice="英雄留下来保护失名旅人。",
            memory_consequence="守望会因此关闭正门并开放旧路。",
        )
        manager.story_arc_manager.state.session_progress_history.append(prior)
        repeated = SessionEpisodeProgress(
            session_number=2,
            memory_image=prior.memory_image,
            memory_choice=prior.memory_choice,
            memory_consequence=prior.memory_consequence,
        )

        feedback = manager.feedback_from_episode(repeated)

        self.assertGreaterEqual(feedback.memory_similarity_to_recent, 0.72)
        self.assertFalse(feedback.session_identity_distinct)

    def test_episode_progress_needs_choice_consequence_and_climax(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1", opening_image="逆风响起的白花风铃。")
        for index in range(28):
            manager.observe_turn(
                player_action=True,
                action_summary=f"英雄作出选择 {index}",
                consequence="守望会改变了对英雄的态度。" if index == 3 else "",
            )
        self.assertFalse(manager.story_arc_manager.state.current_session_progress.closure_ready)

        manager.observe_scene_started("scene-2")
        manager.observe_turn(
            player_action=True,
            action_summary="英雄核对风铃与旧路记录。",
            reveal="记录证明守望会曾为同样的旅人打开过旧路。",
            reversal=True,
        )
        manager.observe_scene_started("scene-3")
        manager.observe_turn(
            player_action=True,
            action_summary="英雄选择承担担保并要求立刻开启旧路。",
        )
        manager.observe_turn(
            player_action=False,
            climax="旧路闸门在众人的选择下真正开启。",
            opposition_move="守望会落下正门，迫使英雄立即决定去留。",
            local_question_changed=True,
            signature_image_evolved=True,
            public_image="白花风铃在打开的旧路上方第一次同时响起。",
        )

        progress = manager.finish_session_progress()
        self.assertTrue(progress.closure_ready)
        self.assertEqual(progress.substantial_scene_ids, ["scene-1", "scene-2", "scene-3"])
        self.assertEqual(progress.stage, "closure")
        self.assertEqual(progress.memory_image, "白花风铃在打开的旧路上方第一次同时响起。")

    def test_opening_empty_scenes_does_not_inflate_episode_scene_count(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)

        manager.observe_scene_started("scene-1")
        manager.observe_scene_started("scene-2")
        manager.observe_scene_started("scene-3")

        progress = manager.story_arc_manager.state.current_session_progress
        feedback = manager.feedback_from_episode(progress)
        self.assertEqual(progress.scene_ids, ["scene-1", "scene-2", "scene-3"])
        self.assertEqual(progress.substantial_scene_ids, [])
        self.assertEqual(feedback.scene_count, 0)

    def test_current_scene_becomes_substantial_only_after_action_changes_world(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1")

        manager.observe_turn(player_action=True, action_summary="英雄检查门锁。")
        progress = manager.story_arc_manager.state.current_session_progress
        self.assertEqual(progress.substantial_scene_ids, [])

        manager.observe_turn(
            player_action=False,
            opposition_move="守门人当场封死侧门。",
            consequence="侧门暂时无法通行。",
        )
        self.assertEqual(progress.substantial_scene_ids, ["scene-1"])

    def test_restored_scene_focus_receives_later_pacing_evidence(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1")
        manager.observe_turn(
            player_action=True,
            action_summary="伊莉雅留在风铃廊稳住守望会。",
            consequence="守望会暂缓关门。",
        )
        manager.observe_scene_started("scene-2")
        manager.observe_turn(
            player_action=True,
            action_summary="洛岚在登记小室核对旧册。",
            consequence="旧册中露出被删改的一页。",
        )

        progress = manager.observe_scene_focused("scene-1")
        manager.observe_turn(
            player_action=True,
            action_summary="伊莉雅向会长提交担保。",
            local_payoff="会长接受担保并交出旧路钥匙。",
        )

        self.assertEqual(progress.active_scene_id, "scene-1")
        self.assertEqual(progress.scene_progress["scene-1"].player_actions, 2)
        self.assertEqual(progress.scene_progress["scene-2"].player_actions, 1)
        self.assertEqual(progress.scene_ids, ["scene-1", "scene-2"])

    def test_episode_records_gm_beat_phase_and_latest_player_change(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1")

        manager.observe_turn(
            player_action=False,
            opposition_move="巡守当场落下外门。",
            gm_beat_purpose="escalation",
        )
        progress = manager.observe_turn(
            player_action=True,
            action_summary="英雄拆下外门的锁销。",
            consequence="外门恢复通行。",
        )

        self.assertEqual(progress.gm_beat_purposes, ["escalation"])
        self.assertEqual(progress.gm_beat_player_turns, [0])
        self.assertEqual(progress.last_player_material_change_turn, 1)

    def test_ordinary_clue_does_not_mark_session_reversal(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1")

        progress = manager.observe_turn(
            player_action=True,
            action_summary="英雄检查黑蜡碎屑。",
            reveal="黑蜡与旧印来自同一批材料。",
        )

        self.assertFalse(progress.reversal_reached)
        self.assertNotEqual(progress.stage, "reversal")

        progress = manager.observe_turn(
            player_action=True,
            action_summary="英雄把被刮去的名字拼回去。",
            reveal="被刮去的名字属于伊莉雅本应记得的人。",
            reversal=True,
        )
        self.assertTrue(progress.reversal_reached)
        self.assertEqual(progress.stage, "reversal")

    def test_fulfilled_pressure_consequence_advances_episode_to_reversal(self) -> None:
        manager = self._manager_with_clocks()
        manager.refresh_plan(force_session_number=1)
        manager.observe_scene_started("scene-1")

        progress = manager.observe_turn(
            player_action=False,
            consequence="财团巡逻队包围白花碑驿站。",
            reversal=True,
            opposition_move="财团巡逻队包围白花碑驿站。",
            local_question_changed=True,
        )

        self.assertEqual(progress.stage, "reversal")
        self.assertTrue(progress.reversal_reached)
        self.assertTrue(progress.local_question_changed)

    def test_legacy_ellipsized_contract_uses_complete_thread_summary(self) -> None:
        clocks = ClockManager()
        world = WorldState()
        story = StoryArcManager(world, clocks)
        story.state.threads = [
            StoryThread(
                thread_id="threat-1",
                title="辉钢财团正在把灰晶病患者的记忆作为可...",
                thread_type="world_threat",
                summary="辉钢财团正在把灰晶病患者的记忆作为可买卖燃料",
            )
        ]
        manager = CampaignPacingManager(story, clocks, world)
        contract = SessionDramaticContract(
            session_number=1,
            title="第01场·辉钢财团正在把灰晶病患者的记忆作为可...",
            location="白花碑驿站",
            focus_thread="辉钢财团正在把灰晶病患者的记忆作为可...",
            dramatic_question="英雄能否改变【辉钢财团正在把灰晶病患者的记忆作为可...】？",
            closure_requirement="本场必须改变【辉钢财团正在把灰晶病患者的记忆作为可...】。",
        )

        fixed = manager.contract_planner.repair_legacy_contract_identity(contract)

        self.assertEqual(fixed.focus_thread, "辉钢财团正在把灰晶病患者的记忆作为可买卖燃料")
        self.assertIn("可买卖燃料", fixed.dramatic_question)
        self.assertNotIn("可...", fixed.title)
        self.assertEqual(len({scene.location for scene in fixed.potential_scenes}), 5)


if __name__ == "__main__":
    unittest.main()
