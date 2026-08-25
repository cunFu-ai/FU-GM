import json
import os

import pytest
from types import SimpleNamespace

import scripts.run_20_session_campaign_test as campaign_runner
import scripts.run_ultra_from_scratch_campaign_test as ultra_runner
from scripts.run_20_session_campaign_test import CampaignSessionSpec, TwentySessionCampaignHarness
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.scene_transition_coordinator import SceneTransitionAnchor
from fu_gm.components.scene_cast_coordinator import SceneCastCoordinator
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import SceneType
from fu_gm.testing.legal_actions import LegalActionLayer
from fu_gm.testing.codex_subagent_spool import CodexSubagentSpoolClient
from fu_gm.testing.luna_player_agent import PlayerPersona
from fu_gm.testing.natural_table_runtime import NaturalTableRuntime
from fu_gm.testing.player_simulator import SimulatedUtterance
from fu_gm.testing.replay_models import LegalActionContext


def test_ultra_report_discloses_direct_component_paths(tmp_path) -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.conversation_path = tmp_path / "conversation.txt"
    harness.conversation_path.write_text("", encoding="utf-8")

    rendered = harness._format_report(
        {
            "campaign_id": "audit",
            "session_id": "session",
            "ok": True,
            "test_fidelity": {
                "classification": "hybrid_component_integration",
                "production_e2e_verified": False,
                "direct_component_paths": ["scene_start", "conflict_fixture_injection"],
            },
            "checks": {},
            "errors": [],
            "notes": [],
            "latency": {"count": 0, "total_ms": 0, "avg_ms": 0, "max_ms": 0, "slowest": []},
            "map_status": {},
            "dashboard_phase": {},
            "chapter_package": {},
            "tool_events": [],
            "core_design_tools": {},
            "artifacts": {},
        }
    )

    assert "分类: hybrid_component_integration" in rendered
    assert "已验证生产端到端: False" in rendered
    assert "- scene_start" in rendered
    assert "- conflict_fixture_injection" in rendered


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


def test_campaign_speaker_fallback_uses_active_three_player_roster() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)

    speakers = harness._seed_speakers(
        CampaignSessionSpec(1, "空白场", "第一幕", "", [])
    )

    assert speakers == ["阿凛", "南星", "白河"]


def test_gm_stinger_is_derived_from_current_session_opposition() -> None:
    spec = CampaignSessionSpec(
        number=5,
        title="静锈扩散",
        arc="第一幕",
        gm_opening="",
        turns=[],
        expected_focus=["保护诊所", "会传染的静锈", "保住修复工具"],
        episode_identity={
            "opposition": "与静锈扩散有关的行动者会争夺受污染的工具",
            "payoff": ["诊所是否恢复运转", "一套工具的归属确定"],
        },
    )

    objective, message = TwentySessionCampaignHarness._gm_stinger_brief(spec)

    assert "静锈扩散" in message
    assert "诊所是否恢复运转" in message
    assert "本场对立方" in objective
    assert "艾蕾娜" not in message
    assert "辉钢财团" not in message


def test_gm_stinger_uses_session_focus_when_identity_has_no_opposition() -> None:
    spec = CampaignSessionSpec(
        number=5,
        title="无风航路",
        arc="第一幕",
        gm_opening="",
        turns=[],
        expected_focus=["护送渡船", "不断扩张的无风带", "抵达北岸"],
    )

    _objective, message = TwentySessionCampaignHarness._gm_stinger_brief(spec)

    assert "不断扩张的无风带" in message


def test_ultra_session_zero_fixture_is_incremental_too() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)

    turns = harness._session_zero_world_turns()
    messages = [message for _speaker, message in turns]

    assert len(turns) >= 13
    assert any("大家觉得" in message for message in messages)
    assert any("我赞成" in message or "我也同意" in message for message in messages)
    assert any("先跳过" in message for message in messages)
    assert any("我的威胁贡献是" in message for message in messages)
    assert not any(
        all(
            token in message
            for token in ("魔法与科技", "界限：", "重大历史事件", "世界奥秘", "世界威胁")
        )
        for message in messages
    )


def test_ultra_character_recovery_ignores_world_only_gate_blocker() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.errors = []
    harness.gate_body = {
        "blocked": True,
        "blockers": {
            "reason": "session_zero_world_incomplete",
            "hero_creation": {"ready": True, "missing_by_player": {}},
            "session_zero": {
                "ready": False,
                "missing": ["每位玩家的威胁贡献或跳过"],
            },
        },
    }

    harness._recover_missing_character_fields()

    assert harness.errors == []


