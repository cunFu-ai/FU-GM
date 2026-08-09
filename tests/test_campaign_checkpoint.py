import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from fu_gm.testing.campaign_checkpoint import CampaignRunCheckpoint
from fu_gm.testing.conversation_quality import ConversationQualityAuditor
from fu_gm.llm_client import LLMHTTPError
from fu_gm.components.session_closure_policy import SessionClosurePolicy
from fu_gm.testing.session_progress_evaluator import SessionProgressAssessment
from fu_gm.models import SceneRecord, SceneType, SessionSceneOpportunity
from scripts.run_20_session_campaign_test import (
    CampaignSessionSpec,
    TwentySessionCampaignHarness,
)
from scripts.run_ultra_from_scratch_campaign_test import FromScratchUltraHarness


def test_harness_requires_authoritative_resolution_before_opening_aftermath() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    episode = SimpleNamespace(
        local_question_resolved=False,
        deliberate_cliffhanger=False,
        concrete_consequences=["失名旅人已经说出一小段方向感。"],
        opposition_moves=["财团使者提出交换条件。"],
    )
    app = SimpleNamespace(
        story_arc_manager=SimpleNamespace(
            state=SimpleNamespace(current_session_progress=episode)
        ),
        scene_frame_manager=SimpleNamespace(current_frame=None),
        campaign_pacing_manager=SimpleNamespace(closure_policy=SessionClosurePolicy()),
        interceptor=SimpleNamespace(
            decision_window_manager=SimpleNamespace(pending=lambda **_kwargs: [])
        ),
    )
    harness._runtime = lambda: SimpleNamespace(app=app)
    harness._latest_world_action_is_unanswered = lambda: False
    transitions: list[int] = []
    harness._transition_session_scene = (
        lambda _spec, act, **_kwargs: transitions.append(act)
    )
    harness._record_tool_event = lambda *_args, **_kwargs: None
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])
    assessment = SessionProgressAssessment(
        stage="climax",
        local_question_changed=True,
        local_question_resolved=True,
        reversal_reached=True,
        concrete_consequence=True,
        npc_answer_complete=True,
        opposition_move_present=True,
        local_payoff_present=True,
    )

    assert harness._advance_session_act_if_earned(spec, 3, assessment, turns_in_act=6) == 3
    assert transitions == []

    episode.local_question_resolved = True
    assert harness._advance_session_act_if_earned(spec, 3, assessment, turns_in_act=6) == 4
    assert transitions == [4]


def test_harness_offers_route_before_opening_a_different_location() -> None:
    """Prepared act locations must not teleport the party during a long test."""

    harness = object.__new__(TwentySessionCampaignHarness)
    scene = SimpleNamespace(
        location="白花碑驿站·风铃廊",
        pending_transition_location="",
        pending_transition_reason="",
        pending_transition_participants=[],
    )
    episode = SimpleNamespace(
        local_question_resolved=False,
        deliberate_cliffhanger=False,
        concrete_consequences=["会长允许英雄决定是否走旧路。"],
        opposition_moves=["财团的巡逻声已经传到驿站门口。"],
        scene_progress={},
        active_scene_id="",
    )
    app = SimpleNamespace(
        scene_manager=SimpleNamespace(current_scene=scene),
        story_arc_manager=SimpleNamespace(
            state=SimpleNamespace(current_session_progress=episode)
        ),
        scene_frame_manager=SimpleNamespace(current_frame=None),
        campaign_pacing_manager=SimpleNamespace(closure_policy=SessionClosurePolicy()),
        interceptor=SimpleNamespace(
            decision_window_manager=SimpleNamespace(pending=lambda **_kwargs: [])
        ),
    )
    harness._runtime = lambda: SimpleNamespace(app=app)
    harness._latest_world_action_is_unanswered = lambda: False
    harness._pending_scene_transition = {}
    harness._scene_opportunity_for_act = lambda *_args, **_kwargs: SimpleNamespace(
        location="白花碑驿站·登记小室"
    )
    beats: list[str] = []
    harness._session_gm_beat = lambda _spec, _index, reason: (
        beats.append(reason) or {"reply": "会长让开去登记小室的门。", "send_reply": True}
    )
    harness._record_tool_event = lambda *_args, **_kwargs: None
    transitions: list[int] = []
    harness._transition_session_scene = (
        lambda _spec, act, **_kwargs: transitions.append(act)
    )
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])
    assessment = SessionProgressAssessment(
        stage="development",
        local_question_changed=True,
        concrete_consequence=True,
        npc_answer_complete=True,
        opposition_move_present=True,
        local_payoff_present=True,
    )

    assert harness._advance_session_act_if_earned(spec, 1, assessment, turns_in_act=5) == 1
    assert transitions == []
    assert len(beats) == 1
    assert harness._pending_scene_transition["target_location"] == "白花碑驿站·登记小室"

    # Once a real player-owned move has established an anchor, the same act
    # transition may open the prepared camera.
    scene.pending_transition_location = "白花碑驿站·登记小室"
    scene.pending_transition_reason = "伊莉雅带旅人前往登记小室"
    scene.pending_transition_participants = ["伊莉雅", "失忆旅人"]
    assert harness._advance_session_act_if_earned(spec, 1, assessment, turns_in_act=5) == 2
    assert transitions == [2]


