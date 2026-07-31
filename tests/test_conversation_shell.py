from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fu_gm.conversation import (
    MessageEvent,
    ReplyEnvelope,
    ReplyLedger,
    TablePresenceScheduler,
    plan_resolution_speech,
)
from fu_gm.gm_tool_agent import GMToolAgentOutcome
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import Action, ActionResolution, ActionType, RollOutcome
from fu_gm.session_gate import SessionGateState


class MessageEventTests(unittest.TestCase):
    def test_platform_message_id_is_stable_within_campaign(self) -> None:
        payload = {
            "campaign_id": "白钟大陆",
            "session_id": "main",
            "channel_id": "group-1",
            "message_id": "991",
            "speaker": "阿凛",
            "speaker_id": "10001",
            "message": "@时悠 我调查风铃。",
            "is_at_bot": True,
        }
        first = MessageEvent.from_payload(payload)
        second = MessageEvent.from_payload(payload)
        other_campaign = MessageEvent.from_payload({**payload, "campaign_id": "另一团"})

        self.assertEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.event_id, other_campaign.event_id)
        self.assertTrue(first.directly_addresses_gm)

    def test_adapter_timestamp_stabilizes_event_when_message_id_is_missing(self) -> None:
        payload = {
            "campaign_id": "白钟大陆",
            "session_id": "main",
            "channel_id": "group-1",
            "speaker": "阿凛",
            "speaker_id": "10001",
            "message": "我检查风铃。",
            "astrbot_context": {"timestamp": "1785354000"},
        }

        first = MessageEvent.from_payload(payload)
        second = MessageEvent.from_payload(payload)

        self.assertEqual(first.event_id, second.event_id)

    def test_reply_envelope_quotes_group_message_but_not_proactive_beat(self) -> None:
        event = MessageEvent.from_payload(
            {
                "campaign_id": "白钟大陆",
                "session_id": "main",
                "channel_id": "group-1",
                "message_id": "992",
                "speaker": "白河",
                "message": "我检查旧钟。",
            }
        )
        reply = ReplyEnvelope.create(event, "钟摆背面刻着新鲜划痕。")
        proactive = ReplyEnvelope.proactive(
            campaign_id="白钟大陆",
            session_id="main",
            channel_id="group-1",
            text="门外的铁靴声又近了一层。",
        )

        self.assertTrue(reply.quote)
        self.assertEqual(reply.target_message_id, "992")
        self.assertFalse(proactive.quote)
        self.assertFalse(proactive.target_message_id)


class ReplyLedgerTests(unittest.TestCase):
    def test_failed_reply_persistence_keeps_delivery_and_flushes_before_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ReplyLedger(tmpdir)
            event = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "persist-failure",
                    "speaker": "阿凛",
                    "message": "时悠，记一下。",
                }
            )
            ledger.register_event(event)
            envelope = ReplyEnvelope.create(event, "记下了。")

            with patch.object(
                ledger,
                "_append_record",
                side_effect=OSError("disk busy"),
            ):
                ledger.record_reply(envelope)

            self.assertTrue(ledger.has_replied_to(event.event_id))
            self.assertEqual(ledger.latest_reply_for_event(event.event_id), envelope)
            self.assertEqual(ledger.persistence_status()["pending_records"], 1)

            followup = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "persist-recovery",
                    "speaker": "阿凛",
                    "message": "继续。",
                }
            )
            ledger.register_event(followup)

            self.assertTrue(ledger.persistence_status()["ok"])
            restored = ReplyLedger(tmpdir)
            self.assertTrue(
                restored.has_event(
                    event.event_id,
                    campaign_id="白钟大陆",
                )
            )
            self.assertEqual(
                restored.latest_reply_for_event(event.event_id).text,
                "记下了。",
            )

    def test_failed_initial_ledger_read_is_not_treated_as_an_empty_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "existing-event",
                    "speaker": "阿凛",
                    "message": "推进旧路。",
                }
            )
            ReplyLedger(tmpdir).register_event(event)
            restored = ReplyLedger(tmpdir)

            with patch.object(
                Path,
                "read_text",
                side_effect=OSError("ledger read unavailable"),
            ):
                with self.assertRaises(OSError):
                    restored.has_event(
                        event.event_id,
                        campaign_id="白钟大陆",
                    )

            self.assertTrue(
                restored.has_event(
                    event.event_id,
                    campaign_id="白钟大陆",
                )
            )

    def test_ledger_tracks_reply_and_followup_without_storing_hidden_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ReplyLedger(tmpdir)
            event = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "100",
                    "speaker": "阿凛",
                    "speaker_id": "u1",
                    "message": "时悠，钟声是什么意思？",
                }
            )
            ledger.register_event(event)
            envelope = ReplyEnvelope.create(event, "第三声钟响代表旧路即将关闭。")
            ledger.record_reply(envelope)
            followup = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "101",
                    "speaker": "阿凛",
                    "speaker_id": "u1",
                    "message": "那我去卡住闸门。",
                }
            )
            ledger.register_event(followup)

            self.assertTrue(ledger.has_replied_to(event.event_id))
            self.assertEqual(ledger.latest_reply_for_event(event.event_id), envelope)
            snapshot = ledger.snapshot("白钟大陆", "main", "group-1")
            self.assertEqual(snapshot["message_count"], 2)
            self.assertEqual(snapshot["reply_count"], 1)
            records = [
                json.loads(line)
                for line in (Path(tmpdir) / "白钟大陆" / "conversation" / "reply_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(any(record["record_type"] == "reply_followup" for record in records))

    def test_ledger_restores_events_replies_and_outcomes_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = ReplyLedger(tmpdir)
            replied_event = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "persisted-reply",
                    "speaker": "阿凛",
                    "message": "时悠，存档。",
                }
            )
            silent_event = MessageEvent.from_payload(
                {
                    "campaign_id": "白钟大陆",
                    "session_id": "main",
                    "channel_id": "group-1",
                    "message_id": "persisted-silent",
                    "speaker": "白河",
                    "message": "我们先商量一下。",
                }
            )
            first.register_event(replied_event)
            first.record_reply(ReplyEnvelope.create(replied_event, "存好了。"))
            first.register_event(silent_event)
            first.mark_outcome(silent_event, "silent")

            restored = ReplyLedger(tmpdir)

            self.assertTrue(
                restored.has_event(
                    replied_event.event_id,
                    campaign_id="白钟大陆",
                )
            )
            self.assertEqual(
                restored.latest_reply_for_event(replied_event.event_id).text,
                "存好了。",
            )
            self.assertEqual(
                restored.outcome_for_event(silent_event.event_id),
                "silent",
            )


class TablePresenceSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = TablePresenceScheduler()
        self.thresholds = {
            "pre_session": 600,
            "session_zero": 600,
            "adventure": 240,
            "pc_turn": 300,
            "npc_turn": 45,
        }

    def test_direct_call_is_mandatory(self) -> None:
        event = MessageEvent.from_payload(
            {
                "message_id": "1",
                "speaker": "阿凛",
                "message": "在吗？",
                "is_at_bot": True,
            }
        )
        decision = self.scheduler.message_policy(
            event,
            gate_status="session_zero",
            route_target="fu_gm",
            route_mode="casual",
            reply_required=False,
        )
        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.priority, "mandatory")

    def test_high_recent_gm_presence_suppresses_optional_beat(self) -> None:
        decision = self.scheduler.heartbeat_policy(
            gate_status="adventure",
            idle_seconds=999,
            cooldown_remaining=0,
            has_public_entries=True,
            last_entry_role="assistant",
            current_actor="",
            conflict_active=False,
            current_actor_is_pc=False,
            held_action_summary="",
            thresholds=self.thresholds,
            force=False,
            recent_gm_ratio=0.75,
            recent_message_count=8,
        )
        self.assertFalse(decision.should_speak)
        self.assertIn("占比偏高", decision.reason)

    def test_npc_turn_cannot_be_suppressed_by_presence_ratio(self) -> None:
        decision = self.scheduler.heartbeat_policy(
            gate_status="adventure",
            idle_seconds=999,
            cooldown_remaining=0,
            has_public_entries=True,
            last_entry_role="assistant",
            current_actor="监察官艾蕾娜",
            conflict_active=True,
            current_actor_is_pc=False,
            held_action_summary="",
            thresholds=self.thresholds,
            force=False,
            recent_gm_ratio=0.9,
            recent_message_count=10,
        )
        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.action, "npc_turn")

    def test_session_zero_stall_uses_last_player_idle_and_ignores_presence_ratio(self) -> None:
        decision = self.scheduler.heartbeat_policy(
            gate_status="session_zero",
            idle_seconds=30,
            player_idle_seconds=601,
            cooldown_remaining=0,
            has_public_entries=True,
            last_entry_role="user",
            current_actor="",
            conflict_active=False,
            current_actor_is_pc=False,
            held_action_summary="",
            thresholds=self.thresholds,
            force=False,
            recent_gm_ratio=0.9,
            recent_message_count=12,
            setup_nudge_count=0,
        )

        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.action, "session_zero_nudge")
        self.assertEqual(decision.telemetry["player_idle_seconds"], 601)

    def test_session_zero_never_sends_a_second_nudge(self) -> None:
        decision = self.scheduler.heartbeat_policy(
            gate_status="session_zero",
            idle_seconds=999,
            player_idle_seconds=1800,
            cooldown_remaining=0,
            has_public_entries=True,
            last_entry_role="assistant",
            current_actor="",
            conflict_active=False,
            current_actor_is_pc=False,
            held_action_summary="",
            thresholds=self.thresholds,
            force=False,
            recent_gm_ratio=0.9,
            recent_message_count=12,
            setup_nudge_count=1,
            setup_nudge_limit=2,
            seconds_since_setup_nudge=1199,
            setup_nudge_followup_seconds=1200,
        )

        self.assertFalse(decision.should_speak)
        self.assertIn("次数已用完", decision.reason)

    def test_session_zero_stops_after_the_first_nudge(self) -> None:
        common = {
            "gate_status": "session_zero",
            "idle_seconds": 999,
            "player_idle_seconds": 2400,
            "cooldown_remaining": 0,
            "has_public_entries": True,
            "last_entry_role": "assistant",
            "current_actor": "",
            "conflict_active": False,
            "current_actor_is_pc": False,
            "held_action_summary": "",
            "thresholds": self.thresholds,
            "force": False,
            "recent_gm_ratio": 0.9,
            "recent_message_count": 12,
            "setup_nudge_limit": 2,
            "seconds_since_setup_nudge": 1200,
            "setup_nudge_followup_seconds": 1200,
        }

        after_first = self.scheduler.heartbeat_policy(
            **common,
            setup_nudge_count=1,
        )
        exhausted = self.scheduler.heartbeat_policy(
            **common,
            setup_nudge_count=2,
        )

        self.assertFalse(after_first.should_speak)
        self.assertIn("次数已用完", after_first.reason)
        self.assertFalse(exhausted.should_speak)
        self.assertIn("次数已用完", exhausted.reason)