@pytest.mark.parametrize(
    ("semantic_llm", "typed_setup", "expected"),
    [
        (True, False, False),
        (True, True, True),
        (False, False, True),
    ],
)
def test_typed_setup_fixture_can_skip_only_repeated_setup(
    semantic_llm: bool,
    typed_setup: bool,
    expected: bool,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = semantic_llm
    harness.typed_setup = typed_setup

    assert harness._uses_typed_setup_fixture() is expected


def test_codex_spool_bundle_shares_one_test_only_client(tmp_path) -> None:
    bundle = TwentySessionCampaignHarness._build_test_llm_bundle(tmp_path)

    assert isinstance(bundle.core, CodexSubagentSpoolClient)
    assert bundle.test_only is True
    assert bundle.model == "codex-subagent-test"
    assert bundle.core is bundle.expressor
    assert bundle.core is bundle.npc_design
    assert bundle.core is bundle.pacing
    assert bundle.core is bundle.summarizer
    assert bundle.core is bundle.player


def test_codex_spool_mode_removes_external_api_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.run_root = tmp_path
    monkeypatch.setenv("FU_GM_API_KEY", "must-not-survive")
    monkeypatch.setenv("FU_GM_TERRA_API_KEY", "must-not-survive")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    monkeypatch.setenv("FU_GM_DOTENV_PATH", "")
    monkeypatch.setenv("FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT", "")

    harness._disable_external_api_credentials_for_spool()

    assert "FU_GM_API_KEY" not in os.environ
    assert "FU_GM_TERRA_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["FU_GM_DOTENV_PATH"].endswith(
        ".codex-spool-do-not-load-dotenv"
    )
    assert os.environ["FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT"] == "1"


def test_ultra_codex_spool_mode_removes_external_api_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.run_root = tmp_path
    monkeypatch.setenv("FU_GM_API_KEY", "must-not-survive")
    monkeypatch.setenv("FU_GM_TERRA_API_KEY", "must-not-survive")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-survive")
    monkeypatch.setenv("FU_GM_DOTENV_PATH", "")
    monkeypatch.setenv("FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT", "")

    harness._disable_external_api_credentials_for_spool()

    assert "FU_GM_API_KEY" not in os.environ
    assert "FU_GM_TERRA_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT"] == "1"
    assert os.environ["FU_GM_DOTENV_PATH"].endswith(
        ".codex-spool-do-not-load-dotenv"
    )


def test_ultra_harness_attaches_distinct_stable_group_message_ids() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.calls = []
    payload = {
        "campaign_id": "campaign",
        "session_id": "session",
        "channel_id": "group",
        "speaker": "阿凛",
        "message": "我查看门锁。",
        "message_id": "inherited-parent-id",
    }

    first = harness._attach_test_message_identity(
        "玩家行动 01",
        "POST",
        "/v1/game/turn",
        payload,
    )
    repeated = harness._attach_test_message_identity(
        "玩家行动 01",
        "POST",
        "/v1/game/turn",
        payload,
    )
    harness.calls.append({"label": "玩家行动 01"})
    followup = harness._attach_test_message_identity(
        "自动回应GM追问 阿凛",
        "POST",
        "/v1/game/turn",
        {**payload, "message": "要投。"},
    )

    assert first["message_id"].startswith("longrun-00001-")
    assert repeated["message_id"] == first["message_id"]
    assert followup["message_id"].startswith("longrun-00002-")
    assert followup["message_id"] != first["message_id"]
    assert payload["message_id"] == "inherited-parent-id"


def test_ultra_session_zero_turns_use_the_real_group_message_router() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.common = {
        "campaign_id": "campaign",
        "session_id": "session",
        "channel_id": "group",
    }
    harness.errors = []
    captured: dict[str, object] = {}

    def invoke(label, method, route, payload):
        captured.update(
            {
                "label": label,
                "method": method,
                "route": route,
                "payload": payload,
            }
        )
        return {
            "target": "silent",
            "send_reply": False,
            "tool_receipts": [
                {"ok": True, "state_changed": True}
            ],
        }

    harness.invoke = invoke
    harness._record_tool_event = lambda *_args, **_kwargs: None

    result = harness.route_session_zero_message(
        "第零章角色创建 01 阿凛",
        "阿凛",
        "伊莉雅选择保镖。",
    )

    assert result["target"] == "silent"
    assert captured["method"] == "POST"
    assert captured["route"] == "/v1/message/route"
    assert captured["payload"]["is_at_bot"] is False
    assert harness.errors == []


def test_ultra_checkpoint_resume_replays_only_missing_character_turns() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness._session_zero_character_turns = lambda: [
        ("阿凛", "伊莉雅选择保镖。"),
        ("阿凛", "伊莉雅选择防御精通。"),
        ("南星", "赛璃选择灵魂魔法。"),
    ]
    routed: list[tuple[str, str, str]] = []
    def route(label, speaker, message):
        routed.append((label, speaker, message))
        return {"route": "gm_agent_silent_commit"}

    harness.route_session_zero_message = route

    harness._resume_missing_session_zero_character_turns(
        {"第零章角色创建 01 阿凛", "第零章角色创建 03 南星"}
    )

    assert routed == [
        (
            "第零章角色创建 02 阿凛",
            "阿凛",
            "伊莉雅选择防御精通。",
        )
    ]


def test_ultra_checkpoint_resume_stops_on_an_uncertain_inflight_message() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness._session_zero_character_turns = lambda: [
        ("阿凛", "伊莉雅确认角色并正式建卡。"),
    ]
    harness.route_session_zero_message = lambda *_args, **_kwargs: {
        "route": "deduplicated_incomplete",
    }

    with pytest.raises(RuntimeError, match="未完成去重记录"):
        harness._resume_missing_session_zero_character_turns(set())


