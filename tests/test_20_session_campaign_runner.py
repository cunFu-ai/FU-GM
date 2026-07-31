import pytest
from types import SimpleNamespace

from scripts.run_20_session_campaign_test import CampaignSessionSpec, TwentySessionCampaignHarness
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_transition_coordinator import SceneTransitionAnchor
from fu_gm.components.scene_cast_coordinator import SceneCastCoordinator
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import SceneType
from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.replay_models import LegalActionContext


def test_session_zero_fixture_is_incremental_and_contains_real_table_discussion() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)

    turns = harness._session_zero_world_turns()
    messages = [message for _speaker, message in turns]

    assert len(turns) >= 13
    assert any("大家觉得" in message for message in messages)
    assert any("我赞成" in message for message in messages)
    assert any("先跳过" in message for message in messages)
    assert not any(
        all(token in message for token in ("魔法与科技", "界限：", "重大历史事件", "世界奥秘", "世界威胁"))
        for message in messages
    )


def test_contract_quality_inputs_are_available_to_session_report() -> None:
    contract = SimpleNamespace(
        important_npcs=[SimpleNamespace(name="白花守望会会长")],
        potential_scenes=[
            SimpleNamespace(
                npc_names=["失忆旅人"],
                required_npc_names=["白花守望会会长"],
            )
        ],
        clue_routes=[SimpleNamespace(source="迟响风铃")],
    )

    quality = TwentySessionCampaignHarness._contract_quality_inputs(contract)

    assert quality == {
        "prepared_npc_names": ["白花守望会会长"],
        "scene_cast_names": ["失忆旅人", "白花守望会会长"],
        "clue_sources": ["迟响风铃"],
    }


def test_strict_longrun_stops_on_route_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.fail_fast_route_mismatch = True
    harness.calls = []

    monkeypatch.setattr(
        TwentySessionCampaignHarness.__mro__[1],
        "route_table_message",
        lambda *_args, **_kwargs: {"target": "silent", "send_reply": False},
    )

    with pytest.raises(RuntimeError, match="玩家消息路由"):
        harness.route_table_message(
            "严格路由样本",
            "白河",
            "洛岚沿旧路离开。",
            expected_target="fu_gm",
            expected_send_reply=True,
        )


def test_endurance_longrun_collects_route_mismatch_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.fail_fast_route_mismatch = False
    harness.calls = []

    monkeypatch.setattr(
        TwentySessionCampaignHarness.__mro__[1],
        "route_table_message",
        lambda *_args, **_kwargs: {"target": "silent", "send_reply": False},
    )

    body = harness.route_table_message(
        "耐久路由样本",
        "白河",
        "洛岚沿旧路离开。",
        expected_target="fu_gm",
        expected_send_reply=True,
    )

    assert body == {"target": "silent", "send_reply": False}


def test_session_scene_records_include_all_active_split_party_cameras() -> None:
    scenes = SceneManager()
    first = scenes.start_scene(
        "风铃廊",
        SceneType.STANDARD,
        location="白花碑驿站·风铃廊",
        participants=["伊莉雅"],
    )
    second, _ = scenes.focus_actor_branch(
        "洛岚",
        name="登记小室",
        location="白花碑驿站·登记小室",
    )
    third, _ = scenes.focus_actor_branch(
        "赛璃",
        name="旧路闸门",
        location="白花碑驿站·旧路闸门",
    )
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=scenes)
    )

    records = harness._current_session_scene_records(0)

    assert {item.scene_id for item in records} == {
        first.scene_id,
        second.scene_id,
        third.scene_id,
    }
    assert harness._current_session_scene_count(0) == 3


def test_llm_preflight_failure_is_recorded_as_core_agent_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def create_chat_completion(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    harness = object.__new__(TwentySessionCampaignHarness)
    harness._llm_preflight_attempted = False
    harness._llm_preflight_ok = False
    harness._llm_preflight_error = ""
    component = SimpleNamespace(client=FailingClient(), model="test-model")
    harness.service = SimpleNamespace(gm_tool_agent=component)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            expressor=None,
            npc_combat_rules=None,
        )
    )
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "0")

    with pytest.raises(RuntimeError, match="长测 LLM 前置检查失败"):
        harness._assert_llm_preflight()

    assert harness._llm_preflight_attempted is True
    assert harness._llm_preflight_ok is False
    assert "provider unavailable" in harness._llm_preflight_error


def test_noncombat_setup_assertion_accepts_natural_negative_paraphrase() -> None:
    world = SimpleNamespace(
        consensus_notes=[],
        core_themes=["证据、承诺与情感能够改变立场和决定"],
        playstyle_themes=["第一章包含一场不依靠战斗解决的冲突"],
    )

    assert TwentySessionCampaignHarness._records_noncombat_resolution_preference(world)


def test_session_zero_character_fixture_answers_required_skill_option_before_confirmation() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)

    messages = [message for _speaker, message in harness._session_zero_character_turns()]
    option_index = messages.index("拟兽系仪式的施法属性我选洞察+意志。")
    confirmation_index = next(
        index
        for index, message in enumerate(messages)
        if "苍祈确认角色并正式建卡" in message
    )

    assert option_index < confirmation_index


def test_lane_pressure_detects_three_heroes_repeating_one_group_route() -> None:
    pressure = TwentySessionCampaignHarness._action_lane_pressure(
        [
            "洛岚接受北侧风铃廊旧阶，陪着失忆旅人避开主铃架。",
            "伊莉雅沿北侧风铃廊旧阶走，把失忆旅人带离失真的铃声。",
            "苍祈继续贴着北侧旧阶，护住失忆旅人，不让风铃牵着他走。",
        ]
    )

    assert pressure is not None
    assert {"road", "traveler", "wind_chime"}.issubset(pressure["anchors"])
    assert pressure["occurrences"] == 3


