from __future__ import annotations

from types import SimpleNamespace

from fu_gm.components.gm_tool_pacing_observer import GMToolPacingObserver
from fu_gm.gm_tool_contracts import (
    GMToolExecutionContext,
    GMToolPacingEvent,
    GMToolReceipt,
)


class RecordingPacingManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def observe_turn(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            session_number=3,
            meaningful_turns=7,
            stage="development",
            closure_ready=False,
        )


def _context() -> GMToolExecutionContext:
    return GMToolExecutionContext(
        campaign_id="campaign",
        session_id="session",
        channel_id="group",
        speaker="阿凛",
        gate_status="adventure",
        metadata={"current_message": "伊莉雅请会长开门，然后走进旧路。"},
    )


def test_observer_merges_multiple_tool_events_into_one_player_turn() -> None:
    manager = RecordingPacingManager()
    resource_tracker = SimpleNamespace(observe=lambda _progress: None)
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            campaign_pacing_manager=manager,
            session_episode_tracker=SimpleNamespace(resource_tracker=resource_tracker),
        )
    )
    receipts = [
        GMToolReceipt.success(
            "decide_npc_response",
            state_changed=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary="伊莉雅请会长开门。",
                    local_payoff="会长答应开放旧路。",
                )
            ],
        ),
        GMToolReceipt.success(
            "transition_scene",
            state_changed=True,
            pacing_events=[
                GMToolPacingEvent(
                    player_action=True,
                    action_summary="伊莉雅走进旧路。",
                    public_image="门后是潮湿的石阶。",
                )
            ],
        ),
    ]

    audit = GMToolPacingObserver().observe(runtime, _context(), receipts)

    assert len(manager.calls) == 1
    assert manager.calls[0]["player_action"] is True
    assert manager.calls[0]["action_summary"] == "伊莉雅请会长开门。"
    assert manager.calls[0]["local_payoff"] == "会长答应开放旧路。"
    assert manager.calls[0]["public_image"] == "门后是潮湿的石阶。"
    assert audit["event_count"] == 2


def test_observer_ignores_failed_receipts_and_empty_events() -> None:
    manager = RecordingPacingManager()
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            campaign_pacing_manager=manager,
            session_episode_tracker=SimpleNamespace(resource_tracker=None),
        )
    )
    receipts = [
        GMToolReceipt(
            tool_name="change_clock",
            ok=False,
            pacing_events=[GMToolPacingEvent(consequence="绝不能记录")],
        ),
        GMToolReceipt.success("get_clocks"),
    ]

    audit = GMToolPacingObserver().observe(runtime, _context(), receipts)

    assert audit == {}
    assert manager.calls == []


def test_receipt_serializes_typed_pacing_events_for_audit() -> None:
    receipt = GMToolReceipt.success(
        "commit_scene_response",
        state_changed=True,
        pacing_events=[
            GMToolPacingEvent(
                opposition_move="巡逻队封住了北门。",
                gm_beat_purpose="opposition_move",
            )
        ],
    )

    payload = receipt.to_dict()

    assert payload["pacing_events"] == [
        {
            "player_action": False,
            "action_summary": "",
            "consequence": "",
            "local_payoff": "",
            "reveal": "",
            "reversal": False,
            "climax": "",
            "opposition_move": "巡逻队封住了北门。",
            "public_image": "",
            "local_question_changed": False,
            "local_question_resolved": False,
            "deliberate_cliffhanger": False,
            "signature_image_evolved": False,
            "callback_to_previous": "",
            "gm_beat_purpose": "opposition_move",
        }
    ]


def test_observer_promotes_direct_action_round_pressure_fulfillment() -> None:
    manager = RecordingPacingManager()
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            campaign_pacing_manager=manager,
            session_episode_tracker=SimpleNamespace(resource_tracker=None),
        )
    )
    receipt = GMToolReceipt.success(
        "pass_in_scene_action",
        state_changed=True,
        result={
            "action_round": {
                "settled_pressure_clocks": [
                    {
                        "clock_name": "财团巡逻队逼近",
                        "clock_type": "threat",
                        "consequence": "财团巡逻队包围白花碑驿站。",
                        "status": "resolved",
                    }
                ]
            }
        },
    )

    audit = GMToolPacingObserver().observe(runtime, _context(), [receipt])

    assert audit["event_count"] == 1
    assert manager.calls[0]["player_action"] is False
    assert manager.calls[0]["reversal"] is True
    assert manager.calls[0]["local_question_changed"] is True
    assert manager.calls[0]["opposition_move"] == "财团巡逻队包围白花碑驿站。"


def test_required_ending_echo_records_signature_image_evolution() -> None:
    manager = RecordingPacingManager()
    runtime = SimpleNamespace(
        app=SimpleNamespace(
            campaign_pacing_manager=manager,
            session_episode_tracker=SimpleNamespace(resource_tracker=None),
        )
    )
    context = _context()
    context.metadata.update(
        {
            "system_gm_beat_request": True,
            "heartbeat_require_signature_image_evolution": True,
        }
    )
    receipt = GMToolReceipt.success(
        "commit_scene_response",
        state_changed=True,
        pacing_events=[
            GMToolPacingEvent(
                public_image="排水沟里的银白残光逐渐暗下，只剩一线冷光。",
                gm_beat_purpose="aftermath",
            )
        ],
    )

    GMToolPacingObserver().observe(runtime, context, [receipt])

    assert manager.calls[0]["signature_image_evolved"] is True