def test_ultra_adventure_transition_uses_agent_start_session_and_start_scene() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.pc_names = ["伊莉雅"]
    harness.campaign_id = "campaign"
    harness.channel_id = "group"
    harness.session_id = "session"
    harness.errors = []
    scene = SimpleNamespace(
        name="第一章",
        scene_type=SceneType.STANDARD,
    )
    app = SimpleNamespace(
        scene_manager=SimpleNamespace(current_scene=None),
        session_zero_manager=SimpleNamespace(
            hero_creation_status=lambda: {"ready": True},
            world_creation_ready=lambda: True,
        ),
        world_map_generation_status=lambda: {"status": "generated"},
    )
    gate = SimpleNamespace(status="session_zero", reason="")
    harness._runtime = lambda: SimpleNamespace(app=app)
    harness._snapshot = lambda **_kwargs: {}
    harness.service = SimpleNamespace(
        session_gates=SimpleNamespace(
            get=lambda *_args: gate,
        )
    )
    def route(*_args, **_kwargs):
        gate.status = "adventure"
        app.scene_manager.current_scene = scene
        return {
            "ok": True,
            "tool_receipts": [
                {
                    "tool_name": "start_session",
                    "ok": True,
                    "result": {},
                },
                {
                    "tool_name": "start_scene",
                    "ok": True,
                    "result": {"scene": {"name": "第一章"}},
                },
            ],
        }

    harness.route_table_message = route

    harness._enter_adventure_after_session_zero()

    assert harness.gate_body["blocked"] is False
    assert harness.gate_body["opening_tool_receipts"] == [
        "start_session",
        "start_scene",
    ]
    assert harness.errors == []


def test_ultra_completed_labels_ignore_failed_checkpoint_steps() -> None:
    harness = object.__new__(ultra_runner.FromScratchUltraHarness)
    harness.calls = [
        {"label": "成功步骤", "ok": True},
        {"label": "失败步骤", "ok": False},
        {"label": "缺失状态"},
    ]

    assert harness._completed_labels() == {"成功步骤"}


def test_codex_spool_report_cannot_claim_api_identity_or_cache(tmp_path) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.semantic_backend = "codex_subagent_spool"
    harness._llm_preflight_ok = True
    harness.test_llm_bundle = TwentySessionCampaignHarness._build_test_llm_bundle(
        tmp_path
    )

    report = harness._semantic_backend_report()

    assert report["test_only"] is True
    assert report["external_api_called"] is False
    assert report["model_identity_verified"] is False
    assert report["usage_available"] is False
    assert report["prompt_cache_available"] is False
    assert report["latency_comparable_to_external_api"] is False
    assert report["queue_round_trip"]["pending_calls"] == 0


def test_codex_spool_pending_call_is_not_reported_as_complete(tmp_path) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.test_llm_bundle = TwentySessionCampaignHarness._build_test_llm_bundle(
        tmp_path
    )
    harness.test_llm_bundle.core.calls.append({"status": "waiting"})

    assert harness._test_backend_has_no_pending_calls() is False


def test_external_backend_report_does_not_claim_unknown_usage_as_available() -> None:
    class FakeClient:
        reported = False

        @classmethod
        def telemetry_payload(cls):
            return {
                "total_calls": 1,
                "prompt_cache": {
                    "usage_status": "reported" if cls.reported else "unknown",
                    "usage_reported_calls": 1 if cls.reported else 0,
                    "prompt_tokens": 800 if cls.reported else 0,
                },
            }

    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.semantic_backend = "external_openai_compatible_api"
    harness._llm_preflight_ok = True
    harness.test_llm_bundle = None
    harness.service = SimpleNamespace(
        gm_agent_runtime=SimpleNamespace(llm_client=FakeClient()),
        gm_tool_agent=None,
    )

    unknown = harness._semantic_backend_report()

    assert unknown["usage_available"] is False
    assert unknown["usage_status"] == "unknown"
    assert unknown["prompt_cache_available"] is False
    assert unknown["prompt_cache_usage_status"] == "unknown"

    FakeClient.reported = True
    reported = harness._semantic_backend_report()

    assert reported["usage_available"] is True
    assert reported["usage_status"] == "reported"
    assert reported["prompt_cache_available"] is True
    assert reported["prompt_cache_usage_status"] == "reported"


def test_codex_spool_forces_injected_player_client_over_legacy_environment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign_runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("FU_GM_REPLAY_PLAYER_ENGINE", "legacy")
    monkeypatch.setenv("FU_GM_DOTENV_PATH", "")
    monkeypatch.setenv("FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT", "")
    harness = TwentySessionCampaignHarness(
        target_sessions=1,
        run_astrbot_smoke=False,
        semantic_llm=True,
        setup_only=True,
        codex_spool_root=tmp_path / "spool",
    )

    assert harness.player_engine == "natural_v1"
    assert harness.player_simulator.client is harness.test_llm_bundle.player
    assert isinstance(harness.player_simulator.client, CodexSubagentSpoolClient)
    assert harness._rule_followup_depth == 0
    client_audit = harness._test_client_registry_audit()
    assert client_audit["applicable"] is True
    assert client_audit["all_known_roles_use_test_client"] is True
    assert client_audit["unexpected_roles"] == []