def test_quality_gate_does_not_treat_individually_authorized_group_move_as_loop() -> None:
    from fu_gm.testing.conversation_quality import ConversationQualityAuditor

    rows = []
    for index, (speaker, actor) in enumerate(
        (("阿凛", "伊莉雅"), ("南星", "赛璃"), ("白河", "洛岚"), ("时雨", "艾薇娅")),
        start=1,
    ):
        rows.append(
            {
                "label": f"第01场行动 {index:02d} {speaker}",
                "speaker": speaker,
                "message": f"{actor}沿风铃廊进入登记小室，与队友会合。",
                "body": {
                    "tool_receipts": [
                        {
                            "tool_name": "perform_in_scene_action",
                            "ok": True,
                            "state_changed": True,
                            "result": {"joined_current_focus": True, "actor": actor},
                        }
                    ]
                },
            }
        )

    report = ConversationQualityAuditor().audit(rows)

    assert report.repeated_player_action_lanes == 0


def test_synthetic_player_instruction_never_reads_unspoken_scene_frame_facts() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.calls = [
        {
            "label": "第01场行动 01 阿凛",
            "message": "伊莉雅检查薄钢牌。",
            "reply": "你只确认它是财团制式牌。",
        }
    ]
    spec = CampaignSessionSpec(
        number=1,
        title="雾中的牌子",
        arc="序章",
        gm_opening="",
        turns=[],
    )

    instruction = harness._player_action_diversity_instruction(spec)

    assert "三瓣花纹" not in instruction
    assert "银蓝晶粉" not in instruction


def test_gm_beat_reason_excludes_absent_npc_candidates_and_fallback_audit_text() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    contract = SimpleNamespace(
        escalation_ladder=[
            "【白花守望会会长】在风铃廊提出放行条件",
            "【监察官艾蕾娜】命令车队封路",
            "【旧路闸门与巡逻队】立即改变门外处置",
        ],
        important_npcs=[
            SimpleNamespace(name="白花守望会会长"),
            SimpleNamespace(name="监察官艾蕾娜"),
        ],
    )
    scene = SimpleNamespace(
        name="登记小室查册",
        location="白花碑驿站·登记小室",
        participants=["伊莉雅", "财团巡逻队"],
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(current_scene=scene),
            story_arc_manager=SimpleNamespace(
                state=SimpleNamespace(
                    current_pacing_plan=SimpleNamespace(dramatic_contract=contract)
                )
            ),
        )
    )
    harness.session_progress_assessments = {
        1: SimpleNamespace(
            next_gm_need="离线场次评估不可用，不能据此认定本场已经收束。",
            unresolved_now="",
            used_fallback=True,
        )
    }
    spec = CampaignSessionSpec(
        number=1,
        title="登记小室查册",
        arc="序章",
        gm_opening="",
        turns=[],
        expected_focus=["核对登记记录"],
    )

    reason = harness._gm_beat_reason(spec, 3)

    assert "当前参与者唯一名单是【伊莉雅、财团巡逻队】" in reason
    assert "【旧路闸门与巡逻队】立即改变门外处置" in reason
    assert "【白花守望会会长】在风铃廊提出放行条件" not in reason
    assert "【监察官艾蕾娜】命令车队封路" not in reason
    assert "离线场次评估不可用" not in reason


def test_priority_gm_beat_marker_is_not_hidden_behind_runtime_assessment() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.common = {}
    harness.session_progress_assessments = {
        1: SimpleNamespace(
            next_gm_need="继续追查尚未解决的登记记录。",
            unresolved_now="",
        )
    }
    captured = {}

    def invoke(_label, _method, _route, payload):
        captured.update(payload)
        return {"reply": "闸门后的道路显露出来。", "tool_receipts": []}

    harness.invoke = invoke
    harness._record_tool_event = lambda *_args, **_kwargs: None
    spec = CampaignSessionSpec(
        number=1,
        title="离开驿站",
        arc="序章",
        gm_opening="",
        turns=[],
    )

    harness._session_gm_beat(
        spec,
        1,
        "【玩家主导转场】队伍已经明确走入旧路，请兑现这次移动。",
    )

    assert captured["instruction"].startswith("【玩家主导转场】")
    assert "后台进展评估" not in captured["instruction"]


def test_scripted_table_discussion_uses_the_same_semantic_contract_as_dynamic_talk() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.calls = [{"reply": "巡守举起微光牌，示意队伍贴着左墙前进。"}]
    harness.campaign_id = "campaign"
    harness.session_id = "session"
    harness.channel_id = "channel"
    harness.service = object()
    harness.player_simulation_metrics = []
    harness._recent_public_dialogue = lambda **_kwargs: "时悠：巡守举起微光牌。"
    harness.player_legal_actions = SimpleNamespace(
        build=lambda *_args, **_kwargs: LegalActionContext(stage_goal="桌边商量")
    )
    captured = {}

    def compose(**kwargs):
        captured["message"] = kwargs["step"].message
        return SimpleNamespace(
            text="谁来盯着巡守的微光牌？",
            used_fallback=False,
            validation_errors=[],
            model_attempts=[{"contract": "table_discussion"}],
        )

    harness.player_simulator = SimpleNamespace(
        compose=compose,
        last_table_discussion_review={"pure_table_discussion": True},
    )
    spec = CampaignSessionSpec(number=1, title="旧路", arc="序章", gm_opening="", turns=[])

    result = harness._simulate_table_discussion(
        spec,
        8,
        scripted_message="那就先别分散，大家都贴着巡守的微光标记走。",
    )

    assert captured["message"] == "那就先别分散，大家都贴着巡守的微光标记走。"
    assert result == "谁来盯着巡守的微光牌？"


