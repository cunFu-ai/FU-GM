from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest


def load_delivery_module():
    path = Path(__file__).resolve().parents[1] / "integrations" / "astrbot" / "fu_gm_bridge" / "delivery.py"
    spec = importlib.util.spec_from_file_location("fu_gm_bridge_delivery", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


delivery = load_delivery_module()
reply_delivery_specs = delivery.reply_delivery_specs
ReplyDeliveryCoordinator = delivery.ReplyDeliveryCoordinator


class MemoryJournal:
    def __init__(self) -> None:
        self.sent: set[str] = set()

    def was_sent(self, delivery_id: str) -> bool:
        return delivery_id in self.sent

    def mark_sent(self, delivery_id: str) -> bool:
        self.sent.add(delivery_id)
        return True

    def mark_confirmed(self, delivery_id: str) -> bool:
        self.sent.discard(delivery_id)
        return True


class AstrBotDeliveryTests(unittest.TestCase):
    def test_envelopes_preserve_exact_reply_targets(self) -> None:
        specs = reply_delivery_specs(
            {
                "reply": "合并后的兼容文本",
                "reply_envelopes": [
                    {
                        "envelope_id": "r1",
                        "text": "白河先看清了车辙。",
                        "quote": True,
                        "target_message_id": "m-10",
                        "target_speaker": "白河",
                    },
                    {
                        "envelope_id": "r2",
                        "text": "阿凛的符文检定随后结算。",
                        "quote": True,
                        "target_message_id": "m-11",
                        "target_speaker": "阿凛",
                    },
                ],
            }
        )

        self.assertEqual([item["target_message_id"] for item in specs], ["m-10", "m-11"])
        self.assertTrue(all(item["quote"] for item in specs))

    def test_proactive_and_plain_replies_are_not_quoted(self) -> None:
        proactive = reply_delivery_specs(
            {
                "reply_envelopes": [
                    {"envelope_id": "r3", "text": "铁靴声更近了。", "quote": False, "target_message_id": ""}
                ]
            }
        )
        plain = reply_delivery_specs({"reply": "普通接口回复"})

        self.assertFalse(proactive[0]["quote"])
        self.assertFalse(plain[0]["quote"])
        self.assertEqual(plain[0]["kind"], "gm_reply")

    def test_duplicate_envelopes_are_emitted_once(self) -> None:
        specs = reply_delivery_specs(
            {
                "reply_envelopes": [
                    {"envelope_id": "same", "text": "只发一次", "quote": True, "target_message_id": "m1"},
                    {"envelope_id": "same", "text": "只发一次", "quote": True, "target_message_id": "m1"},
                ]
            }
        )
        self.assertEqual(len(specs), 1)

    def test_map_image_is_attached_to_the_exact_reply(self) -> None:
        specs = reply_delivery_specs(
            {
                "reply": "地图画好了。",
                "reply_media": [
                    {
                        "type": "image",
                        "path": "/tmp/world-map.png",
                        "url": "",
                        "alt": "世界地图",
                    }
                ],
                "reply_envelopes": [
                    {
                        "envelope_id": "map-1",
                        "text": "地图画好了。",
                        "quote": True,
                        "target_message_id": "m-map",
                        "metadata": {},
                    }
                ],
            }
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["target_message_id"], "m-map")
        self.assertEqual(specs[0]["media"][0]["path"], "/tmp/world-map.png")

    def test_confirmation_failure_does_not_resend_same_envelope(self) -> None:
        async def scenario() -> None:
            journal = MemoryJournal()
            coordinator = ReplyDeliveryCoordinator(journal)
            sends: list[str] = []
            confirmations: list[str] = []
            specs = [{"envelope_id": "reply-1", "text": "只发送一次"}]

            async def send(result: str) -> None:
                sends.append(result)

            async def fail_confirm(envelope_id: str) -> bool:
                confirmations.append(envelope_id)
                return False

            assert await coordinator.deliver(
                specs,
                ["result"],
                already_confirmed=False,
                send=send,
                confirm=fail_confirm,
            )
            assert journal.was_sent("reply-1")
            assert await coordinator.deliver(
                specs,
                ["result"],
                already_confirmed=False,
                send=send,
                confirm=fail_confirm,
            )
            self.assertEqual(sends, ["result"])
            self.assertEqual(confirmations, ["reply-1", "reply-1"])

        asyncio.run(scenario())

    def test_restart_recovery_confirms_without_resending(self) -> None:
        async def scenario() -> None:
            journal = MemoryJournal()
            journal.mark_sent("reply-restart")
            coordinator = ReplyDeliveryCoordinator(journal)
            confirmations: list[str] = []

            async def confirm(envelope_id: str) -> bool:
                confirmations.append(envelope_id)
                return True

            confirmed = await coordinator.recover(confirm)
            self.assertEqual(confirmed, ["reply-restart"])
            self.assertEqual(confirmations, ["reply-restart"])
            self.assertFalse(journal.was_sent("reply-restart"))

        asyncio.run(scenario())

    def test_successful_confirmation_clears_unconfirmed_journal(self) -> None:
        async def scenario() -> None:
            journal = MemoryJournal()
            coordinator = ReplyDeliveryCoordinator(journal)

            async def send(_result: str) -> None:
                return None

            async def confirm(_envelope_id: str) -> bool:
                return True

            assert await coordinator.deliver(
                [{"envelope_id": "reply-2", "text": "确认"}],
                ["result"],
                already_confirmed=False,
                send=send,
                confirm=confirm,
            )
            self.assertFalse(journal.was_sent("reply-2"))

        asyncio.run(scenario())

    def test_media_only_fallback_is_deliverable(self) -> None:
        specs = reply_delivery_specs(
            {
                "reply_media": [
                    {
                        "type": "image",
                        "url": "https://example.invalid/map.png",
                    }
                ]
            }
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["text"], "")
        self.assertEqual(
            specs[0]["media"][0]["url"],
            "https://example.invalid/map.png",
        )


if __name__ == "__main__":
    unittest.main()
