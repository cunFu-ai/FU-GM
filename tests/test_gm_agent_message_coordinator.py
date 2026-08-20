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

    assert "当前聚焦地点：【白花碑驿站·登记小室】" in boundary
    assert "当前可自主行动的场景主体：【伊莉雅、财团巡逻队】" in boundary
    assert "旧实录" not in boundary


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


class _SequentialOutcomeAgent:
    def __init__(self, outcomes: list[GMToolAgentOutcome]) -> None:
        self.outcomes = list(outcomes)

    def run(self, *_args, **_kwargs) -> GMToolAgentOutcome:
        return self.outcomes.pop(0)


class _MetadataCaptureAgent:
    timeout_seconds = 30.0

    def __init__(self) -> None:
        self.metadata: dict[str, object] = {}

    def run(self, *_args, **kwargs) -> GMToolAgentOutcome:
        self.metadata = dict(kwargs["context"].metadata)
        return GMToolAgentOutcome(
            handled=True,
            reply="",
            target="silent",
            mode="gm_agent_silent",
            terminal_action="silent",
        )


class _DraftRewriteExpressor:
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.last_agent_message_metadata: dict[str, object] = {}

    def render_agent_message(self, draft_parts: list[str], **kwargs: object) -> list[str]:
        self.calls.append({"draft_parts": list(draft_parts), **kwargs})
        self.last_agent_message_metadata = {
            "author": "expressor",
            "model": self.model,
            "used_fallback": False,
        }
        return ["DeepSeek写出的最终公开消息。" for _ in draft_parts]


class _FailingDraftExpressor:
    model = "deepseek-v4-flash"

    def render_agent_message(self, _draft_parts: list[str], **_kwargs: object) -> list[str]:
        raise RuntimeError("DeepSeek expression unavailable")


def _pacing_payload() -> dict[str, object]:
    return {
        "campaign_id": "pacing-persistence",
        "session_id": "session-1",
        "channel_id": "group-1",
        "speaker": "阿凛",
        "message": "伊莉雅请守门人让路。",
    }


def test_adventure_without_scene_recovers_complete_opening_prep_metadata() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        agent = _MetadataCaptureAgent()
        service.gm_tool_agent = agent
        service.session_gates.activate(
            "opening-recovery",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "opening-recovery",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "opening-recovery",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "时悠，请从第一章开场。",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="大家已经完成第零章。",
            record_log=False,
        )

        assert response is not None
        assert agent.metadata["opening_scene_requires_complete_prep"] is True


def test_direct_core_reply_is_published_without_outer_expressor() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="核心GM给出的最终公开消息。",
                    target="fu_gm",
                    mode="gm_agent_reply",
                    terminal_action="final",
                    trace=[{"message_kind": "gm_request"}],
                )
            ]
        )
        runtime = service._runtime("expression-owner")
        expressor = _DraftRewriteExpressor()
        runtime.app.expressor = expressor
        service.session_gates.activate(
            "expression-owner",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "expression-owner",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "expression-owner",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "牢房外有什么？",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="刚才有人听见钥匙声。",
            record_log=False,
        )

        assert response is not None
        assert response["reply"] == "核心GM给出的最终公开消息。"
        assert response["public_expression"] == {
            "attempted": False,
            "author": "core_gm",
            "model": service.gm_agent_runtime.llm_model,
            "merged_into_core": True,
            "input_parts": 1,
            "output_parts": 1,
            "expression_mode": "core",
        }
        assert expressor.calls == []