def test_agent_clarification_is_answered_by_same_player_before_speaker_cycle_advances() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.campaign_id = "campaign"
    harness.session_id = "session"
    harness.channel_id = "channel"
    harness.service = object()
    harness.player_simulation_metrics = []
    harness._recent_public_dialogue = lambda **_kwargs: "时悠：你想让装置执行哪一种规则功能？"
    harness.player_legal_actions = SimpleNamespace(
        build=lambda *_args, **_kwargs: LegalActionContext(
            stage_goal="回答GM追问",
            legal_skill_rules=[
                {
                    "skill_name": "便携装置",
                    "rule": "魔导装置基础增益只解锁魔导覆写，不是通用扫描器。",
                }
            ],
        )
    )
    harness.player_simulator = SimpleNamespace(
        compose=lambda **_kwargs: SimpleNamespace(
            text="白河：我不是发动魔导覆写，只用随身工具辅助听声，按普通调查处理。",
            used_fallback=False,
            validation_errors=[],
        )
    )
    routed = []

    def route(label, speaker, message, **kwargs):
        routed.append((label, speaker, message, kwargs))
        return {
            "target": "fu_gm",
            "send_reply": True,
            "reply": "请进行一次洞察检定。",
            "decision": {"agent_action": "final"},
        }

    harness.route_table_message = route
    spec = CampaignSessionSpec(number=1, title="雾中的回声", arc="序章", gm_opening="", turns=[])

    result = harness._answer_agent_clarification(
        spec,
        4,
        speaker="白河",
        actor="洛岚",
        body={
            "reply": "你想让便携装置执行哪一种已解锁的规则功能？",
            "decision": {"agent_action": "ask_user"},
        },
    )

    assert result["decision"]["agent_action"] == "final"
    assert routed[0][1] == "白河"
    assert "普通调查" in routed[0][2]
    assert routed[0][3]["directed_at_gm"] is True
    assert harness.player_simulation_metrics[0]["kind"] == "gm_clarification"


def test_fu_pl_skill_rules_expose_only_unlocked_portable_device_functions() -> None:
    rules = LegalActionLayer._skill_rules(
        SimpleNamespace(
            skills={"便携装置": 1},
            skill_options={"便携装置": ["魔导装置"]},
        )
    )

    portable = next(item for item in rules if item["name"] == "便携装置")
    assert "基础魔导装置仅有魔导覆写" in portable["description"]
    assert "通用扫描" in portable["description"]
    assert "魔法加农炮" not in portable["description"]
    assert "法球" not in portable["description"]


def test_legal_action_menu_removes_runtime_facts_not_said_in_public_chat() -> None:
    context = LegalActionContext(
        stage_goal="处理眼前局面",
        known_enemies=["监察官艾蕾娜"],
        known_npcs=["本地巡守", "暗处钟匠"],
        visible_scene_elements=["薄钢牌：财团制式牌", "密门：藏在柜台后"],
        established_scene_facts=[
            "薄钢牌属于财团。",
            "牌背藏着三瓣花纹与银蓝晶粉。",
        ],
        active_clocks=["【巡逻队逼近】2/6", "【暗门崩塌】1/4"],
        open_npc_conditions=[
            {
                "npc": "本地巡守",
                "condition": "先交出薄钢牌",
                "promised_result": "开放旧路",
            }
        ],
    )

    LegalActionLayer._restrict_to_public_context(
        context,
        "时悠：本地巡守指着薄钢牌说，那是财团制式牌。【巡逻队逼近】2/6。",
    )

    assert context.known_enemies == []
    assert context.known_npcs == ["本地巡守"]
    assert context.visible_scene_elements == ["薄钢牌：财团制式牌"]
    assert context.established_scene_facts == []
    assert context.active_clocks == ["【巡逻队逼近】2/6"]
    assert context.open_npc_conditions == []


def test_in_place_scene_cut_keeps_current_npcs_present() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.pc_names = ["伊莉雅", "赛璃"]

    participants = harness._scene_transition_participants(
        current_scene=SimpleNamespace(
            participants=["伊莉雅", "赛璃", "失忆旅人", "本地巡守"]
        ),
        transition_anchor=None,
        in_place=True,
    )

    assert participants == ["伊莉雅", "赛璃", "失忆旅人", "本地巡守"]


def test_scene_cast_keeps_existing_people_and_adds_prepared_required_npcs() -> None:
    opportunity = SimpleNamespace(
        required_npc_names=["白花守望会会长"],
        npc_names=["失忆旅人", "白花守望会会长"],
    )

    participants = SceneCastCoordinator.compose(
        ["伊莉雅", "赛璃"],
        opportunity=opportunity,
        established=["伊莉雅", "本地巡守"],
    )

    assert participants == [
        "伊莉雅",
        "赛璃",
        "本地巡守",
        "白花守望会会长",
        "失忆旅人",
    ]


def test_physical_scene_transition_carries_only_resolved_companions() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.pc_names = ["伊莉雅", "赛璃"]

    participants = harness._scene_transition_participants(
        current_scene=SimpleNamespace(
            participants=["伊莉雅", "赛璃", "守门人", "失忆旅人"]
        ),
        transition_anchor=SceneTransitionAnchor(
            location="下行旧路深处",
            participants=("赛璃", "失忆旅人"),
        ),
        in_place=False,
    )

    assert participants == ["伊莉雅", "赛璃", "失忆旅人"]


