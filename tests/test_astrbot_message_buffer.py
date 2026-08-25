from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys


def _load_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "message_buffer.py"
    )
    spec = importlib.util.spec_from_file_location(
        "fu_gm_bridge_message_buffer",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


message_buffer = _load_module()


def test_empty_adapter_event_is_not_meaningful_activity() -> None:
    assert message_buffer.has_meaningful_message_activity("", {}) is False
    assert (
        message_buffer.has_meaningful_message_activity(
            "   ",
            {"segment_types": ["ComponentType.Plain"]},
        )
        is False
    )


def test_text_addressing_and_attachments_are_meaningful_activity() -> None:
    assert message_buffer.has_meaningful_message_activity("成", {}) is True
    assert (
        message_buffer.has_meaningful_message_activity("", {"is_at_bot": True})
        is True
    )
    assert (
        message_buffer.has_meaningful_message_activity(
            "",
            {"attachments": [{"type": "image", "file": "scene.png"}]},
        )
        is True
    )


def test_direct_message_can_discard_unsubmitted_passive_batch() -> None:
    async def scenario() -> None:
        buffer = message_buffer.DebouncedMessageBuffer(
            debounce_seconds=0.2,
            max_wait_seconds=1,
        )
        task = asyncio.create_task(
            buffer.add(
                "group-1",
                {"speaker": "甲", "message": "先商量一下"},
            )
        )
        await asyncio.sleep(0)
        assert buffer.pending_count("group-1") == 1
        assert buffer.discard("group-1") is True
        assert await task is None
        assert buffer.pending_count("group-1") == 0

    asyncio.run(scenario())


def test_debounce_batch_preserves_each_speaker_and_does_not_choose_first_actor() -> None:
    async def scenario() -> None:
        buffer = message_buffer.DebouncedMessageBuffer(
            debounce_seconds=0.05,
            max_wait_seconds=0.5,
            max_messages=3,
        )
        first = asyncio.create_task(
            buffer.add(
                "group-1",
                {
                    "speaker": "阿凛",
                    "speaker_id": "user-a",
                    "message": "伊莉雅守住门口。",
                    "message_id": "message-a",
                    "activity_version": 4,
                },
            )
        )
        await asyncio.sleep(0)
        assert await buffer.add(
            "group-1",
            {
                "speaker": "白河",
                "speaker_id": "user-b",
                "message": "洛岚去检查闸门。",
                "message_id": "message-b",
                "activity_version": 5,
            },
        ) is None
        assert await buffer.add(
            "group-1",
            {
                "speaker": "南星",
                "speaker_id": "user-c",
                "message": "赛璃准备治疗。",
                "message_id": "message-c",
                "activity_version": 6,
            },
        ) is None

        collapsed = await first
        assert collapsed is not None
        assert collapsed.payload["speaker"] == "多人发言"
        assert [
            item["speaker"] for item in collapsed.payload["batch_messages"]
        ] == ["阿凛", "白河", "南星"]
        assert [
            item["payload"]["speaker_id"]
            for item in collapsed.payload["batch_messages"]
        ] == ["user-a", "user-b", "user-c"]
        assert "1. 阿凛：伊莉雅守住门口。" in collapsed.payload["message"]
        assert "2. 白河：洛岚去检查闸门。" in collapsed.payload["message"]
        assert "3. 南星：赛璃准备治疗。" in collapsed.payload["message"]
        assert collapsed.payload["activity_version"] == 6
        assert collapsed.payload["activity_token"].startswith("batch:")
        assert collapsed.payload["activity_members"] == [
            {
                "speaker": "阿凛",
                "speaker_id": "user-a",
                "activity_version": 4,
                "message_id": "message-a",
            },
            {
                "speaker": "白河",
                "speaker_id": "user-b",
                "activity_version": 5,
                "message_id": "message-b",
            },
            {
                "speaker": "南星",
                "speaker_id": "user-c",
                "activity_version": 6,
                "message_id": "message-c",
            },
        ]

    asyncio.run(scenario())
