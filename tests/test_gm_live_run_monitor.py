from __future__ import annotations

import threading
import time

from fu_gm.components.gm_live_run_monitor import (
    GMLiveRunMonitor,
    bind_live_run,
    emit_live_run_event,
    reset_live_run,
)


def test_active_run_is_visible_and_private_details_are_opt_in() -> None:
    monitor = GMLiveRunMonitor()
    run_id = monitor.start_run(
        campaign_id="雾港",
        session_id="s1",
        channel_id="group-1",
        conversation_turn_id="turn-1",
        message_id="message-1",
        speaker="阿凛",
        model="terra",
        timeout_seconds=60,
        max_iterations=8,
        message="替我调查钟楼。",
    )
    token = bind_live_run(monitor, run_id)
    try:
        emit_live_run_event(
            "model_output",
            phase="validating_model_output",
            iteration=1,
            summary="模型已返回完整正文。",
            public_details={"output_chars": 18},
            private_details={
                "raw_output": '{"decision":"call_tool"}',
                "tool_arguments": {"secret": "hidden"},
            },
        )
    finally:
        reset_live_run(token)

    public = monitor.snapshot(campaign_id="雾港")
    assert public["active_count"] == 1
    public_run = public["active_runs"][0]
    assert public_run["phase"] == "validating_model_output"
    assert "message_id" not in public_run
    assert "speaker" not in public_run
    event = public_run["events"][-1]
    assert event["details"] == {"output_chars": 18}
    assert "raw_output" not in str(public)
    assert "hidden" not in str(public)

    private = monitor.snapshot(campaign_id="雾港", include_private=True)
    private_run = private["active_runs"][0]
    assert private_run["message_id"] == "message-1"
    assert private_run["speaker"] == "阿凛"
    assert private_run["events"][-1]["details"]["raw_output"] == (
        '{"decision":"call_tool"}'
    )
    assert private_run["events"][-1]["details"]["tool_arguments"] == {
        "secret": "hidden"
    }


def test_context_binding_is_thread_local_and_runs_do_not_overwrite_each_other() -> None:
    monitor = GMLiveRunMonitor()
    barrier = threading.Barrier(3)

    def worker(campaign_id: str, raw: str) -> None:
        run_id = monitor.start_run(
            campaign_id=campaign_id,
            session_id="s1",
            channel_id=campaign_id,
            timeout_seconds=5,
        )
        token = bind_live_run(monitor, run_id)
        try:
            barrier.wait(timeout=2)
            emit_live_run_event(
                "model_output",
                phase="validating_model_output",
                private_details={"raw_output": raw},
            )
        finally:
            reset_live_run(token)
        monitor.finish_run(run_id, terminal_reason="completed")

    first = threading.Thread(target=worker, args=("团一", "one"))
    second = threading.Thread(target=worker, args=("团二", "two"))
    first.start()
    second.start()
    barrier.wait(timeout=2)
    first.join(timeout=2)
    second.join(timeout=2)

    first_payload = monitor.snapshot(
        campaign_id="团一",
        include_private=True,
    )
    second_payload = monitor.snapshot(
        campaign_id="团二",
        include_private=True,
    )
    assert first_payload["active_count"] == 0
    assert second_payload["active_count"] == 0
    assert "one" in str(first_payload["recent_runs"])
    assert "two" not in str(first_payload["recent_runs"])
    assert "two" in str(second_payload["recent_runs"])
    assert "one" not in str(second_payload["recent_runs"])


def test_snapshot_is_a_deep_copy_and_completed_history_is_bounded() -> None:
    monitor = GMLiveRunMonitor(completed_limit=2)
    ids = []
    for index in range(3):
        run_id = monitor.start_run(
            campaign_id="界限团",
            session_id="s1",
            channel_id="group-1",
            message_id=str(index),
        )
        ids.append(run_id)
        monitor.event(
            run_id,
            kind="tool_receipt",
            private_details={"receipt": {"result": [index]}},
        )
        monitor.finish_run(run_id, terminal_reason="completed")

    payload = monitor.snapshot(include_private=True, limit=10)
    assert [item["run_id"] for item in payload["recent_runs"]] == [
        ids[2],
        ids[1],
    ]
    payload["recent_runs"][0]["events"][1]["details"]["receipt"][
        "result"
    ].append("mutated")
    fresh = monitor.snapshot(include_private=True, limit=10)
    assert "mutated" not in str(fresh)


def test_new_channel_activity_marks_an_active_run_superseded() -> None:
    monitor = GMLiveRunMonitor()
    run_id = monitor.start_run(
        campaign_id="并发团",
        session_id="s1",
        channel_id="group-1",
        message_id="old",
        timeout_seconds=60,
    )

    assert (
        monitor.mark_superseded(
            campaign_id="并发团",
            session_id="s1",
            channel_id="group-1",
            newer_message_id="new",
        )
        == 1
    )
    run = monitor.snapshot(include_private=True)["active_runs"][0]
    assert run["run_id"] == run_id
    assert run["superseded"] is True
    assert run["health"] == "superseded"
    assert run["superseded_by"] == "new"
    assert run["events"][-1]["kind"] == "run_superseded"
    public = monitor.snapshot(include_private=False)["active_runs"][0]
    assert "newer_message_id" not in public["events"][-1]["details"]
    assert public["events"][-1]["details"] == {"superseded": True}


def test_waiting_provider_is_not_reported_stuck_before_deadline() -> None:
    monitor = GMLiveRunMonitor(stuck_grace_seconds=0.01)
    run_id = monitor.start_run(
        campaign_id="慢模型团",
        session_id="s1",
        channel_id="group-1",
        timeout_seconds=1,
    )
    monitor.event(
        run_id,
        kind="model_request_started",
        phase="requesting_model",
    )
    first = monitor.snapshot()["active_runs"][0]
    time.sleep(0.02)
    second = monitor.snapshot()["active_runs"][0]

    assert first["health"] == "waiting_provider"
    assert second["health"] == "waiting_provider"
    assert second["elapsed_ms"] >= first["elapsed_ms"]
    assert second["thread_alive"] is True


def test_observer_callback_failure_cannot_escape_to_the_transaction() -> None:
    class BrokenMonitor(GMLiveRunMonitor):
        def event(self, *_args, **_kwargs) -> None:
            raise RuntimeError("dashboard broken")

    monitor = BrokenMonitor()
    run_id = monitor.start_run(
        campaign_id="安全团",
        session_id="s1",
        channel_id="group-1",
    )
    token = bind_live_run(monitor, run_id)
    try:
        emit_live_run_event("phase", phase="requesting_model")
    finally:
        reset_live_run(token)