def test_harness_treats_continued_actions_as_an_in_place_choice() -> None:
    """A public route cannot become an endlessly repeated GM instruction."""

    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 1,
        "next_act": 2,
        "target_location": "白花碑驿站·登记小室",
        "public_target_announced": True,
        "offered_at_turn_in_act": 4,
    }
    harness._required_player_transition = lambda *_args, **_kwargs: {
        "from_location": "白花碑驿站·风铃廊",
        "target_location": "白花碑驿站·登记小室",
    }
    harness._record_tool_event = lambda *_args, **_kwargs: None
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])

    assert not harness._offer_player_led_scene_transition(
        spec,
        current_act=1,
        next_act=2,
        assessment=SessionProgressAssessment(),
        turns_in_act=6,
    )
    assert harness._pending_scene_transition["continue_in_place"] is True


def test_in_place_scene_keeps_function_but_not_private_prepared_location() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._scene_opportunity_for_act = lambda *_args, **_kwargs: SessionSceneOpportunity(
        scene_key="s01-private-room",
        scene_role="social_or_investigation",
        title="登记小室的核验",
        location="白花碑驿站·登记小室",
        situation="登记小室里有一张需要核验的名册。",
        purpose="在登记小室判断谁伪造了名册。",
    )
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])

    opportunity = harness._in_place_scene_opportunity(
        spec,
        2,
        location="白花碑驿站·风铃廊",
    )

    assert opportunity.scene_key == "s01-private-room"
    assert opportunity.scene_role == "social_or_investigation"
    assert opportunity.location == "白花碑驿站·风铃廊"
    assert "登记小室" not in opportunity.title
    assert "登记小室" not in opportunity.situation
    assert "登记小室" not in opportunity.purpose


def test_in_place_transition_opens_a_new_camera_without_moving_party() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.pc_names = ["伊莉雅", "赛璃"]
    harness.common = {}
    current_scene = SceneRecord(
        "第01场·场景1：风铃廊问路",
        SceneType.STANDARD,
        location="白花碑驿站·风铃廊",
    )
    captured: dict[str, object] = {}

    def start_scene(name, scene_type, **kwargs):
        captured["name"] = name
        captured["scene_type"] = scene_type
        captured.update(kwargs)

    app = SimpleNamespace(
        scene_manager=SimpleNamespace(current_scene=current_scene),
        start_scene=start_scene,
    )
    harness._runtime = lambda: SimpleNamespace(app=app)
    harness._close_active_play_scene = lambda _summary: None
    harness._scene_opportunity_for_act = lambda *_args, **_kwargs: SessionSceneOpportunity(
        scene_key="s01-alternate",
        scene_role="alternate_approach",
        title="登记小室的另一条路",
        location="白花碑驿站·登记小室",
        situation="登记小室里出现一张伪造名册。",
        purpose="在登记小室核验名册。",
    )
    harness._act_opening_prompt = lambda *_args, **_kwargs: "只描述当前现场。"
    harness._verified_session_results = lambda: []
    harness._record_tool_event = lambda *_args, **_kwargs: None
    harness.invoke = lambda *_args, **_kwargs: {"ok": True}
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])

    harness._transition_session_scene(spec, 2, in_place=True)

    assert captured["location"] == "白花碑驿站·风铃廊"
    assert captured["session_opportunity_role"] == "alternate_approach"
    assert captured["session_opportunity_key"] == "s01-alternate"
    assert "登记小室" not in str(captured["summary"])