def test_pacing_sync_accepts_next_function_scene_already_opened_by_player_move() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "next_act": 2,
        "prepared_opportunity_key": "s01-chapter-2",
    }
    scene = SimpleNamespace(
        active=True,
        session_opportunity_key="s01-chapter-2",
        session_opportunity_role="alternate_approach",
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    spec = CampaignSessionSpec(number=1, title="迟响", arc="序章", gm_opening="", turns=[])

    assert harness._active_scene_represents_act(spec, 2)
    assert not harness._active_scene_represents_act(spec, 3)


def test_pacing_sync_accepts_public_exact_destination_without_test_metadata() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "next_act": 4,
        "target_location": "白花碑驿站·旧路出口外",
        "public_target_announced": True,
        "prepared_opportunity_key": "s01-chapter-aftermath",
    }
    scene = SimpleNamespace(
        active=True,
        location="白花碑驿站·旧路出口外",
        session_opportunity_key="",
        session_opportunity_role="",
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    spec = CampaignSessionSpec(number=1, title="离开驿站", arc="序章", gm_opening="", turns=[])

    assert harness._active_scene_represents_act(spec, 4)


def test_player_move_into_public_aftermath_counts_as_first_closure_response() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="白花碑驿站·旧路出口外")
            )
        )
    )

    started = harness._act_started_at_turn_after_sync(
        next_act=4,
        player_turn_count=48,
        transition_before={
            "public_target_announced": True,
            "target_location": "白花碑驿站·旧路出口外",
        },
    )

    assert started == 47


def test_act_sync_precedes_old_scene_pacing_recommendation() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            story_arc_manager=SimpleNamespace(
                state=SimpleNamespace(
                    current_session_progress=SimpleNamespace(
                        local_question_resolved=True
                    )
                )
            )
        )
    )
    harness._latest_world_action_is_unanswered = lambda: False
    harness._synchronize_active_scene_act = (
        lambda _spec, next_act: next_act == 4
    )
    spec = CampaignSessionSpec(number=1, title="余波", arc="序章", gm_opening="", turns=[])

    next_act = harness._advance_session_act_if_earned(
        spec,
        3,
        SimpleNamespace(),
        turns_in_act=1,
    )

    assert next_act == 4


def test_gm_only_act_change_does_not_impersonate_player_closure_response() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="白花碑驿站·旧路出口外")
            )
        )
    )

    started = harness._act_started_at_turn_after_sync(
        next_act=4,
        player_turn_count=48,
        transition_before={},
    )

    assert started == 48


def test_pending_scene_transition_reuses_the_first_public_prepared_candidate() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 1,
        "next_act": 2,
        "from_location": "白花碑驿站·风铃廊",
        "target_location": "白花碑驿站·登记小室",
        "prepared_opportunity_key": "s01-chapter-2",
        "prepared_opportunity_role": "alternate_approach",
    }
    scene = SimpleNamespace(
        location="白花碑驿站·风铃廊",
        pending_transition_location="",
        pending_transition_reason="",
        pending_transition_participants=[],
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    harness._scene_opportunity_for_act = lambda *_args, **_kwargs: pytest.fail(
        "已公开候选不应随新上下文重新选择"
    )
    spec = CampaignSessionSpec(number=1, title="迟响", arc="序章", gm_opening="", turns=[])

    required = harness._required_player_transition(spec, next_act=2)

    assert required == {
        "from_location": "白花碑驿站·风铃廊",
        "target_location": "白花碑驿站·登记小室",
        "prepared_opportunity_key": "s01-chapter-2",
        "prepared_opportunity_role": "alternate_approach",
    }


def test_lane_pressure_does_not_interrupt_shared_ritual_work() -> None:
    pressure = TwentySessionCampaignHarness._action_lane_pressure(
        [
            "伊莉雅以守护誓言推进仪式命刻。",
            "赛璃施放元素幕障，继续推进仪式。",
            "苍祈借奥灵的领域补上仪式的最后一环。",
        ]
    )

    assert pressure is None


def test_recent_public_gm_beat_defers_a_second_forced_refocus() -> None:
    recent = [
        {"label": "第01场行动 23 白河", "reply": "局面继续向前。"},
        {"label": "第01场GM主动节拍 27", "reply": "右廊尽头的门被人从里面推开。"},
    ]
    stale = [
        {"label": "第01场GM主动节拍 27", "reply": "右廊尽头的门被人从里面推开。"},
        {"label": "第01场行动 28 白河", "reply": "洛岚行动。"},
        {"label": "第01场行动 29 时雨", "reply": "艾薇娅行动。"},
    ]

    assert TwentySessionCampaignHarness._recent_public_gm_beat(recent, session_number=1)
    assert not TwentySessionCampaignHarness._recent_public_gm_beat(stale, session_number=1)
    assert TwentySessionCampaignHarness._recent_public_gm_beat(
        stale,
        session_number=1,
        max_player_actions=3,
    )


def test_blank_heartbeat_does_not_hide_the_previous_material_gm_beat() -> None:
    calls = [
        {"label": "第01场GM主动节拍 17", "reply": "会长打开登记小室，示意众人转移。"},
        {"label": "第01场玩家自由讨论 18", "reply": ""},
        {"label": "第01场GM主动节拍 19", "reply": ""},
    ]

    assert TwentySessionCampaignHarness._recent_public_gm_beat(
        calls,
        session_number=1,
        max_player_actions=3,
    )


def test_first_scene_opening_rejects_meta_acknowledgement() -> None:
    assert not TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "白花碑驿站的现场描述已经呈现给大家。接下来轮到你们决定。"
    )


def test_first_scene_opening_accepts_visible_situation() -> None:
    assert TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "白花碑驿站的门廊下，白花风铃无风自响。失名旅人坐在炉火旁，远处脚步声被雾潮吞没。"
    )


def test_first_scene_opening_need_not_repeat_an_established_place_name() -> None:
    assert TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "门廊下的白花风铃无风自响。失忆旅人退到闸门边，会长按住铜牌；"
        "旧路方向已有财团巡逻队的脚步声逼近。"
    )


