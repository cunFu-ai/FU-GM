import os
import tempfile
from pathlib import Path

from fu_gm.testing.model_benchmark import (
    ModelProbeTurn,
    ModelProviderSpec,
    _provider_environment,
    _equipment_access_state,
    _next_player_window_followup,
    _probe_result,
    compare_probe_results,
    load_provider_from_dotenv,
)
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Character, HeroDraft


def test_provider_loader_keeps_secret_out_of_repr() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / ".env"
        path.write_text(
            "FU_GM_API_BASE_URL=https://example.invalid/v1\n"
            "FU_GM_API_KEY=do-not-print-me\n",
            encoding="utf-8",
        )
        provider = load_provider_from_dotenv(
            path,
            name="probe",
            model="model-x",
        )

    assert provider.endpoint_host == "example.invalid"
    assert provider.response_format_enabled is True
    assert "do-not-print-me" not in repr(provider)


def test_provider_environment_accepts_a_bounded_endpoint_timeout() -> None:
    spec = ModelProviderSpec(
        name="probe",
        api_base_url="https://example.invalid/v1",
        api_key="secret",
        model="model-x",
    )

    with _provider_environment(
        spec,
        endpoint_attempt_timeout_seconds=45,
        core_endpoint_attempt_timeout_seconds=75,
    ):
        assert os.environ["FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS"] == "45.0"
        assert (
            os.environ["FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS"]
            == "75.0"
        )


def test_provider_loader_reads_response_format_capability() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / ".env"
        path.write_text(
            "FU_GM_API_BASE_URL=https://example.invalid/v1\n"
            "FU_GM_API_KEY=secret\n"
            "FU_GM_RESPONSE_FORMAT_ENABLED=0\n",
            encoding="utf-8",
        )
        provider = load_provider_from_dotenv(
            path,
            name="probe",
            model="model-x",
        )

    assert provider.response_format_enabled is False


def test_provider_loader_prefers_model_specific_api_key() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / ".env"
        path.write_text(
            "FU_GM_API_BASE_URL=https://example.invalid/v1\n"
            "FU_GM_API_KEY=shared-key\n"
            "FU_GM_LUNA_API_KEY=luna-key\n",
            encoding="utf-8",
        )
        provider = load_provider_from_dotenv(
            path,
            name="luna",
            model="gpt-5.6-luna",
        )

    assert provider.api_key == "luna-key"
    assert "luna-key" not in repr(provider)


def test_equipment_access_snapshot_reads_ready_kariba_heroes() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("probe")
        runtime.app.character_manager.add(
            Character(
                name="诺艾尔",
                attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                max_hp=45,
                hp=45,
                max_mp=45,
                mp=45,
                equipment=["钢匕首", "细剑"],
                unavailable_equipment={"钢匕首": {"reason": "收缴"}},
                equipped_main_hand="徒手攻击",
            )
        )

        state = _equipment_access_state(runtime)

    assert state["诺艾尔"]["unavailable"] == ["钢匕首"]
    assert state["诺艾尔"]["equipped_main_hand"] == "徒手攻击"
    assert "艾丽妮" not in state


def test_critical_opportunity_followup_names_its_beneficiary() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("probe")
        runtime.app.world_state.world_profile.hero_drafts["玩家"] = HeroDraft(
            player_name="玩家",
            hero_name="诺艾尔",
        )
        window = runtime.app.interceptor.decision_window_manager.create(
            kind="critical_opportunity",
            owner="诺艾尔",
            blocking=True,
        )

        followup = _next_player_window_followup(
            runtime,
            attempted_window_ids=set(),
        )

    assert followup is not None
    assert followup[0] == window.window_id
    assert "【优势】" in followup[1].text
    assert "【诺艾尔】" in followup[1].text


def test_model_probe_does_not_read_hidden_trait_window_for_player() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        runtime = service._runtime("probe")
        runtime.app.world_state.world_profile.hero_drafts["玩家"] = HeroDraft(
            player_name="玩家",
            hero_name="诺艾尔",
        )
        runtime.app.interceptor.decision_window_manager.create(
            kind="trait_invocation",
            owner="诺艾尔",
            blocking=False,
            payload={"silent_failure_grace": True, "roll_success": False},
        )

        followup = _next_player_window_followup(
            runtime,
            attempted_window_ids=set(),
        )

    assert followup is None


def test_comparison_prefers_quality_before_latency() -> None:
    report = compare_probe_results(
        [
            {
                "provider": "fast",
                "provider_available": True,
                "quality_score": 75.0,
                "p50_latency_ms": 10,
            },
            {
                "provider": "careful",
                "provider_available": True,
                "quality_score": 100.0,
                "p50_latency_ms": 100,
            },
        ]
    )

    assert report["ranking"] == ["careful", "fast"]