def test_player_transition_instruction_hides_unannounced_prepared_location() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 1,
        "next_act": 2,
        "target_location": "白花碑驿站·登记小室",
        "public_target_announced": False,
    }
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])

    instruction = harness._player_transition_instruction(spec, current_act=1)

    assert instruction == ""
    assert "登记小室" not in instruction


def test_unannounced_transition_offer_retries_after_one_player_turn() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 2,
        "next_act": 3,
        "target_location": "白花碑驿站·旧路闸门",
        "public_target_announced": False,
        "offered_at_turn_in_act": 20,
    }
    calls: list[dict[str, object]] = []

    def offer(_spec, **kwargs):
        calls.append(dict(kwargs))
        return True

    harness._offer_player_led_scene_transition = offer
    spec = CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", [])
    assessment = SessionProgressAssessment(stage="climax")

    assert not harness._retry_unannounced_scene_transition(
        spec,
        current_act=2,
        assessment=assessment,
        turns_in_act=20,
    )
    assert harness._retry_unannounced_scene_transition(
        spec,
        current_act=2,
        assessment=assessment,
        turns_in_act=21,
    )
    assert calls == [
        {
            "current_act": 2,
            "next_act": 3,
            "assessment": assessment,
            "turns_in_act": 21,
        }
    ]


def test_transition_target_must_be_named_in_the_public_gm_reply() -> None:
    checker = TwentySessionCampaignHarness._public_reply_names_transition_target

    assert checker("会长让开通往登记小室的内门。", "白花碑驿站·登记小室")
    assert not checker("会长朝里面让开一步，示意你们自己决定。", "白花碑驿站·登记小室")


def test_transition_publication_is_detectable_as_a_player_handoff() -> None:
    before = {
        "target_location": "白花碑驿站·旧路闸门",
        "public_target_announced": False,
    }
    after = {
        **before,
        "public_target_announced": True,
        "public_route_evidence": "旧路闸门就在左侧石基尽头。",
    }

    assert TwentySessionCampaignHarness._transition_offer_became_public(before, after)
    assert not TwentySessionCampaignHarness._transition_offer_became_public(after, after)


def test_aftermath_prompt_distinguishes_goals_from_verified_results() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    spec = CampaignSessionSpec(
        1,
        "白花碑驿站的迟响",
        "第一幕",
        "",
        [],
        expected_focus=["取得旧路", "建立财团巡逻压力", "揭示风铃线索"],
    )
    opportunity = SimpleNamespace(
        situation="同一地点因英雄选择呈现新的状态",
        purpose="兑现局部结果",
    )

    prompt = harness._act_opening_prompt(
        spec,
        4,
        opportunity=opportunity,
        resolved_location="白花碑驿站",
        assessment=SessionProgressAssessment(
            stage="climax",
            unresolved_now="财团使者的交换条件仍未回应。",
        ),
        verified_results=["失名旅人已经说出一小段方向感。"],
    )

    assert "这些目标不代表已经完成" in prompt
    assert "实录已经兑现的结果只有：失名旅人已经说出一小段方向感" in prompt
    assert "让取得旧路、建立财团巡逻压力、揭示风铃线索的局部结果落地" not in prompt


def test_campaign_checkpoint_round_trip_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    checkpoint = CampaignRunCheckpoint(
        target_sessions=20,
        campaign_id="白钟大陆",
        completed_session=7,
        state={"previous_summary": "旧钟恢复了名字。", "upgrade_cursors": {"伊莉雅": 2}},
    )

    checkpoint.save(path)
    loaded = CampaignRunCheckpoint.load(path)

    assert loaded.target_sessions == 20
    assert loaded.campaign_id == "白钟大陆"
    assert loaded.completed_session == 7
    assert loaded.state["upgrade_cursors"]["伊莉雅"] == 2
    assert not path.with_suffix(".json.tmp").exists()


