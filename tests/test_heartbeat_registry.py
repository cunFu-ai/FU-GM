from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys


def _load_module():
    path = Path(__file__).resolve().parents[1] / "integrations" / "astrbot" / "fu_gm_bridge" / "heartbeat.py"
    spec = importlib.util.spec_from_file_location("fu_gm_bridge_heartbeat", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


heartbeat = _load_module()


def test_new_activity_can_cancel_a_pending_heartbeat() -> None:
    async def scenario() -> None:
        registry = heartbeat.HeartbeatTaskRegistry()
        reached_send = False

        async def pending() -> None:
            nonlocal reached_send
            await asyncio.sleep(1)
            reached_send = True

        assert registry.start("group-1", pending)
        assert registry.running("group-1")
        assert registry.cancel("group-1")
        await asyncio.sleep(0)
        assert not reached_send
        assert not registry.running("group-1")
        await registry.close()

    asyncio.run(scenario())


def test_only_one_heartbeat_runs_per_channel_while_channels_are_independent() -> None:
    async def scenario() -> None:
        registry = heartbeat.HeartbeatTaskRegistry()
        gate = asyncio.Event()

        async def pending() -> None:
            await gate.wait()

        assert registry.start("group-1", pending)
        assert not registry.start("group-1", pending)
        assert registry.start("group-2", pending)
        assert registry.running("group-1")
        assert registry.running("group-2")
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await registry.close()

    asyncio.run(scenario())


def test_late_uncommitted_heartbeat_can_be_dropped() -> None:
    assert not heartbeat.heartbeat_committed_state_change(
        {
            "send_reply": True,
            "tool_receipts": [
                {
                    "tool_name": "get_scene_state",
                    "ok": True,
                    "state_changed": False,
                }
            ],
        }
    )


def test_late_committed_heartbeat_must_still_be_delivered() -> None:
    assert heartbeat.heartbeat_committed_state_change(
        {
            "send_reply": True,
            "tool_receipts": [
                {
                    "tool_name": "run_current_npc_turn",
                    "ok": True,
                    "state_changed": True,
                }
            ],
        }
    )


def test_sent_delivery_journal_survives_restart_until_confirmation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "heartbeat_sent_unconfirmed.json"
    first = heartbeat.HeartbeatDeliveryJournal(path)

    assert first.mark_sent("delivery-1")
    assert first.was_sent("delivery-1")

    restarted = heartbeat.HeartbeatDeliveryJournal(path)
    assert restarted.was_sent("delivery-1")
    assert restarted.mark_confirmed("delivery-1")

    restarted_again = heartbeat.HeartbeatDeliveryJournal(path)
    assert not restarted_again.was_sent("delivery-1")