class _NaturalHarnessAgent:
    def __init__(self, utterance: SimulatedUtterance) -> None:
        self.utterance = utterance
        self.model = "fake-natural"
        self.use_llm = True
        self.client = None
        self.last_action_progress_review = {}
        self.last_table_discussion_review = {}

    def compose(self, **_kwargs):
        return self.utterance

    def telemetry_payload(self):
        return {}


def test_natural_campaign_slot_does_not_preselect_speaker() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    personas = {
        "甲": PlayerPersona("甲", "甲英雄", "谨慎", "简短"),
        "乙": PlayerPersona("乙", "乙英雄", "主动", "直接"),
    }
    utterances = {
        "甲": SimulatedUtterance(
            text="",
            decision="wait",
            utterance_kind="wait",
            audience="table",
        ),
        "乙": SimulatedUtterance(
            text="乙英雄贴近窗边听门外的脚步。",
            decision="speak",
            utterance_kind="action",
            audience="gm",
            speak_after_ms=700,
        ),
    }
    harness.player_simulator = NaturalTableRuntime(
        personas=personas,
        agent_factory=lambda persona: _NaturalHarnessAgent(
            utterances[persona.player_name]
        ),
    )
    harness.player_engine = "natural_v1"
    harness.min_table_turns_per_session = 4
    harness.player_simulation_metrics = []
    harness.calls = [
        {
            "index": 1,
            "route": "/v1/game/scene-opening",
            "speaker": "时悠",
            "message": "后台开场请求",
            "reply": "门外传来两组不一致的脚步声。",
        }
    ]
    harness.campaign_id = "campaign"
    harness.session_id = "session"
    harness.channel_id = "channel"
    harness.service = object()
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            conflict_manager=SimpleNamespace(
                state=SimpleNamespace(active=False),
                format_turn_board=lambda: {},
            ),
            interceptor=SimpleNamespace(
                decision_window_manager=SimpleNamespace(
                    public_summary=lambda: []
                )
            ),
        )
    )
    harness.player_legal_actions = SimpleNamespace(
        build=lambda *_args, **_kwargs: LegalActionContext(
            stage_goal="公开局面",
            scene_name="牢区",
            legal_actions=["调查"],
        )
    )
    harness._natural_last_public_signature = ""
    harness._natural_last_event = None
    harness._natural_stale_drafts = {}
    harness._natural_quiet_wave_count = 0
    harness._recent_public_dialogue = lambda **_kwargs: (
        "时悠：门外传来两组不一致的脚步声。"
    )
    spec = CampaignSessionSpec(1, "越狱", "第一幕", "", [])

    turns = harness._expanded_session_turns(spec)
    message = harness._simulate_player_turn(
        spec,
        "框架预设名字不应生效",
        1,
    )

    assert turns == [("__NATURAL__", "__SIMULATE__")] * 4
    assert message == "乙英雄贴近窗边听门外的脚步。"
    assert harness.player_simulation_metrics[-1]["speaker"] == "乙"
    assert harness.player_simulation_metrics[-1]["reactions"][0]["decision"] == "wait"


def test_natural_table_keeps_separate_gm_reply_parts_as_public_messages() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.player_simulator = SimpleNamespace(personas={"阿凛": object()})
    harness.calls = [
        {
            "index": 7,
            "route": "/v1/message/route",
            "speaker": "阿凛",
            "message": "我和艾丽妮在同一间牢房吗？顺便处理失物机会。",
            "reply": "不是同一间。\n铁片落在东侧牢房的铁栏旁。",
            "body": {
                "reply_parts": [
                    "不是同一间。",
                    "铁片落在东侧牢房的铁栏旁。",
                ]
            },
        }
    ]

    assert harness._latest_public_table_sources() == [
        (
            "7:message",
            "阿凛",
            "我和艾丽妮在同一间牢房吗？顺便处理失物机会。",
        ),
        ("7:reply:0", "时悠", "不是同一间。"),
        ("7:reply:1", "时悠", "铁片落在东侧牢房的铁栏旁。"),
    ]
    assert harness._latest_public_table_source() == (
        "7:reply:1",
        "时悠",
        "铁片落在东侧牢房的铁栏旁。",
    )


def test_natural_gm_cadence_counts_discussion_without_calling_it_an_action() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._natural_table_active = lambda: True
    harness.min_table_turns_per_session = 28
    harness.max_table_turns_per_session = 42

    assert harness._gm_cadence_counter(
        player_turn_count=2,
        processed_player_turns=6,
    ) == 6
    assert harness._natural_table_event_limit() == 56


def test_legacy_gm_cadence_still_counts_only_assigned_actions() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._natural_table_active = lambda: False

    assert harness._gm_cadence_counter(
        player_turn_count=2,
        processed_player_turns=6,
    ) == 2