def test_campaign_checkpoint_restores_independent_working_copies(tmp_path: Path) -> None:
    source = tmp_path / ".checkpoints" / "session_00" / "campaign"
    source.mkdir(parents=True)
    (source / "snapshot.json").write_text('{"scene": "Session 0"}', encoding="utf-8")
    digest = CampaignRunCheckpoint.directory_digest(source)
    checkpoint = CampaignRunCheckpoint(
        target_sessions=20,
        campaign_id="白钟大陆",
        state={
            "campaign_backup": ".checkpoints/session_00/campaign",
            "campaign_backup_sha256": digest,
        },
    )

    first = checkpoint.restore_campaign_copy(tmp_path, tmp_path / ".resume" / "first" / "白钟大陆")
    (first / "snapshot.json").write_text('{"scene": "第一场"}', encoding="utf-8")
    second = checkpoint.restore_campaign_copy(tmp_path, tmp_path / ".resume" / "second" / "白钟大陆")

    assert (source / "snapshot.json").read_text(encoding="utf-8") == '{"scene": "Session 0"}'
    assert (second / "snapshot.json").read_text(encoding="utf-8") == '{"scene": "Session 0"}'
    assert CampaignRunCheckpoint.directory_digest(source) == digest


def test_campaign_checkpoint_resolves_run_root_and_immutable_bundle_sources(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / ".checkpoints" / "turn_01_004"
    campaign = bundle / "campaign"
    campaign.mkdir(parents=True)
    (campaign / "snapshot.json").write_text('{"step": 4}', encoding="utf-8")
    checkpoint = CampaignRunCheckpoint(
        target_sessions=20,
        campaign_id="白钟大陆",
        state={
            "campaign_backup": ".checkpoints/turn_01_004/campaign",
            "campaign_backup_sha256": CampaignRunCheckpoint.directory_digest(campaign),
            "in_progress_session": {"scripted_next_index": 4},
        },
    )
    checkpoint.save(bundle / CampaignRunCheckpoint.FILENAME)
    checkpoint.save(tmp_path / CampaignRunCheckpoint.FILENAME)

    for source in (
        tmp_path,
        bundle,
        bundle / CampaignRunCheckpoint.FILENAME,
    ):
        run_root, checkpoint_path, loaded = CampaignRunCheckpoint.load_resume_source(source)
        assert run_root == tmp_path.resolve()
        assert checkpoint_path.is_file()
        assert loaded.state["in_progress_session"]["scripted_next_index"] == 4
        assert loaded.resolve_campaign_backup(run_root) == campaign.resolve()


def test_campaign_checkpoint_rejects_writable_resume_source(tmp_path: Path) -> None:
    source = tmp_path / ".resume" / "old" / "campaign"
    source.mkdir(parents=True)
    checkpoint = CampaignRunCheckpoint(
        target_sessions=20,
        campaign_id="白钟大陆",
        state={"campaign_backup": ".resume/old/campaign"},
    )

    with pytest.raises(ValueError, match=r"只能从 \.checkpoints 恢复"):
        checkpoint.restore_campaign_copy(tmp_path, tmp_path / ".resume" / "new" / "白钟大陆")


def test_harness_keeps_recent_in_progress_bundles_and_clears_them_after_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FU_GM_LONG_TEST_CHECKPOINT_HISTORY", "3")
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.run_root = tmp_path
    harness.checkpoint_root = tmp_path / ".checkpoints"
    harness.checkpoint_path = tmp_path / "campaign_checkpoint.json"
    harness.campaign_root = tmp_path / "campaigns"
    harness.campaign_id = "白钟大陆"
    harness.target_sessions = 20
    harness.calls = []
    harness.notes = []
    harness.errors = []
    harness.tool_events = []
    harness.session_reports = []
    harness.astrbot_bridge_results = []
    harness.heartbeat_results = []
    harness.session_table_metrics = {}
    harness.session_scene_metrics = {}
    harness.player_simulation_metrics = []
    harness.session_progress_assessments = {}
    harness.session_completion_results = {}
    harness.level_up_results = []
    harness._previous_session_summary = ""
    harness._adventure_started = True
    harness._upgrade_cursors = {}
    harness._in_progress_session_state = {}
    harness.conversation_path = tmp_path / "conversation.txt"
    harness.conversation_path.write_text("", encoding="utf-8")
    campaign = harness.campaign_root / harness.campaign_id
    campaign.mkdir(parents=True)
    (campaign / "snapshot.json").write_text('{"step": 1}', encoding="utf-8")

    first_state = {
        "session_number": 1,
        "phase": "scripted",
        "scripted_next_index": 4,
    }
    harness._write_campaign_checkpoint(0, in_progress_state=first_state)
    first = CampaignRunCheckpoint.load(harness.checkpoint_path)
    first_bundle = next(harness.checkpoint_root.glob("turn_01_*"))
    bundled_first = CampaignRunCheckpoint.load(
        first_bundle / CampaignRunCheckpoint.FILENAME
    )

    assert first.completed_session == 0
    assert first.state["in_progress_session"]["scripted_next_index"] == 4
    assert bundled_first.state == first.state
    assert len(list(harness.checkpoint_root.glob("turn_01_*"))) == 1

    (campaign / "snapshot.json").write_text('{"step": 2}', encoding="utf-8")
    harness._write_campaign_checkpoint(
        0,
        in_progress_state={**first_state, "scripted_next_index": 5},
    )
    second = CampaignRunCheckpoint.load(harness.checkpoint_path)

    assert second.state["in_progress_session"]["scripted_next_index"] == 5
    assert len(list(harness.checkpoint_root.glob("turn_01_*"))) == 2

    (campaign / "snapshot.json").write_text('{"step": 3}', encoding="utf-8")
    harness._write_campaign_checkpoint(
        0,
        in_progress_state={**first_state, "scripted_next_index": 6},
    )
    (campaign / "snapshot.json").write_text('{"step": 4}', encoding="utf-8")
    harness._write_campaign_checkpoint(
        0,
        in_progress_state={**first_state, "scripted_next_index": 7},
    )

    retained = list(harness.checkpoint_root.glob("turn_01_*"))
    assert len(retained) == 3
    retained_steps = {
        json.loads((bundle / "campaign" / "snapshot.json").read_text(encoding="utf-8"))["step"]
        for bundle in retained
    }
    assert retained_steps == {2, 3, 4}

    harness._write_campaign_checkpoint(1)
    completed = CampaignRunCheckpoint.load(harness.checkpoint_path)

    assert completed.completed_session == 1
    assert completed.state["in_progress_session"] == {}
    assert list(harness.checkpoint_root.glob("turn_01_*")) == []


def test_in_progress_checkpoint_preserves_exact_pending_table_event() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {}
    captured: list[dict[str, object]] = []
    harness._write_campaign_checkpoint = (
        lambda _completed, **kwargs: captured.append(
            dict(kwargs.get("in_progress_state") or {})
        )
    )
    pending = {
        "phase": "scripted",
        "kind": "player_action",
        "index": 19,
        "speaker": "澄砚",
        "message": "苍祈故意暴露身影，把巡逻队注意力引开。",
        "fallback_kind": "",
    }

    harness._write_in_progress_session_checkpoint(
        CampaignSessionSpec(1, "白花碑驿站的迟响", "第一幕", "", []),
        phase="scripted",
        scripted_next_index=19,
        continuation_index=20,
        session_start_call_count=0,
        scene_history_start=0,
        resource_before={},
        gm_beat_count=1,
        player_turn_count=12,
        routed_discussion_count=2,
        processed_player_turns=12,
        current_act=2,
        act_started_at_turn=8,
        last_assessment_turn=12,
        last_extension_gm_beat_turn=9,
        last_lane_refocus_signature="",
        last_lane_refocus_turn=-100,
        assessment=SessionProgressAssessment(),
        pending_table_event=pending,
    )

    assert captured[0]["scripted_next_index"] == 19
    assert captured[0]["pending_table_event"] == pending


def test_single_session_pilot_keeps_twenty_session_pacing_horizon_on_resume() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.length_profile = "short"
    harness.campaign_profile_sessions = 20
    harness.target_arcs = 4

    assert harness._pacing_configure_kwargs() == {
        "length": "short",
        "target_sessions": 20,
        "target_arcs": 4,
    }


def test_longrun_accepts_ready_world_map_when_artifact_exists(tmp_path: Path) -> None:
    image = tmp_path / "world.png"
    image.write_bytes(b"png")

    assert TwentySessionCampaignHarness._world_map_artifact_ready(
        {"status": "ready", "output_path": str(image)}
    )
    assert TwentySessionCampaignHarness._world_map_artifact_ready(
        {"status": "generated", "output_path": str(image)}
    )
    assert not TwentySessionCampaignHarness._world_map_artifact_ready(
        {"status": "ready", "output_path": str(tmp_path / "missing.png")}
    )


def test_strict_longrun_incremental_gate_stops_on_backstage_instruction() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.conversation_quality_auditor = ConversationQualityAuditor()
    harness.calls = [
        {
            "label": "第01场行动 01",
            "reply": "只描述眼前真实可见的阻挡，不要替角色改做其他行动。",
        }
    ]

    with pytest.raises(RuntimeError, match="增量质量门禁.*后台指令泄露"):
        harness._assert_incremental_conversation_quality("第01场行动 01")


def test_strict_longrun_incremental_gate_reports_action_lane_loops_only_at_end() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.conversation_quality_auditor = ConversationQualityAuditor()
    harness.calls = [
        {"label": "玩家行动 01", "message": "伊莉雅询问旅人记得什么。", "reply": "他记得两位数。"},
        {"label": "玩家行动 02", "message": "洛岚继续询问旅人记得什么。", "reply": "他记得一道门。"},
        {"label": "玩家行动 03", "message": "赛璃又询问旅人记得什么。", "reply": "他记得冷光。"},
        {"label": "玩家行动 04", "message": "苍祈再次询问旅人记得什么。", "reply": "他记得巡逻印记。"},
    ]

    report = harness.conversation_quality_auditor.audit(harness.calls)
    assert report.repeated_player_action_lanes >= 1
    harness._assert_incremental_conversation_quality("玩家行动 04")


def test_strict_longrun_stops_on_player_facing_llm_unavailable(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.calls = []
    harness.notes = []

    def fake_base_invoke(self, label, method, route, payload=None):
        self.calls.append(
            {
                "label": label,
                "status": 200,
                "llm_diagnostics": {
                    "core_gm": {"error": "deadline exceeded"},
                    "expressor": {},
                },
            }
        )
        return {
            "ok": True,
            "reply": "刚才那句我没听清，你再说一遍？",
            "llm_unavailable": True,
            "error": "LLMActionBrain failed and heuristic fallback is disabled.",
        }

    monkeypatch.setattr(TwentySessionCampaignHarness.__mro__[1], "invoke", fake_base_invoke)

    with pytest.raises(RuntimeError, match="模型链路不可用"):
        harness.invoke("第01场行动 01", "POST", "/v1/message/route", {})

    assert harness.calls[-1]["strict_semantic_failure"]["kind"] == "llm_unavailable"


def test_strict_longrun_reports_invalid_model_json_separately(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.calls = []
    harness.notes = []

    def fake_base_invoke(self, label, method, route, payload=None):
        self.calls.append(
            {
                "label": label,
                "status": 200,
                "llm_diagnostics": {
                    "core_gm": {
                        "error": "NPC decision returned invalid JSON",
                        "error_kind": "invalid_json",
                    },
                    "npc_transaction": {"error_kind": "invalid_json"},
                },
            }
        )
        return {
            "ok": True,
            "reply": "刚才那句我没听清，你再说一遍？",
            "llm_invalid_output": True,
            "llm_failure_kind": "invalid_json",
            "error": "NPC decision returned invalid JSON",
        }

    monkeypatch.setattr(TwentySessionCampaignHarness.__mro__[1], "invoke", fake_base_invoke)

    with pytest.raises(RuntimeError, match="模型结构化输出无效"):
        harness.invoke("第01场行动 01", "POST", "/v1/message/route", {})

    failure = harness.calls[-1]["strict_semantic_failure"]
    assert failure["kind"] == "model_invalid_output"
    assert failure["failure_kind"] == "invalid_json"


def test_strict_longrun_does_not_count_fail_closed_silence_as_semantic_pass(
    monkeypatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.calls = []
    harness.notes = []

    def fake_base_invoke(self, label, method, route, payload=None):
        self.calls.append({"label": label, "status": 200})
        return {
            "ok": True,
            "route": "gm_agent_unavailable_silent",
            "target": "silent",
            "send_reply": False,
            "reply": "",
            "agent_error": (
                'LLM HTTP 403: {"code":"INSUFFICIENT_BALANCE",'
                '"message":"Insufficient account balance"}'
            ),
            "tool_receipts": [],
        }

    monkeypatch.setattr(
        TwentySessionCampaignHarness.__mro__[1], "invoke", fake_base_invoke
    )

    with pytest.raises(RuntimeError, match="GM智能体链路不可用"):
        harness.invoke("玩家自由讨论", "POST", "/v1/message/route", {})

    failure = harness.calls[-1]["strict_semantic_failure"]
    assert failure["kind"] == "gm_agent_unavailable"
    assert "INSUFFICIENT_BALANCE" in failure["agent_error"]


def test_strict_longrun_privately_retries_core_agent_provider_outage(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "2")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "30")

    delay = harness._service_retry_delay_seconds(
        label="玩家自由讨论",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=500,
        body={
            "ok": False,
            "error": "LLM HTTP 502: core GM provider unavailable",
            "llm_unavailable": True,
            "llm_failure_kind": "provider_unavailable",
        },
        attempt=2,
    )

    assert delay == 15.0


def test_strict_longrun_waits_until_open_provider_circuit_can_be_probed(
    monkeypatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "2")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "30")
    client = SimpleNamespace(
        circuit_breaker_payload=lambda: {
            "circuits": [
                {
                    "state": "open",
                    "retry_after_seconds": 29.4,
                }
            ]
        }
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            gm_tool_agent=SimpleNamespace(client=client),
            expressor=SimpleNamespace(client=None),
        )
    )

    delay = harness._service_retry_delay_seconds(
        label="玩家行动",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body={
            "ok": True,
            "route": "gm_agent_unavailable_silent",
            "agent_error": "LLM HTTP 502: upstream unavailable",
            "tool_receipts": [],
        },
        attempt=1,
    )

    assert delay == pytest.approx(30.4)
    assert harness._is_provider_unavailable_exception(
        RuntimeError("LLM provider circuit is open; retry after 14.8s")
    )


def test_strict_longrun_retries_agent_provider_failure_only_before_any_commit(
    monkeypatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "2")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "30")
    unavailable = {
        "ok": True,
        "route": "gm_agent_unavailable_silent",
        "target": "silent",
        "reply": "",
        "agent_error": "LLM HTTP 502: upstream unavailable",
        "tool_receipts": [],
    }

    assert harness._service_retry_delay_seconds(
        label="角色增量选择",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body=unavailable,
        attempt=1,
    ) == 10.0

    # A validation rejection followed by a transient provider outage is still
    # safe to replay when no write tool succeeded. The public route name may
    # remain gm_agent_tool because the failure policy produced a reply.
    unavailable.update(
        {
            "route": "gm_agent_tool",
            "reply": "这次操作没有成功，我没有改动当前状态。",
            "agent_error": "LLM HTTP 503: Service temporarily unavailable",
            "tool_receipts": [
                {
                    "tool_name": "commit_session_zero_update",
                    "ok": False,
                    "state_changed": False,
                    "error_code": "INVALID_ARGUMENTS",
                }
            ],
        }
    )
    assert harness._service_retry_delay_seconds(
        label="贡献纠正后供应商中断",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body=unavailable,
        attempt=1,
    ) == 10.0

    unavailable["tool_receipts"] = [
        {"tool_name": "update_hero_draft", "ok": True, "state_changed": True}
    ]
    assert harness._service_retry_delay_seconds(
        label="已经提交状态",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body=unavailable,
        attempt=1,
    ) is None