def test_first_scene_opening_accepts_actionable_live_model_opening() -> None:
    assert TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "白花碑驿站的门廊下，白花风铃无风自响，铃身凝着潮盐，铃舌每一下都比回声慢半拍。"
        "失忆旅人听见第三声铃响，脸色骤白，低声说：‘这声音……我记得它在一条旧路上。’"
        "白花守望会会长站在风铃廊尽头，望向你们与旅人：‘旧路能不能开，先让我知道谁负责照看他；"
        "若财团巡逻出现，谁带他撤、在哪里集合、谁负责示警。你们给出当场能执行的安排，我就决定是否放行。’"
        "远处山道传来断续的铁蹄与车轮声，尚未进驿站，却正一点点靠近；会长的手停在风铃下，等着你们回答。"
    )


def test_first_scene_opening_accepts_changed_clue_without_forced_npc() -> None:
    assert TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "白花碑驿站的风铃廊下，铜制驿铃无风自响；潮盐凝在铃面，铃舌每次都比回声慢半拍。"
        "就在这一声迟响落下时，驿站公告板上一排原本清晰的姓名同时褪成空白，板缝里滑出一张薄薄的"
        "辉钢结算单，纸角压着‘灰晶病患·记忆’与一串待核价印记；远处荒道传来尚未抵达的车轮与金属铃声。"
        "伊莉雅、赛璃、洛岚、艾薇娅与苍祈都在廊下，通往下一处安全地点的旧路牌被那些空白姓名遮去一角，"
        "风铃又慢半拍地响了起来。"
    )


def test_first_scene_opening_accepts_observable_pressure_and_action_handoff() -> None:
    assert TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "雾潮从海岸漫上来，白花碑驿站就立在雾里，碑顶的白花风铃无风迟响。"
        "驿站里的人压低声音，有人在门缝后窥视；远处雾中，一点不属于驿站的冷白灯光忽明忽暗。"
        "石阶尽头挂着白花守望会的木牌。你们可以先去见守望会，也可以先观察驿站、寻找失忆旅人的踪迹。"
    )


def test_first_scene_opening_rejects_backstage_scene_outline() -> None:
    assert not TwentySessionCampaignHarness._is_substantive_first_scene_opening(
        "白花碑驿站的场景框架如下：在场人物是失忆旅人与会长；互动焦点是旧路是否放行，"
        "玩家可以调查巡逻队靠近的迹象。"
    )


def test_semantic_pending_npc_question_selects_its_addressed_player() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    frame_manager = SimpleNamespace(
        latest_pending_npc_question=lambda: {
            "npc": "本地巡守",
            "addressed_actor": "艾薇娅",
            "summary": "说明路线",
        }
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_frame_manager=frame_manager)
    )

    assert harness._preferred_npc_followup_speaker("阿凛") == "时雨"


def test_saturated_lane_does_not_force_gm_beat_over_pending_npc_question() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    frame_manager = SimpleNamespace(
        latest_pending_npc_question=lambda: {
            "npc": "未具名发问者",
            "addressed_actor": "",
            "summary": "说明路线",
        }
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_frame_manager=frame_manager)
    )
    spec = CampaignSessionSpec(
        number=1,
        title="雾中的核验",
        arc="序章",
        gm_opening="",
        turns=[],
    )

    assert harness._refocus_saturated_action_lane(
        spec,
        index=55,
        player_turn_count=42,
        last_signature="",
        last_refocus_turn=0,
    ) is None


def test_new_scene_opening_gets_two_player_actions_before_scheduled_beat() -> None:
    fresh = [
        {
            "label": "第01场场景2开场",
            "route": "/v1/game/scene-opening",
            "reply": "旧门在众人面前打开。",
        },
        {"label": "第01场玩家自由讨论 12", "reply": ""},
    ]
    one_action = [
        *fresh,
        {"label": "第01场行动 13 阿凛", "reply": "伊莉雅走进门内。"},
    ]
    two_actions = [
        *one_action,
        {"label": "第01场行动 14 南星", "reply": "赛璃观察门后的房间。"},
    ]

    assert TwentySessionCampaignHarness._recent_scene_opening_needs_player_space(
        fresh,
        session_number=1,
    )
    assert TwentySessionCampaignHarness._recent_scene_opening_needs_player_space(
        one_action,
        session_number=1,
    )
    assert not TwentySessionCampaignHarness._recent_scene_opening_needs_player_space(
        two_actions,
        session_number=1,
    )


def test_player_context_stops_at_scene_opening_without_leaking_private_brief() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.calls = [
        {
            "label": "第01场行动 24 时雨",
            "route": "/v1/message/route",
            "speaker": "时雨",
            "message": "艾薇娅贴住旧门框，听门外的脚步。",
            "reply": "门外的动静暂时停在雾里。",
        },
        {
            "label": "第01场场景3开场",
            "route": "/v1/game/scene-opening",
            "speaker": "时悠",
            "message": "私有场景简报：让财团在门外施压，但不要告诉玩家真相。",
            "reply": "旧路闸门外的风把白布吹得猎猎作响，门外有人停住了脚步。",
        },
        {
            "label": "第01场行动 32 澄砚",
            "route": "/v1/message/route",
            "speaker": "澄砚",
            "message": "苍祈先把白布边缘压稳，再望向门外。",
            "reply": "白布没有再被风卷起。",
        },
    ]

    current_scene = harness._recent_public_dialogue(limit=10)
    campaign_context = harness._recent_public_dialogue(limit=10, current_scene_only=False)

    assert "苍祈先把白布边缘压稳" in current_scene
    assert "旧路闸门外的风" in current_scene
    assert "艾薇娅贴住旧门框" not in current_scene
    assert "私有场景简报" not in current_scene
    assert "艾薇娅贴住旧门框" in campaign_context
    assert "私有场景简报" not in campaign_context