def test_natural_session_zero_does_not_play_fixed_contribution_turns() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._natural_table_active = lambda: True
    calls: list[str] = []
    harness._run_natural_setup_contributions = lambda: calls.append("natural")
    harness._session_zero_world_turns = lambda: (_ for _ in ()).throw(
        AssertionError("fixed world turns must not be read")
    )

    harness._run_setup_contributions()

    assert calls == ["natural"]


def test_natural_chapter_start_uses_actual_player_response_not_fixed_arlin() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.campaign_id = "campaign"
    harness.channel_id = "channel"
    harness.session_id = "campaign-session-01"
    harness.common = {
        "campaign_id": "campaign",
        "channel_id": "channel",
        "session_id": "campaign-session-01",
    }
    harness.player_engine = "natural_v1"
    harness.player_simulation_metrics = []
    harness.errors = []
    harness._adventure_started = False
    activated: list[tuple[str, str, str, str]] = []
    harness.service = SimpleNamespace(
        session_gates=SimpleNamespace(
            activate=lambda campaign, channel, session, **kwargs: activated.append(
                (campaign, channel, session, kwargs["status"])
            )
        )
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(
                    scene_type=SceneType.STANDARD,
                    location="新共创世界的第一处现场",
                    objective="处理眼前的具体危机",
                    summary="局势已经公开",
                    participants=["三名英雄"],
                )
            )
        )
    )
    invoked: list[tuple[str, dict[str, object]]] = []

    def invoke(label, _method, _route, payload):
        invoked.append((label, dict(payload)))
        if label.startswith("自然玩家回应开章邀请"):
            return {
                "gate": {"status": "adventure"},
                "reply": "悬索忽然停在半空，修桥匠从摇晃的平台上向三名英雄招手。",
                "tool_receipts": [
                    {
                        "tool_name": "start_session",
                        "ok": True,
                        "state_changed": True,
                    },
                    {
                        "tool_name": "start_scene",
                        "ok": True,
                        "state_changed": True,
                    },
                ],
            }
        return {"ok": True, "reply": "现在进入第一章吗？"}

    harness.invoke = invoke

    def simulate(_spec, _index, **_kwargs):
        harness.player_simulation_metrics.append(
            {
                "speaker": "乙",
                "utterance_kind": "table_discussion",
                "audience": "gm",
            }
        )
        return "我这边没要改的了，可以开第一章。"

    harness._simulate_natural_table_turn = simulate
    harness._wait_for_async_map_if_any = lambda: None
    harness._write_campaign_checkpoint = lambda *_args, **_kwargs: None
    harness._record_tool_event = lambda *_args, **_kwargs: None
    harness.calls = []
    spec = CampaignSessionSpec(1, "迟响风铃", "第一幕", "", [])

    assert harness._ensure_natural_adventure_started(spec) is True
    player_payloads = [
        payload
        for label, payload in invoked
        if label.startswith("自然玩家回应开章邀请")
    ]
    assert player_payloads == [
        {
            **harness.common,
            "speaker": "乙",
            "message": "我这边没要改的了，可以开第一章。",
        }
    ]
    assert activated == [
        ("campaign", "channel", "campaign-session-01", "session_zero")
    ]


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


def test_strict_longrun_accepts_an_authoritative_silent_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.semantic_llm = True
    harness.fail_fast_route_mismatch = True
    harness.calls = []
    harness.errors = []
    body = {
        "target": "silent",
        "send_reply": False,
        "reply": "",
        "tool_receipts": [
            {
                "ok": True,
                "state_changed": True,
                "result": {"silent_commit_allowed": True},
            }
        ],
    }

    def routed(*_args, **_kwargs):
        harness.errors.extend(
            [
                "本地动作 routing target='silent', expected 'fu_gm'",
                "本地动作 send_reply=False, expected True",
            ]
        )
        harness.calls.append({})
        return body

    monkeypatch.setattr(
        TwentySessionCampaignHarness.__mro__[1],
        "route_table_message",
        routed,
    )

    result = harness.route_table_message(
        "本地动作",
        "南星",
        "赛璃退到门边等待。",
        expected_target="fu_gm",
        expected_send_reply=True,
    )

    assert result == body
    assert harness.errors == []
    assert harness.calls[-1]["accepted_silent_commit"] is True
    assert harness.calls[-1]["expected_target"] == "silent"


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
    option_index = next(
        index
        for index, message in enumerate(messages)
        if "洛岚的便携装置选择魔导装置" in message
    )
    confirmation_index = next(
        index
        for index, message in enumerate(messages)
        if "洛岚确认角色并正式建卡" in message
    )

    assert option_index < confirmation_index