def test_strict_longrun_does_not_retry_validation_failure_without_provider_outage(
    monkeypatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "2")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "30")
    unavailable = {
        "ok": True,
        "route": "gm_agent_tool_failure",
        "reply": "这次操作没有成功，我没有改动当前状态。",
        "tool_receipts": [
            {
                "tool_name": "update_hero_draft",
                "ok": False,
                "state_changed": False,
                "error_code": "INVALID_ARGUMENTS",
            }
        ],
    }

    assert harness._service_retry_delay_seconds(
        label="角色增量选择",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=200,
        body=unavailable,
        attempt=1,
    ) is None


def test_strict_longrun_never_replays_invalid_core_output_or_stateful_turn(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "9")
    invalid_json = {
        "ok": False,
        "error": "Core GM returned invalid structured output.",
        "llm_invalid_output": True,
        "llm_failure_kind": "invalid_json",
    }
    provider_outage = {
        "ok": False,
        "error": "LLM HTTP 502: core GM provider unavailable",
        "llm_unavailable": True,
        "llm_failure_kind": "provider_unavailable",
    }

    assert harness._service_retry_delay_seconds(
        label="坏 JSON",
        method="POST",
        route="/v1/message/route",
        payload={},
        status=500,
        body=invalid_json,
        attempt=1,
    ) is None
    assert harness._service_retry_delay_seconds(
        label="已进入游戏结算",
        method="POST",
        route="/v1/game/turn",
        payload={},
        status=500,
        body=provider_outage,
        attempt=1,
    ) is None