class HeartbeatIdleEpisodeTests(unittest.TestCase):
    def test_only_material_setup_progress_resets_setup_nudge_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            runtime = service._runtime("白钟大陆")
            log = runtime.log_manager
            gate = SessionGateState(
                campaign_id="白钟大陆",
                session_id="group-1",
                channel_id="group-1",
                status="session_zero",
            )
            thresholds = {
                "pre_session": 0,
                "session_zero": 0,
                "adventure": 0,
                "pc_turn": 0,
                "npc_turn": 0,
            }

            log.append_message(
                "白钟大陆",
                "group-1",
                speaker="阿凛",
                content="铁誓教团放在北方。",
                role="user",
            )
            for index in range(2):
                log.append_message(
                    "白钟大陆",
                    "group-1",
                    speaker="时悠",
                    content=f"第{index + 1}次轻推",
                    role="assistant",
                    metadata={
                        "mode": "heartbeat_agent_session_zero_nudge",
                        "delivery_confirmed": True,
                    },
                )

            exhausted = service._heartbeat_decision(
                runtime,
                campaign_id="白钟大陆",
                session_id="group-1",
                channel_id="group-1",
                gate=gate,
                thresholds=thresholds,
                cooldown_seconds=0,
                force=False,
                setup_nudge_followup_seconds=0,
                setup_nudge_limit=2,
            )
            self.assertFalse(exhausted["should_respond"])
            self.assertEqual(exhausted["idle_episode"]["status"], "exhausted")
            self.assertEqual(exhausted["idle_episode"]["nudge_count"], 2)

            log.append_message(
                "白钟大陆",
                "group-1",
                speaker="白河",
                content="我觉得北方应该终年积雪。",
                role="table_talk",
            )
            still_exhausted = service._heartbeat_decision(
                runtime,
                campaign_id="白钟大陆",
                session_id="group-1",
                channel_id="group-1",
                gate=gate,
                thresholds=thresholds,
                cooldown_seconds=0,
                force=False,
                setup_nudge_followup_seconds=0,
                setup_nudge_limit=2,
            )

            self.assertFalse(still_exhausted["should_respond"])
            self.assertEqual(
                still_exhausted["idle_episode"]["status"],
                "exhausted",
            )

            log.append_message(
                "白钟大陆",
                "group-1",
                speaker="白河",
                content="北方终年积雪，就这样定。",
                role="user",
                metadata={
                    "state_changed": True,
                    "tool_receipts": [
                        {
                            "tool_name": "commit_session_zero_update",
                            "ok": True,
                            "state_changed": True,
                        }
                    ],
                },
            )
            reset = service._heartbeat_decision(
                runtime,
                campaign_id="白钟大陆",
                session_id="group-1",
                channel_id="group-1",
                gate=gate,
                thresholds=thresholds,
                cooldown_seconds=0,
                force=False,
                setup_nudge_followup_seconds=0,
                setup_nudge_limit=2,
            )

            self.assertTrue(reset["should_respond"])
            self.assertEqual(reset["idle_episode"]["nudge_count"], 0)
            self.assertEqual(reset["idle_episode"]["last_player_speaker"], "白河")
            self.assertEqual(reset["idle_episode"]["status"], "ready")