def test_chapter_package_is_registered_before_final_first_act_invitation() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._session_zero_world_turns = lambda: []
    harness._session_zero_character_turns = lambda: [
        ("阿凛", "伊莉雅确认角色并正式建卡。"),
        ("阿凛", "我们确认第一幕：白花碑驿站的迟响。"),
    ]
    events: list[str] = []
    harness._ensure_test_chapter_package_registered = lambda: (
        events.append("chapter_package") or True
    )
    harness.route_session_zero_contribution = (
        lambda _label, _speaker, message, **_kwargs: events.append(message)
    )
    harness._write_campaign_checkpoint = lambda *_args, **_kwargs: None
    harness._assert_character_setup_complete = lambda: None

    harness._run_setup_contributions()

    assert events == [
        "伊莉雅确认角色并正式建卡。",
        "chapter_package",
        "我们确认第一幕：白花碑驿站的迟响。",
    ]


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


def test_pacing_sync_labels_player_opened_scene_with_prepared_function_role() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 1,
        "next_act": 2,
        "target_location": "白花碑驿站·登记小室",
        "public_target_announced": True,
        "prepared_opportunity_key": "s01-investigation",
        "prepared_opportunity_role": "social_or_investigation",
    }
    scene = SimpleNamespace(
        active=True,
        scene_id="scene-2",
        name="登记小室",
        location="白花碑驿站·登记小室",
        session_opportunity_key="",
        session_opportunity_role="",
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    harness._record_tool_event = lambda *_args, **_kwargs: None
    spec = CampaignSessionSpec(number=1, title="迟响", arc="序章", gm_opening="", turns=[])

    assert harness._synchronize_active_scene_act(spec, 2)
    assert scene.session_opportunity_key == "s01-investigation"
    assert scene.session_opportunity_role == "social_or_investigation"


def test_public_route_offer_reserves_two_player_actions_before_another_gm_beat() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._pending_scene_transition = {
        "session_number": 1,
        "current_act": 2,
        "next_act": 3,
        "target_location": "白花碑驿站·旧路闸门",
        "public_target_announced": True,
        "offered_at_turn_in_act": 6,
    }
    scene = SimpleNamespace(location="白花碑驿站·候车厅")
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    spec = CampaignSessionSpec(number=1, title="迟响", arc="序章", gm_opening="", turns=[])

    assert harness._public_transition_awaits_player_response(
        spec,
        current_act=2,
        turns_in_act=6,
    )
    assert harness._public_transition_awaits_player_response(
        spec,
        current_act=2,
        turns_in_act=7,
    )
    assert not harness._public_transition_awaits_player_response(
        spec,
        current_act=2,
        turns_in_act=8,
    )


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


def _opening_harness(scene) -> TwentySessionCampaignHarness:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_manager=SimpleNamespace(current_scene=scene))
    )
    return harness


def _opening_result(*, reply: str = "雾里的吊桥忽然停住，一名修桥匠抬头看向你们。") -> dict:
    return {
        "reply": reply,
        "tool_receipts": [
            {"tool_name": "start_session", "ok": True, "state_changed": True},
            {"tool_name": "start_scene", "ok": True, "state_changed": True},
        ],
    }


def test_first_scene_opening_accepts_any_committed_world_situation() -> None:
    harness = _opening_harness(
        SimpleNamespace(
            scene_type=SceneType.STANDARD,
            location="一座此前从未出现在测试词表里的浮空集市",
            objective="处理突然停摆的升降索",
            summary="摊贩和旅客被困在上下两层",
            participants=["三名英雄", "修桥匠"],
        )
    )

    assert harness._is_substantive_first_scene_opening(_opening_result())


def test_first_scene_opening_rejects_text_without_committed_scene_transaction() -> None:
    harness = _opening_harness(
        SimpleNamespace(
            scene_type=SceneType.STANDARD,
            location="浮空集市",
            objective="处理停摆",
            summary="",
            participants=[],
        )
    )

    assert not harness._is_substantive_first_scene_opening(
        {"reply": "我已经把开场写好了。", "tool_receipts": []}
    )


def test_first_scene_opening_rejects_session_zero_even_with_tool_claims() -> None:
    harness = _opening_harness(
        SimpleNamespace(
            scene_type=SceneType.SESSION_ZERO,
            location="共创桌",
            objective="讨论世界",
            summary="",
            participants=[],
        )
    )

    assert not harness._is_substantive_first_scene_opening(_opening_result())


def test_semantic_pending_npc_question_selects_its_addressed_player() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    frame_manager = SimpleNamespace(
        latest_pending_npc_question=lambda: {
            "npc": "本地巡守",
            "addressed_actor": "洛岚",
            "summary": "说明路线",
        }
    )
    harness._runtime = lambda: SimpleNamespace(
        app=SimpleNamespace(scene_frame_manager=frame_manager)
    )

    assert harness._preferred_npc_followup_speaker("阿凛") == "白河"


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
    harness = object.__new__(TwentySessionCampaignHarness)
    conditions = [
        {
            "npc": "白花守望会会长",
            "condition": "伊莉雅当面说明失名旅人的具体去向，并以自己的名义承担护送责任。",
            "required_actor": "伊莉雅",
            "status": "open",
        }
    ]

    assert harness._speaker_for_personal_condition("白河", conditions) == "阿凛"