def test_comparison_never_prefers_unavailable_provider_for_speed() -> None:
    report = compare_probe_results(
        [
            {
                "provider": "failed-fast",
                "provider_available": False,
                "quality_score": 0.0,
                "p50_latency_ms": 1,
            },
            {
                "provider": "working",
                "provider_available": True,
                "quality_score": 50.0,
                "p50_latency_ms": 1000,
            },
        ]
    )

    assert report["ranking"] == ["working", "failed-fast"]
    assert "working" in report["recommendation"]


def test_probe_reports_transport_failure_separately_from_gm_behavior() -> None:
    result = _probe_result(
        ModelProviderSpec(
            name="flaky",
            model="model-x",
            api_base_url="https://example.invalid/v1",
            api_key="secret",
        ),
        [
            ModelProbeTurn(
                index=1,
                speaker="玩家",
                message="开始吧",
                expected="reply",
                status=200,
                elapsed_ms=20,
                target="fu_gm",
                route="gm_agent_unavailable_silent",
                send_reply=False,
                reply="",
                agent_error="temporary 502",
                model_call_count=1,
                successful_model_call_count=0,
                failed_model_call_count=1,
            )
        ],
        gate_status="session_zero",
        scene_name="",
        working_brief={},
    )

    assert result["behavior_quality_score"] == 0.0
    assert result["infrastructure_score"] == 0.0
    assert result["end_to_end_score"] < 100.0
    assert result["infrastructure_checks"]["no_unavailable_turns"] is False


def test_probe_requires_prison_opening_to_sync_equipment_access() -> None:
    spec = ModelProviderSpec(
        name="careful",
        model="model-x",
        api_base_url="https://example.invalid/v1",
        api_key="secret",
    )
    turns = [
        ModelProbeTurn(
            index=1,
            speaker="玩家",
            message="开始吧",
            expected="reply",
            status=200,
            elapsed_ms=20,
            target="fu_gm",
            route="gm_agent_tool",
            send_reply=True,
            reply="卡里巴村监狱的牢门震了一下。你们先做什么？",
            model_call_count=1,
            successful_model_call_count=1,
        )
    ]

    unsynced = _probe_result(
        spec,
        turns,
        gate_status="adventure",
        scene_name="卡里巴村监狱",
        working_brief={},
        equipment_access_state={
            "诺艾尔": {
                "unavailable": [],
                "equipped_main_hand": "细剑",
                "equipped_off_hand": "钢匕首",
            },
            "艾丽妮": {
                "unavailable": [],
                "equipped_main_hand": "法杖",
                "equipped_off_hand": "魔典",
            },
        },
    )
    synced = _probe_result(
        spec,
        turns,
        gate_status="adventure",
        scene_name="卡里巴村监狱",
        working_brief={},
        equipment_access_state={
            "诺艾尔": {
                "unavailable": ["钢匕首", "细剑"],
                "equipped_main_hand": "徒手攻击",
                "equipped_off_hand": "",
            },
            "艾丽妮": {
                "unavailable": ["法杖", "魔典"],
                "equipped_main_hand": "徒手攻击",
                "equipped_off_hand": "",
            },
        },
    )

    assert unsynced["behavior_quality_checks"][
        "opening_equipment_access_synced"
    ] is False
    assert synced["behavior_quality_checks"][
        "opening_equipment_access_synced"
    ] is True


def test_probe_rejects_state_writes_on_expected_silence() -> None:
    result = _probe_result(
        ModelProviderSpec(
            name="probe",
            model="model-x",
            api_base_url="https://example.invalid/v1",
            api_key="secret",
        ),
        [
            ModelProbeTurn(
                index=1,
                speaker="玩家甲",
                message="角色甲问角色乙：你看出了什么？",
                expected="silent",
                status=200,
                elapsed_ms=20,
                target="silent",
                route="gm_agent_silent_commit",
                send_reply=False,
                reply="",
                model_call_count=1,
                successful_model_call_count=1,
                receipts=[
                    {
                        "tool_name": "perform_in_scene_action",
                        "ok": True,
                        "state_changed": True,
                    }
                ],
            )
        ],
        gate_status="adventure",
        scene_name="牢房",
        working_brief={},
    )

    assert result["expected_silence_accuracy"] == 0.0
    assert result["silent_state_writes"] == 1
    assert result["behavior_quality_checks"][
        "no_state_writes_on_expected_silence"
    ] is False