def test_harness_service_recovery_records_only_final_public_call(tmp_path: Path, monkeypatch) -> None:
    harness = object.__new__(FromScratchUltraHarness)
    responses = [
        (
            500,
            {
                "ok": False,
                "error": "Core GM provider unavailable.",
            },
        ),
        (200, {"ok": True, "target": "silent", "send_reply": False}),
    ]

    class FakeService:
        runtimes = {}

        @staticmethod
        def handle(method, route, payload):
            return responses.pop(0)

    harness.service = FakeService()
    harness.calls = []
    harness.errors = []
    harness.notes = []
    harness.expected_rules_blocked_labels = set()
    harness._auto_followup_depth = 1
    harness.progress_path = tmp_path / "progress.jsonl"
    harness.conversation_path = tmp_path / "conversation.txt"
    harness.conversation_path.write_text("", encoding="utf-8")
    harness._service_retry_delay_seconds = lambda **kwargs: 0.0 if kwargs["attempt"] == 1 else None
    monkeypatch.setattr("scripts.run_ultra_from_scratch_campaign_test.time.sleep", lambda _delay: None)

    body = harness.invoke(
        "自由讨论",
        "POST",
        "/v1/message/route",
        {"campaign_id": "demo", "speaker": "南星", "message": "大家怎么分工？"},
    )

    assert body["target"] == "silent"
    assert len(harness.calls) == 1
    assert len(harness.calls[0]["service_recovery_attempts"]) == 1
    assert harness.calls[0]["status"] == 200
    assert harness.errors == []


def test_longrun_preflight_waits_through_provider_502(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "2")
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "0")
    monkeypatch.setattr("scripts.run_20_session_campaign_test.time.sleep", lambda _delay: None)

    class RecoveringClient:
        calls = 0

        def create_chat_completion(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LLMHTTPError(status_code=502, body="upstream request failed")
            return '{"ok":true}'

    client = RecoveringClient()
    content, recoveries = harness._preflight_completion_with_recovery(
        client=client,
        model="gpt-5.6-luna",
        component_name="ActionBrain",
    )

    assert content == '{"ok":true}'
    assert client.calls == 2
    assert len(recoveries) == 1


def test_longrun_preflight_does_not_retry_invalid_local_output(monkeypatch) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    monkeypatch.setenv("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "9")

    class InvalidClient:
        calls = 0

        def create_chat_completion(self, **_kwargs):
            self.calls += 1
            raise ValueError("invalid JSON")

    client = InvalidClient()
    with pytest.raises(ValueError, match="invalid JSON"):
        harness._preflight_completion_with_recovery(
            client=client,
            model="gpt-5.6-luna",
            component_name="ActionBrain",
        )

    assert client.calls == 1