def test_unassigned_open_condition_keeps_player_rotation() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    conditions = [
        {
            "npc": "白花守望会会长",
            "condition": "队伍说明去向，并由一名英雄承担护送责任。",
            "status": "open",
        }
    ]

    assert harness._speaker_for_personal_condition("白河", conditions) == "白河"


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
        current_act=2,
        turns_in_closure=1,
        pacing_can_end=False,
        authoritative_resolution=True,
        memory_anchor_complete=True,
        pending_blocking_decisions=0,
        turns_after_authoritative_resolution=1,
    )


def test_fictional_ending_waits_for_aftermath_memory_and_pending_choices() -> None:
    common = {
        "current_act": 4,
        "pacing_can_end": False,
        "authoritative_resolution": True,
        "memory_anchor_complete": True,
        "pending_blocking_decisions": 0,
        "turns_after_authoritative_resolution": 1,
    }

    assert not TwentySessionCampaignHarness._session_has_earned_fictional_ending(
        **{**common, "turns_after_authoritative_resolution": 0},
        turns_in_closure=1,
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

def test_player_simulator_telemetry_separates_unknown_cache_usage() -> None:
    class FakePlayerSimulator:
        engine_name = "luna_v2"
        model = "gpt-5.6-luna"
        use_llm = True

        @staticmethod
        def telemetry_payload():
            return {
                "total_calls": 3,
                "failed_calls": 0,
                "latency": {"sample_count": 3, "p50_ms": 5000},
                "prompt_cache": {
                    "enabled": True,
                    "configured_mode": "key",
                    "eligible_calls": 3,
                    "usage_reported_calls": 0,
                    "hit_calls": 0,
                    "known_miss_calls": 0,
                    "unknown_calls": 3,
                    "by_family": [{"family": "fu-pl-v2", "calls": 3}],
                    "by_operation": [{"operation": "fu_pl.generate", "calls": 3}],
                },
            }

    harness = object.__new__(TwentySessionCampaignHarness)
    harness.player_engine = "luna_v2"
    harness.player_simulator = FakePlayerSimulator()

    payload = harness._player_simulator_telemetry()

    assert payload["engine"] == "luna_v2"
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["prompt_cache"]["eligible_calls"] == 3
    assert payload["prompt_cache"]["hit_calls"] == 0
    assert payload["prompt_cache"]["known_miss_calls"] == 0
    assert payload["prompt_cache"]["unknown_calls"] == 3


def test_model_latency_report_persists_only_sanitized_operation_cache_metrics() -> None:
    class FakeClient:
        call_latency_history_ms = [120, 240]

        @staticmethod
        def telemetry_payload():
            return {
                "total_calls": 2,
                "failed_calls": 0,
                "last_call": {
                    "prompt": "PRIVATE_PROMPT",
                    "prompt_cache": {"key": "PRIVATE_CACHE_KEY"},
                },
                "latency": {
                    "sample_count": 2,
                    "p50_ms": 120,
                    "p95_ms": 240,
                    "max_ms": 240,
                },
                "prompt_cache": {
                    "usage_status": "partial",
                    "usage_reported_calls": 1,
                    "unknown_calls": 1,
                    "hit_calls": 1,
                    "known_miss_calls": 0,
                    "prompt_tokens": 1000,
                    "cached_tokens": 640,
                    "cache_miss_tokens": 360,
                    "cache_miss_tokens_reported_calls": 1,
                    "reported_read_ratio": 0.64,
                    "by_operation": [
                        {
                            "operation": "gm_tool_agent.iteration_1",
                            "calls": 2,
                            "successful_calls": 2,
                            "failed_calls": 0,
                            "usage_status": "partial",
                            "usage_reported_calls": 1,
                            "unknown_calls": 1,
                            "hit_calls": 1,
                            "known_miss_calls": 0,
                            "prompt_tokens": 1000,
                            "cached_tokens": 640,
                            "cache_miss_tokens": 360,
                            "cache_miss_tokens_reported_calls": 1,
                            "reported_read_ratio": 0.64,
                            "latency": {
                                "sample_count": 2,
                                "p50_ms": 120,
                                "p95_ms": 240,
                                "max_ms": 240,
                            },
                            "hit_latency": {
                                "sample_count": 1,
                                "p50_ms": 120,
                                "p95_ms": 120,
                                "max_ms": 120,
                            },
                            "miss_latency": {},
                            "prompt": "PRIVATE_PROMPT",
                            "cache_key": "PRIVATE_CACHE_KEY",
                        }
                    ],
                },
            }

    client = FakeClient()
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.service = SimpleNamespace(
        gm_tool_agent=SimpleNamespace(client=client),
        gm_agent_runtime=SimpleNamespace(llm_client=client),
    )
    harness.player_simulator = None
    harness._session_progress_evaluator = None
    harness.conversation_quality_auditor = SimpleNamespace(
        _percentile=lambda values, ratio: values[
            min(len(values) - 1, round((len(values) - 1) * ratio))
        ]
        if values
        else 0
    )
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            expressor=None,
            scene_creative_writer=None,
            npc_blueprint_designer=None,
            npc_voice_renderer=None,
        ),
        casual_chat=None,
        log_manager=SimpleNamespace(summarizer=None),
    )
    harness._runtime = lambda: runtime

    payload = harness._model_latency_metrics()

    assert payload["telemetry_scope"] == "current_process"
    assert len(payload["clients"]) == 1
    cache = payload["clients"][0]["prompt_cache"]
    operation = cache["by_operation"][0]
    assert cache["usage_status"] == "partial"
    assert cache["unknown_calls"] == 1
    assert operation == {
        "operation": "gm_tool_agent.iteration_1",
        "calls": 2,
        "successful_calls": 2,
        "failed_calls": 0,
        "usage_status": "partial",
        "usage_reported_calls": 1,
        "unknown_calls": 1,
        "hit_calls": 1,
        "known_miss_calls": 0,
        "prompt_tokens": 1000,
        "cached_tokens": 640,
        "cache_miss_tokens": 360,
        "cache_miss_tokens_reported_calls": 1,
        "reported_read_ratio": 0.64,
        "latency": {
            "sample_count": 2,
            "p50_ms": 120,
            "p95_ms": 240,
            "max_ms": 240,
        },
        "hit_latency": {
            "sample_count": 1,
            "p50_ms": 120,
            "p95_ms": 120,
            "max_ms": 120,
        },
        "miss_latency": {
            "sample_count": 0,
            "p50_ms": 0,
            "p95_ms": 0,
            "max_ms": 0,
        },
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "PRIVATE_PROMPT" not in serialized
    assert "PRIVATE_CACHE_KEY" not in serialized


def test_setup_only_report_persists_model_cache_telemetry(tmp_path) -> None:
    map_path = tmp_path / "map.svg"
    map_path.write_text("<svg/>", encoding="utf-8")
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.calls = []
    harness.errors = []
    harness.tool_events = []
    harness.astrbot_bridge_results = []
    harness.heartbeat_results = []
    harness._setup_only_completed = True
    harness.campaign_id = "telemetry-campaign"
    harness.channel_id = "telemetry-channel"
    harness.session_id = "session-zero"
    harness.target_sessions = 1
    harness.length_profile = "short"
    harness.semantic_llm = True
    harness.scripted_identities = False
    harness.pc_names = []
    harness.run_root = tmp_path
    harness.conversation_path = tmp_path / "conversation.txt"
    harness.conversation_export_path = tmp_path / "conversation-export.txt"
    harness.report_json_path = tmp_path / "report.json"
    harness.report_txt_path = tmp_path / "report.txt"
    harness.campaign_root = tmp_path / "campaigns"
    harness.map_root = tmp_path
    harness._agent_error_calls = lambda _calls: []
    harness._recovered_agent_error_calls = lambda _calls: []
    harness._failed_tool_receipts = lambda _calls: []
    harness._unrecovered_tool_failure_calls = lambda _calls: []
    harness._test_backend_has_no_pending_calls = lambda: True
    harness._semantic_backend_report = lambda: {}
    expected_model = {
        "telemetry_scope": "current_process",
        "clients": [
            {
                "prompt_cache": {
                    "usage_status": "unknown",
                    "by_operation": [],
                }
            }
        ],
    }
    harness._model_latency_metrics = lambda: expected_model
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            world_state=SimpleNamespace(
                world_profile=SimpleNamespace(hero_drafts={})
            ),
            session_zero_manager=SimpleNamespace(
                world_creation_ready=lambda: True
            ),
            character_manager=SimpleNamespace(exists=lambda _name: True),
        )
    )
    harness._runtime = lambda: runtime
    harness.service = SimpleNamespace(
        session_gates=SimpleNamespace(
            get=lambda *_args: SimpleNamespace(status="adventure")
        )
    )

    report = harness._build_setup_only_report()

    assert report["latency"]["model"] == expected_model