def test_player_context_includes_public_heartbeat_reply_only() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.calls = [
        {
            "route": "/v1/message/route",
            "speaker": "阿凛",
            "message": "伊莉雅观察门外。",
            "reply": "门外暂时没有人。",
        },
        {
            "route": "/v1/session/heartbeat",
            "speaker": "",
            "message": "内部主动节拍原因，不得公开",
            "reply": "黑色档案筒弹开，黄铜分路片落在门槛中央。",
        },
    ]

    context = harness._recent_public_dialogue(limit=10)

    assert "黄铜分路片落在门槛中央" in context
    assert "内部主动节拍原因" not in context


def test_resume_adds_public_scene_recap_only_when_current_opening_is_missing() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._in_progress_session_state = {"session_number": 1, "current_act": 3}
    harness.calls = [
        {
            "label": "第01场场景2开场",
            "route": "/v1/game/scene-opening",
            "reply": "登记小室里的风铃还在响。",
        }
    ]
    harness.common = {"campaign_id": "test", "session_id": "campaign-session-01"}
    captured: list[tuple[str, str, str, dict[str, object]]] = []
    harness.invoke = lambda label, method, route, payload: captured.append(  # type: ignore[method-assign]
        (label, method, route, dict(payload or {}))
    )
    spec = type("Spec", (), {"number": 1})()

    harness._restore_current_scene_public_context_if_needed(spec)

    assert len(captured) == 1
    label, method, route, payload = captured[0]
    assert label == "第01场场景3断点现场回顾"
    assert method == "POST"
    assert route == "/v1/game/scene-recap"
    assert payload["speaker"] == "时悠"

    harness.calls.append({"label": "第01场场景3开场", "route": "/v1/game/scene-opening"})
    harness._restore_current_scene_public_context_if_needed(spec)
    assert len(captured) == 1


def test_resume_skips_recap_when_latest_public_reply_already_names_live_location() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._in_progress_session_state = {"session_number": 1, "current_act": 2}
    harness.calls = [
        {
            "label": "第01场行动 29 南星",
            "route": "/v1/message/route",
            "reply": "赛璃与失忆旅人抵达白花碑驿站·登记小室。",
        }
    ]
    harness.common = {"campaign_id": "test", "session_id": "campaign-session-01"}
    harness._runtime = lambda: SimpleNamespace(  # type: ignore[method-assign]
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="白花碑驿站·登记小室")
            )
        )
    )
    captured: list[tuple[str, str, str, dict[str, object]]] = []
    harness.invoke = lambda label, method, route, payload: captured.append(  # type: ignore[method-assign]
        (label, method, route, dict(payload or {}))
    )
    spec = SimpleNamespace(number=1)

    harness._restore_current_scene_public_context_if_needed(spec)

    assert captured == []


def test_resume_skips_recap_when_recent_player_message_uses_live_location_short_name() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._in_progress_session_state = {"session_number": 1, "current_act": 2}
    harness.calls = [
        {
            "label": "第01场行动 37 白河",
            "route": "/v1/message/route",
            "message": "洛岚贴近登记小室的门缝观察巡逻灯影。",
            "reply": "门外暂时没有第二队灯影。",
        }
    ]
    harness.common = {"campaign_id": "test", "session_id": "campaign-session-01"}
    harness._runtime = lambda: SimpleNamespace(  # type: ignore[method-assign]
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(location="白花碑驿站·登记小室")
            )
        )
    )
    captured: list[tuple[str, str, str, dict[str, object]]] = []
    harness.invoke = lambda label, method, route, payload: captured.append(  # type: ignore[method-assign]
        (label, method, route, dict(payload or {}))
    )

    harness._restore_current_scene_public_context_if_needed(SimpleNamespace(number=1))

    assert captured == []


def test_setup_only_resume_stops_after_adventure_gate_is_ready() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = False
    harness._in_progress_session_state = {}
    harness._resume_completed_session = 0
    harness.campaign_id = "setup-only-resume"
    harness.campaign_root = "/tmp/setup-only-resume"
    harness.setup_only = True
    harness._setup_only_completed = False
    harness.run_astrbot_smoke = False
    harness._pacing_configure_kwargs = lambda: {}  # type: ignore[method-assign]
    harness._record_tool_event = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    harness._runtime = lambda: SimpleNamespace(  # type: ignore[method-assign]
        app=SimpleNamespace(
            campaign_pacing_manager=SimpleNamespace(configure=lambda **_kwargs: None)
        )
    )
    first_spec = SimpleNamespace(number=1)
    harness._campaign_sessions = lambda: [first_spec]  # type: ignore[method-assign]
    harness._ensure_adventure_started = lambda _spec: True  # type: ignore[method-assign]
    harness._restore_current_scene_public_context_if_needed = (  # type: ignore[method-assign]
        lambda _spec: (_ for _ in ()).throw(AssertionError("setup-only 不应进入场景恢复"))
    )
    harness._run_campaign_session = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        (_ for _ in ()).throw(AssertionError("setup-only 不应执行第一场"))
    )
    checkpoints: list[int] = []
    harness._write_campaign_checkpoint = checkpoints.append  # type: ignore[method-assign]

    harness._resume_main_flow()

    assert harness._setup_only_completed
    assert checkpoints == [0]


def test_astrbot_bridge_smoke_uses_an_isolated_probe_campaign(tmp_path) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.campaign_id = "main-campaign"
    harness.session_id = "session-zero"
    harness.channel_id = "main-channel"
    harness.run_root = tmp_path
    harness.service = FUGMHttpService(data_root=tmp_path / "campaigns", use_llm=False)
    harness.astrbot_bridge_results = []
    harness.errors = []
    harness._record_tool_event = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    main_runtime = harness.service._runtime(harness.campaign_id, auto_load=False)
    main_runtime.app.session_zero_manager.start(participants=["阿凛"])
    main_runtime.app.world_state.present_players = ["阿凛"]
    before = harness._astrbot_main_state_fingerprint()

    harness._run_astrbot_bridge_smoke("isolated-test")

    result = harness.astrbot_bridge_results[-1]
    assert result["ok"] is True
    assert result["main_campaign_unchanged"] is True
    assert result["probe_gate_closed"] is True
    assert result["probe_campaign_id"] != harness.campaign_id
    assert result["status"]["campaign_id"] == result["probe_campaign_id"]
    assert harness._astrbot_main_state_fingerprint() == before
    assert [participant.name for participant in main_runtime.app.session_zero_manager.state.participants] == ["阿凛"]
    assert harness.errors == []