class SpeechIntentPlannerTests(unittest.TestCase):
    def test_failed_investigation_requires_visible_resistance_without_player_control(self) -> None:
        resolution = ActionResolution(
            action=Action(ActionType.INVESTIGATE, {"actor": "伊莉雅"}),
            rules_text="伊莉雅调查失败。",
            payload={
                "roll": RollOutcome(
                    actor="伊莉雅",
                    attributes=["INS", "INS"],
                    dice=[(8, 2), (8, 3)],
                    total=5,
                    modifier=0,
                    high_roll=3,
                    target_number=10,
                    success=False,
                    critical_success=False,
                    fumble=False,
                )
            },
        )
        intent = plan_resolution_speech(resolution)

        self.assertEqual(intent.act, "investigation_resolution")
        self.assertIn("阻力", intent.tone)
        self.assertTrue(any("替玩家" in item for item in intent.avoid))


class MessageRouteIdempotencyTests(unittest.TestCase):
    def test_retried_platform_message_reuses_reply_without_reprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            class ReplyingAgent:
                def run(self, *_args, **_kwargs):
                    return GMToolAgentOutcome(
                        handled=True,
                        target="fu_gm",
                        mode="gm_agent_reply",
                        reply="我们先从基调与安全边界聊起。",
                        stop_astrbot=True,
                    )

            service.gm_tool_agent = ReplyingAgent()
            payload = {
                "campaign_id": "重复消息测试",
                "session_id": "s1",
                "channel_id": "group-1",
                "message_id": "qq-7788",
                "speaker": "阿凛",
                "speaker_id": "u1",
                "message": "开始第零章",
            }
            first_status, first = service.handle("POST", "/v1/message/route", payload)
            second_status, second = service.handle("POST", "/v1/message/route", payload)

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertTrue(first["send_reply"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["reply_envelopes"][0]["envelope_id"], second["reply_envelopes"][0]["envelope_id"])

    def test_retry_after_service_restart_reuses_persisted_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            class ReplyingAgent:
                def __init__(self) -> None:
                    self.calls = 0

                def run(self, *_args, **_kwargs):
                    self.calls += 1
                    return GMToolAgentOutcome(
                        handled=True,
                        target="fu_gm",
                        mode="gm_agent_reply",
                        reply="状态已经推进一次。",
                        stop_astrbot=True,
                    )

            payload = {
                "campaign_id": "跨重启判重",
                "session_id": "s1",
                "channel_id": "group-1",
                "message_id": "qq-restart-1",
                "speaker": "阿凛",
                "speaker_id": "u1",
                "message": "推进命刻。",
            }
            first_agent = ReplyingAgent()
            first_service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            first_service.gm_tool_agent = first_agent
            first_status, first = first_service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )

            second_agent = ReplyingAgent()
            restarted_service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            restarted_service.gm_tool_agent = second_agent
            second_status, second = restarted_service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertEqual(first_agent.calls, 1)
            self.assertEqual(second_agent.calls, 0)
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["reply"], second["reply"])
            self.assertEqual(
                first["reply_envelopes"][0]["envelope_id"],
                second["reply_envelopes"][0]["envelope_id"],
            )

    def test_retry_of_interrupted_known_event_fails_closed_without_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "campaign_id": "中断判重",
                "session_id": "s1",
                "channel_id": "group-1",
                "message_id": "qq-interrupted-1",
                "speaker": "阿凛",
                "speaker_id": "u1",
                "message": "推进命刻。",
            }
            event = MessageEvent.from_payload(payload)
            ReplyLedger(tmpdir).register_event(event)

            class ForbiddenAgent:
                def __init__(self) -> None:
                    self.calls = 0

                def run(self, *_args, **_kwargs):
                    self.calls += 1
                    raise AssertionError("中断消息不得再次进入智能体。")

            agent = ForbiddenAgent()
            service = FUGMHttpService(data_root=tmpdir, use_llm=False)
            service.gm_tool_agent = agent
            status, response = service.handle(
                "POST",
                "/v1/message/route",
                payload,
            )

            self.assertEqual(status, 200)
            self.assertEqual(agent.calls, 0)
            self.assertTrue(response["deduplicated"])
            self.assertTrue(response["incomplete_previous_attempt"])
            self.assertEqual(response["route"], "deduplicated_incomplete")


if __name__ == "__main__":
    unittest.main()