def test_explicit_legacy_expressor_mode_still_rewrites_successfully() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(
            data_root=root,
            use_llm=False,
            public_expression_mode="expressor",
        )
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="核心GM给出的兼容模式输入。",
                    target="fu_gm",
                    mode="gm_agent_reply",
                    terminal_action="final",
                    trace=[{"message_kind": "gm_request"}],
                )
            ]
        )
        runtime = service._runtime("legacy-expression-success")
        expressor = _DraftRewriteExpressor()
        runtime.app.expressor = expressor
        service.session_gates.activate(
            "legacy-expression-success",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "legacy-expression-success",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "legacy-expression-success",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "牢房外有什么？",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["reply"] == "DeepSeek写出的最终公开消息。"
        assert response["public_expression"]["attempted"] is True
        assert response["public_expression"]["author"] == "expressor"
        assert response["public_expression"]["expression_mode"] == "expressor"
        assert len(expressor.calls) == 1


def test_non_locked_tool_final_is_published_without_outer_expressor() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="检查完成，当前没有总控告警。",
                    receipts=[GMToolReceipt.success("inspect_supervisor")],
                    target="fu_gm",
                    mode="gm_agent_tool",
                    terminal_action="final",
                )
            ]
        )
        runtime = service._runtime("tool-final-core-author")
        expressor = _DraftRewriteExpressor()
        runtime.app.expressor = expressor
        service.session_gates.activate(
            "tool-final-core-author",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "tool-final-core-author",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "tool-final-core-author",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "检查总控状态。",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["reply"] == "检查完成，当前没有总控告警。"
        assert response["public_expression"]["attempted"] is False
        assert response["public_expression"]["author"] == "core_gm"
        assert response["public_expression"]["merged_into_core"] is True
        assert expressor.calls == []


def test_locked_focused_reply_bypasses_general_expressor() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        locked_reply = "检定已经由规则表达器写好。"
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply=locked_reply,
                    receipts=[
                        GMToolReceipt.success(
                            "perform_character_action",
                            public_reply=locked_reply,
                            lock_public_reply=True,
                        )
                    ],
                    target="fu_gm",
                    mode="gm_agent_tool",
                    terminal_action="final",
                )
            ]
        )
        runtime = service._runtime("focused-author")
        expressor = _DraftRewriteExpressor()
        runtime.app.expressor = expressor
        service.session_gates.activate(
            "focused-author",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "focused-author",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "focused-author",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "我攻击守卫。",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["reply"] == locked_reply
        assert response["public_expression"]["author"] == "focused_component"
        assert expressor.calls == []


def test_core_publication_preserves_explicit_message_parts() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="先回答问题。\n再描述现场。",
                    reply_parts=["先回答问题。", "再描述现场。"],
                    target="fu_gm",
                    mode="gm_agent_reply",
                    terminal_action="final",
                )
            ]
        )
        runtime = service._runtime("multipart-expression")
        expressor = _DraftRewriteExpressor()
        runtime.app.expressor = expressor
        service.session_gates.activate(
            "multipart-expression",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "multipart-expression",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "multipart-expression",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "回答我和艾丽妮是否同牢，再处理开锁机会。",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["reply_parts"] == ["先回答问题。", "再描述现场。"]
        assert response["reply"] == "先回答问题。\n再描述现场。"
        assert response["public_expression"]["attempted"] is False
        assert response["public_expression"]["author"] == "core_gm"
        assert response["public_expression"]["merged_into_core"] is True
        assert response["public_expression"]["input_parts"] == 2
        assert response["public_expression"]["output_parts"] == 2
        assert expressor.calls == []


def test_explicit_legacy_expressor_failure_falls_back_to_core_reply() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(
            data_root=root,
            use_llm=False,
            public_expression_mode="expressor",
        )
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="核心GM给出的受约束公开回复。",
                    target="fu_gm",
                    mode="gm_agent_reply",
                    terminal_action="final",
                )
            ]
        )
        runtime = service._runtime("expression-degradation")
        runtime.app.expressor = _FailingDraftExpressor()
        service.session_gates.activate(
            "expression-degradation",
            "group-1",
            "session-1",
            status="adventure",
        )
        gate = service.session_gates.get(
            "expression-degradation",
            "group-1",
            "session-1",
        )

        response = service.gm_agent_message_coordinator.handle(
            {
                "campaign_id": "expression-degradation",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "阿凛",
                "message": "牢房外有什么？",
            },
            gate=gate,
            is_private=False,
            explicitly_addressed=True,
            recent_context="",
            record_log=False,
        )

        assert response is not None
        assert response["reply"] == "核心GM给出的受约束公开回复。"
        assert response["public_expression"]["author"] == "core_gm_degraded_fallback"
        assert response["public_expression"]["used_fallback"] is True
        assert response["public_expression"]["expression_mode"] == "expressor"
        assert "DeepSeek expression unavailable" in response["public_expression"]["error"]


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