def test_tool_receipt_audit_distinguishes_recovered_rejection_from_agent_failure() -> None:
    recovered = {
        "index": 1,
        "label": "已恢复",
        "body": {
            "agent_error": "",
            "tool_receipts": [{"tool_name": "start_scene", "ok": False}],
        },
    }
    unresolved = {
        "index": 2,
        "label": "未恢复",
        "body": {
            "agent_error": "工具循环达到上限",
            "tool_receipts": [{"tool_name": "update_hero", "ok": False}],
        },
    }
    corrected = {
        "index": 3,
        "label": "纠正后写入",
        "body": {
            "agent_error": "最终表达格式错误",
            "tool_receipts": [
                {"tool_name": "错误工具", "ok": False},
                {"tool_name": "正确工具", "ok": True, "state_changed": True},
            ],
        },
    }

    calls = [recovered, unresolved, corrected]
    failures = TwentySessionCampaignHarness._failed_tool_receipts(calls)
    unrecovered = TwentySessionCampaignHarness._unrecovered_tool_failure_calls(
        calls
    )
    agent_errors = TwentySessionCampaignHarness._agent_error_calls(calls)
    recovered_agent_errors = TwentySessionCampaignHarness._recovered_agent_error_calls(calls)

    assert len(failures) == 3
    assert [item["label"] for item in unrecovered] == ["未恢复"]
    assert [item["label"] for item in agent_errors] == ["未恢复"]
    assert [item["label"] for item in recovered_agent_errors] == ["纠正后写入"]


def test_provider_timeout_detection_accepts_html_502_chinese_timeout() -> None:
    error = RuntimeError("LLM HTTP 502: <title>网站请求超时</title>")

    assert TwentySessionCampaignHarness._is_provider_unavailable_exception(error)


def test_service_retry_allows_provider_failure_after_only_rejected_receipts(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "1")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "1")
    body = {
        "route": "gm_agent_tool",
        "agent_error": "LLM HTTP 502: <title>网站请求超时</title>",
        "reply": "这个行动还没有结算。",
        "tool_receipts": [
            {
                "tool_name": "resolve_rule_window",
                "ok": False,
                "state_changed": False,
                "error_code": "RULE_ACTION_REJECTED",
            }
        ],
    }

    delay = harness._service_retry_delay_seconds(
        label="待决回应",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body=body,
        attempt=1,
    )

    assert delay == 1


def test_explicit_npc_identity_check_returns_turn_to_named_hero() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    frame_manager = SimpleNamespace(
        latest_pending_npc_question=lambda: {
            "npc": "白花守望会会长",
            "addressed_actor": "伊莉雅",
            "summary": "说明姓名、关系与是否代答",
        }
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_frame_manager=frame_manager)
    )

    assert harness._preferred_npc_followup_speaker("南星") == "阿凛"


def test_ordinary_npc_narration_keeps_player_rotation() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    frame_manager = SimpleNamespace(latest_pending_npc_question=lambda: None)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_frame_manager=frame_manager)
    )

    assert harness._preferred_npc_followup_speaker("南星") == "南星"


def test_personally_assigned_open_condition_returns_slot_to_that_hero() -> None:
    conditions = [
        {
            "npc": "白花守望会会长",
            "condition": "伊莉雅当面说明失名旅人的具体去向，并以自己的名义承担护送责任。",
            "status": "open",
        }
    ]

    assert (
        TwentySessionCampaignHarness._speaker_for_personal_condition("白河", conditions)
        == "阿凛"
    )


def test_unassigned_open_condition_keeps_player_rotation() -> None:
    conditions = [
        {
            "npc": "白花守望会会长",
            "condition": "队伍说明去向，并由一名英雄承担护送责任。",
            "status": "open",
        }
    ]

    assert (
        TwentySessionCampaignHarness._speaker_for_personal_condition("白河", conditions)
        == "白河"
    )


def test_strict_npc_route_audit_rejects_gm_speaking_for_a_pc() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    record = {
        "pipeline_span": {
            "npc_dialogue": {
                "routed_target": "失名旅人",
                "actual_targets": ["赛璃", "失忆旅人"],
                "memory_targets": ["赛璃", "失忆旅人"],
                "player_character_targets": ["赛璃"],
            }
        }
    }

    with pytest.raises(RuntimeError, match="代替玩家角色开口"):
        harness._assert_npc_route_integrity("旅人问答", record)


def test_strict_npc_route_audit_rejects_memory_written_to_another_npc() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    record = {
        "pipeline_span": {
            "npc_dialogue": {
                "routed_target": "失名旅人",
                "actual_targets": ["失忆旅人"],
                "memory_targets": ["白花守望会会长"],
                "player_character_targets": [],
            }
        }
    }

    with pytest.raises(RuntimeError, match="记忆写入者"):
        harness._assert_npc_route_integrity("旅人问答", record)