def test_voluntary_fu_pl_wait_is_not_replaced_by_scripted_table_talk() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.player_simulation_metrics = [
        {
            "kind": "table_discussion",
            "used_fallback": False,
            "model_attempts": [{"decision": "wait"}],
        }
    ]
    harness._simulate_table_discussion = lambda *_args, **_kwargs: ""
    spec = CampaignSessionSpec(1, "雨夜石牢", "第一幕", "", [])

    assert harness._opening_table_prompt(spec, 0) == ""
    assert harness._table_discussion_prompt(spec, 1) == ""


def test_table_discussion_identity_rotates_between_player_personas() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    spec = CampaignSessionSpec(1, "雨夜石牢", "第一幕", "", [])

    identities = {
        harness._table_discussion_identity(spec, index)
        for index in range(3)
    }

    assert identities == {
        ("阿凛", "伊莉雅"),
        ("南星", "赛璃"),
        ("白河", "洛岚"),
    }


def test_every_campaign_session_is_scoped_to_the_three_player_roster() -> None:
    harness = object.__new__(TwentySessionCampaignHarness)
    harness.target_sessions = 20

    sessions = harness._campaign_sessions()

    assert len(sessions) == 20
    for session in sessions:
        speakers = {speaker for speaker, _message in session.turns}
        assert speakers == {"阿凛", "南星", "白河"}
