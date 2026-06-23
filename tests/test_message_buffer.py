from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest


def load_buffer_class():
    path = Path(__file__).resolve().parents[1] / "integrations" / "astrbot" / "fu_gm_bridge" / "message_buffer.py"
    spec = importlib.util.spec_from_file_location("fu_gm_bridge_message_buffer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DebouncedMessageBuffer


DebouncedMessageBuffer = load_buffer_class()


class DebouncedMessageBufferTests(unittest.TestCase):
    def test_debounce_merges_consecutive_messages(self) -> None:
        async def scenario():
            buffer = DebouncedMessageBuffer(debounce_seconds=0.03, max_wait_seconds=0.2, max_messages=5)
            first = asyncio.create_task(
                buffer.add("group-1", {"speaker": "阿凛", "message": "我先观察门", "mode": "auto"})
            )
            await asyncio.sleep(0.01)
            second = await buffer.add("group-1", {"speaker": "白河", "message": "等等，先别碰宝箱", "mode": "auto"})
            result = await first
            self.assertIsNone(second)
            self.assertEqual(result.payload["batch_count"], 2)
            self.assertIn("阿凛：我先观察门", result.payload["message"])
            self.assertIn("白河：等等，先别碰宝箱", result.payload["message"])

        asyncio.run(scenario())

    def test_reaches_max_messages_without_waiting_for_quiet_window(self) -> None:
        async def scenario():
            buffer = DebouncedMessageBuffer(debounce_seconds=1.0, max_wait_seconds=2.0, max_messages=3)
            first = asyncio.create_task(buffer.add("group-1", {"speaker": "阿凛", "message": "第一句"}))
            await asyncio.sleep(0)
            await buffer.add("group-1", {"speaker": "白河", "message": "第二句"})
            await buffer.add("group-1", {"speaker": "澪", "message": "第三句"})
            result = await asyncio.wait_for(first, timeout=0.2)
            self.assertEqual(result.payload["batch_count"], 3)
            self.assertEqual(len(result.payload["batch_messages"]), 3)

        asyncio.run(scenario())

    def test_single_message_keeps_original_payload(self) -> None:
        async def scenario():
            buffer = DebouncedMessageBuffer(debounce_seconds=0.01, max_wait_seconds=0.05, max_messages=5)
            result = await buffer.add("group-1", {"speaker": "阿凛", "message": "我调查宝箱", "mode": "auto"})
            self.assertEqual(result.payload["batch_count"], 1)
            self.assertEqual(result.payload["speaker"], "阿凛")
            self.assertEqual(result.payload["message"], "我调查宝箱")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