def test_strict_npc_route_audit_accepts_locked_traveller_aliases() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)

    class WorldState:
        @staticmethod
        def resolve_npc_name(value: str) -> str:
            return "失忆旅人" if value in {"失名旅人", "失忆旅人"} else ""

    class App:
        world_state = WorldState()

    harness.service = type(
        "Service",
        (),
        {"_runtime": lambda self, _campaign_id: type("Runtime", (), {"app": App()})()},
    )()
    harness.campaign_id = "alias-test"
    record = {
        "pipeline_span": {
            "npc_dialogue": {
                "routed_target": "失名旅人",
                "actual_targets": ["失忆旅人"],
                "memory_targets": ["失忆旅人"],
                "player_character_targets": [],
            }
        }
    }

    harness._assert_npc_route_integrity("旅人问答", record)


def test_strict_longrun_rejects_unknown_for_npc_cross_scene_choice() -> None:
    record = {
        "reply": "这件事我不知道。\n【财团巡逻队逼近】0/8",
        "body": {
            "decision": {
                "movement_scope": "cross_scene",
                "npc_reply_required": True,
                "movement_companions": ["失名旅人", "本地巡守"],
            }
        },
    }

    with pytest.raises(RuntimeError, match="自己的同行或移动选择"):
        TwentySessionCampaignHarness._assert_npc_movement_response_integrity(
            "护送旅人",
            record,
        )


def test_strict_longrun_allows_explicit_npc_movement_refusal() -> None:
    record = {
        "reply": "我现在不能跟你们走。",
        "body": {
            "decision": {
                "movement_scope": "cross_scene",
                "npc_reply_required": True,
                "movement_companions": ["失名旅人"],
            }
        },
    }

    TwentySessionCampaignHarness._assert_npc_movement_response_integrity(
        "护送旅人",
        record,
    )


def test_strict_longrun_rejects_generic_unknown_from_local_guide() -> None:
    record = {
        "message": "旧路前方最近的遮蔽处在哪里？",
        "reply": "这件事我不知道。\n【财团巡逻队逼近】0/8",
        "body": {
            "decision": {
                "npc_reply_required": True,
                "npc_target": "前方的守巡",
            }
        },
    }

    with pytest.raises(RuntimeError, match="职责内的普通路线问题"):
        TwentySessionCampaignHarness._assert_local_guide_response_integrity(
            "询问旧路",
            record,
        )


def test_strict_longrun_allows_guide_uncertainty_about_enemy_intelligence() -> None:
    record = {
        "message": "财团巡逻队会不会认出这盏引路白灯？",
        "reply": "会外还有谁认得，我不知道。",
        "body": {
            "decision": {
                "npc_reply_required": True,
                "npc_target": "白花守望会守巡",
            }
        },
    }

    TwentySessionCampaignHarness._assert_local_guide_response_integrity(
        "询问敌情",
        record,
    )


def test_strict_longrun_rejects_completed_transfer_without_source_fact() -> None:
    record = {
        "message": "艾薇娅伸手示意巡守接过薄牌。",
        "body": {
            "decision": {
                "performed_action": True,
                "action_semantics_required": True,
                "action_semantics_reviewed": True,
                "object_transfer_status": "completed",
                "action_facts": [
                    {
                        "evidence": "艾薇娅伸手示意巡守接过薄牌",
                        "kind": "transfer",
                        "stage": "offered",
                        "requires_external_acceptance": True,
                        "can_commit_world_fact": False,
                    }
                ],
            }
        },
    }

    with pytest.raises(RuntimeError, match="物件交接没有完成证据"):
        TwentySessionCampaignHarness._assert_action_fact_integrity(
            "递交诱饵",
            record,
        )


def test_strict_longrun_accepts_evidence_bound_offered_transfer() -> None:
    record = {
        "message": "艾薇娅伸手示意巡守接过薄牌。",
        "body": {
            "decision": {
                "performed_action": True,
                "action_semantics_required": True,
                "action_semantics_reviewed": True,
                "object_transfer_status": "offered",
                "action_summary": "艾薇娅伸手示意巡守接过薄牌",
                "action_facts": [
                    {
                        "evidence": "艾薇娅伸手示意巡守接过薄牌",
                        "kind": "transfer",
                        "stage": "offered",
                        "requires_external_acceptance": True,
                        "can_commit_world_fact": False,
                    }
                ],
            }
        },
    }

    TwentySessionCampaignHarness._assert_action_fact_integrity(
        "递交诱饵",
        record,
    )


def test_authoritative_resolution_ends_after_one_player_owned_aftermath() -> None:
    assert TwentySessionCampaignHarness._session_has_earned_fictional_ending(
        current_act=4,
        turns_in_closure=1,
        pacing_can_end=False,
        authoritative_resolution=True,
        memory_anchor_complete=True,
        pending_blocking_decisions=0,
    )


def test_fictional_ending_waits_for_aftermath_memory_and_pending_choices() -> None:
    common = {
        "current_act": 4,
        "pacing_can_end": False,
        "authoritative_resolution": True,
        "memory_anchor_complete": True,
        "pending_blocking_decisions": 0,
    }

    assert not TwentySessionCampaignHarness._session_has_earned_fictional_ending(
        **common,
        turns_in_closure=0,
    )
    assert not TwentySessionCampaignHarness._session_has_earned_fictional_ending(
        **{**common, "memory_anchor_complete": False},
        turns_in_closure=1,
    )
    assert not TwentySessionCampaignHarness._session_has_earned_fictional_ending(
        **{**common, "pending_blocking_decisions": 1},
        turns_in_closure=1,
    )
def test_safe_pass_expects_human_like_gm_silence() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._safe_pass_will_publish_clock_change = lambda _speaker: False
    assert harness._player_route_expectation(
        "exhaustion_safe_pass",
        speaker="南星",
    ) == ("silent", False)
    harness._safe_pass_will_publish_clock_change = lambda _speaker: True
    assert harness._player_route_expectation(
        "exhaustion_safe_pass",
        speaker="南星",
    ) == ("fu_gm", True)
    assert harness._player_route_expectation("") == (
        "fu_gm",
        True,
    )
