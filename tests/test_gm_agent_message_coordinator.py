from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from fu_gm.components.gm_agent_message_coordinator import GMAgentMessageCoordinator
from fu_gm.components.gm_agent_outcome import GMToolAgentOutcome
from fu_gm.gm_tool_contracts import (
    GMToolPacingEvent,
    GMToolReceipt,
)
from fu_gm.http_server import FUGMHttpService


def test_request_metadata_contains_raw_message_and_recent_context_only() -> None:
    host = SimpleNamespace(
        _external_message_metadata=lambda _payload: {"message_id": "m-1"},
        _truthy=lambda value: bool(value),
    )
    coordinator = GMAgentMessageCoordinator(host)

    metadata = coordinator._request_metadata(
        {"forced_route_mode": "game"},
        message="时悠，请描述现场。",
        recent_context="大家已经确认进入第一章。",
    )

    assert metadata == {
        "message_id": "m-1",
        "current_message": "时悠，请描述现场。",
        "recent_public_context": "大家已经确认进入第一章。",
        "forced_route_mode": "game",
    }
    assert not any(key.startswith("semantic_route_") for key in metadata)


def test_heartbeat_scene_boundary_uses_only_live_scene_participants() -> None:
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            scene_manager=SimpleNamespace(
                current_scene=SimpleNamespace(
                    name="登记小室查册",
                    location="白花碑驿站·登记小室",
                    participants=["伊莉雅", "财团巡逻队"],
                )
            )
        )
    )

    boundary = FUGMHttpService._heartbeat_scene_boundary(runtime)

    assert "当前聚焦地点是【白花碑驿站·登记小室】" in boundary
    assert "当前参与者唯一名单是【伊莉雅、财团巡逻队】" in boundary
    assert "旧实录或准备候选中也视为缺席" in boundary


def test_heartbeat_request_metadata_exposes_idle_episode_to_agent() -> None:
    host = SimpleNamespace(
        _external_message_metadata=lambda _payload: {},
        _truthy=lambda value: bool(value),
    )
    coordinator = GMAgentMessageCoordinator(host)
    idle_episode = {
        "player_idle_seconds": 601,
        "nudge_count": 1,
        "nudge_limit": 2,
    }
    nudge_target = {
        "status": "targeted",
        "player": "南星",
        "topic": "mystery_contributions",
    }

    metadata = coordinator._request_metadata(
        {
            "system_gm_beat_request": True,
            "heartbeat_action": "session_zero_nudge",
            "heartbeat_idle_episode": idle_episode,
            "heartbeat_session_zero_target": nudge_target,
        },
        message="系统主动节拍",
        recent_context="玩家讨论停在铁誓教团的位置。",
    )

    assert metadata["heartbeat_idle_episode"] == idle_episode
    assert metadata["heartbeat_session_zero_target"] == nudge_target


def test_read_only_inspection_focus_survives_followups_and_load_clears_it() -> None:
    host = SimpleNamespace()
    coordinator = GMAgentMessageCoordinator(host)
    coordinator._update_inspection_focus(
        "s1",
        "group",
        [
            GMToolReceipt(
                tool_name="inspect_campaign",
                ok=True,
                result={"campaign_id": "default", "slot": ""},
            )
        ],
    )

    assert coordinator._inspection_focus("s1", "group") == {
        "campaign_id": "default",
        "slot": "",
    }

    coordinator._update_inspection_focus(
        "s1",
        "group",
        [
            GMToolReceipt(
                tool_name="get_world_state",
                ok=True,
                result={
                    "campaign_id": "1",
                    "slot": "",
                    "source": "live_runtime",
                },
            )
        ],
    )

    assert coordinator._inspection_focus("s1", "group") == {}
    coordinator._update_inspection_focus(
        "s1",
        "group",
        [
            GMToolReceipt(
                tool_name="inspect_campaign",
                ok=True,
                result={"campaign_id": "default", "slot": ""},
            )
        ],
    )
    coordinator._update_inspection_focus(
        "s1",
        "group",
        [
            GMToolReceipt(
                tool_name="load_campaign",
                ok=True,
                state_changed=True,
                result={"campaign_id": "default", "slot": ""},
            )
        ],
    )

    assert coordinator._inspection_focus("s1", "group") == {}


class _PacingAgent:
    def run(self, *_args, **_kwargs) -> GMToolAgentOutcome:
        return GMToolAgentOutcome(
            handled=True,
            reply="守门人侧身让开了道路。",
            receipts=[
                GMToolReceipt.success(
                    "commit_scene_response",
                    state_changed=True,
                    pacing_events=[
                        GMToolPacingEvent(
                            player_action=True,
                            action_summary="伊莉雅请守门人让路。",
                            local_payoff="守门人答应放行。",
                        )
                    ],
                )
            ],
            target="fu_gm",
            terminal_action="final",
        )


def _pacing_payload() -> dict[str, object]:
    return {
        "campaign_id": "pacing-persistence",
        "session_id": "session-1",
        "channel_id": "group-1",
        "speaker": "阿凛",
        "message": "伊莉雅请守门人让路。",
    }


def test_post_tool_pacing_observation_survives_restart() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _PacingAgent()
        service.session_gates.activate(
            "pacing-persistence",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "pacing-persistence",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            _pacing_payload(),
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["pacing_observation"]["meaningful_turns"] == 1
        assert response["pacing_observation"]["saved_path"]
        restarted = FUGMHttpService(data_root=root, use_llm=False)
        restored = restarted._runtime("pacing-persistence").app
        assert (
            restored.story_arc_manager.state.current_session_progress.meaningful_turns
            == 1
        )


def test_failed_pacing_autosave_rolls_back_only_pacing_observation() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _PacingAgent()
        service.session_gates.activate(
            "pacing-persistence",
            "group-1",
            "session-1",
            status="adventure",
        )
        runtime = service._runtime("pacing-persistence")
        gate = service.session_gates.get(
            "pacing-persistence",
            "group-1",
            "session-1",
        )

        with patch.object(
            service,
            "_autosave_campaign",
            side_effect=RuntimeError("pacing disk full"),
        ):
            response = service.gm_agent_message_coordinator.handle(
                _pacing_payload(),
                gate=gate,
                is_private=False,
                explicitly_addressed=True,
                recent_context="",
                record_log=False,
            )

        assert response is not None
        assert response["reply"] == "守门人侧身让开了道路。"
        assert response["pacing_observation"]["rolled_back"] is True
        assert "pacing disk full" in response["pacing_observation"]["error"]
        assert (
            runtime.app.story_arc_manager.state.current_session_progress.meaningful_turns
            == 0
        )