def test_uncommitted_provider_failure_is_audited_without_polluting_story_context() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="模型服务暂时不可用，请稍后重试。",
                    target="fu_gm",
                    mode="gm_agent_unavailable",
                    error="LLM HTTP 429: rate limit",
                ),
                GMToolAgentOutcome(
                    handled=True,
                    reply="牢门上的符文随雨滴明灭。",
                    target="fu_gm",
                    mode="gm_agent_tool",
                    terminal_action="final",
                ),
            ]
        )
        base_payload = {
            "campaign_id": "retry-context",
            "session_id": "session-1",
            "channel_id": "group-1",
            "speaker": "loading",
            "speaker_id": "player-1",
            "message": "艾丽妮观察牢门符文。",
            "is_at_bot": True,
            "logical_source_event_id": "logical:observe-rune",
        }

        status, failed = service.handle(
            "POST",
            "/v1/message/route",
            {**base_payload, "message_id": "delivery-1"},
        )

        assert status == 200
        assert failed["audit_log_isolated"] is True
        assert failed["retry_safe"] is True
        assert failed["provider_error_category"] == "rate_limit"
        assert failed["agent_error_category"] == ""
        runtime = service._runtime("retry-context")
        assert runtime.log_manager.load_transcript("retry-context", "session-1") == []
        diagnostics = runtime.log_manager.load_provider_failures(
            "retry-context",
            "session-1",
        )
        assert diagnostics[-1]["source_event_id"] == "logical:observe-rune"
        assert diagnostics[-1]["message_id"] == "delivery-1"
        assert diagnostics[-1]["error_category"] == "rate_limit"

        status, recovered = service.handle(
            "POST",
            "/v1/message/route",
            {
                **base_payload,
                "message_id": "delivery-2",
                "retry_attempt": 1,
                "retry_reason": "provider_unavailable",
            },
        )

        assert status == 200
        assert recovered["audit_log_isolated"] is False
        entries = runtime.log_manager.load_transcript(
            "retry-context",
            "session-1",
        )
        assert [entry.role for entry in entries] == ["user", "assistant"]
        assert entries[0].content == "艾丽妮观察牢门符文。"
        source_events = entries[0].metadata["current_turn_events"]
        assert source_events[0]["event_id"] == "logical:observe-rune"
        assert source_events[0]["delivery_event_id"].endswith("delivery-2")


def test_uncommitted_semantic_rejection_is_not_labelled_as_provider_failure() -> None:
    with tempfile.TemporaryDirectory() as root:
        service = FUGMHttpService(data_root=root, use_llm=False)
        service.gm_tool_agent = _SequentialOutcomeAgent(
            [
                GMToolAgentOutcome(
                    handled=True,
                    reply="本轮仍未通过事实一致性审校；状态没有改变。",
                    target="fu_gm",
                    mode="gm_agent_unresolved",
                    error=(
                        "GM工具循环达到最大次数。；"
                        "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
                    ),
                    trace=[
                        {
                            "iteration": 8,
                            "protocol_error": (
                                "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
                            ),
                        }
                    ],
                    loop_diagnostics={
                        "terminal_reason": "iteration_exhausted",
                    },
                )
            ]
        )

        status, failed = service.handle(
            "POST",
            "/v1/message/route",
            {
                "campaign_id": "semantic-rejection-context",
                "session_id": "session-1",
                "channel_id": "group-1",
                "speaker": "小澜",
                "speaker_id": "player-2",
                "message_id": "semantic-rejection-1",
                "message": "星澜用双武器攻击赤炉大将。",
                "is_at_bot": True,
            },
        )

        assert status == 200
        assert failed["audit_log_isolated"] is True
        assert failed["retry_safe"] is True
        assert failed["provider_error_category"] == ""
        assert (
            failed["agent_error_category"]
            == "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
        )
        runtime = service._runtime("semantic-rejection-context")
        assert runtime.log_manager.load_transcript(
            "semantic-rejection-context",
            "session-1",
        ) == []
        diagnostics = runtime.log_manager.load_provider_failures(
            "semantic-rejection-context",
            "session-1",
        )
        assert (
            diagnostics[-1]["error_category"]
            == "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
        )
