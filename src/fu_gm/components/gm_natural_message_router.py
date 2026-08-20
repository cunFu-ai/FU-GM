from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

from fu_gm.conversation import ConversationTurn, MessageEvent


class GMNaturalMessageHost(Protocol):
    gm_tool_agent: Any
    gm_message_envelope_builder: Any
    gm_agent_message_coordinator: Any
    reply_ledger: Any
    session_gates: Any

    def _resolve_private_campaign_id(
        self,
        campaign_id: str,
        payload: dict[str, Any],
    ) -> str: ...

    def _record_channel_activity_version(
        self,
        payload: dict[str, Any],
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> None: ...

    def _message_activity(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def _channel_activity_version_is_current(
        self,
        payload: dict[str, Any],
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
    ) -> bool: ...

    def _duplicate_message_route_response(
        self,
        event: MessageEvent,
    ) -> dict[str, Any] | None: ...

    def _mark_astrbot_seen(
        self,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        speaker: str,
    ) -> None: ...

    def _mark_current_campaign(self, campaign_id: str) -> None: ...

    def _runtime(self, campaign_id: str) -> Any: ...

    def _effective_session_gate(
        self,
        runtime: Any,
        campaign_id: str,
        channel_id: str,
        session_id: str,
    ) -> Any: ...

    def _touch_speaker(
        self,
        runtime: Any,
        speaker: str,
        *,
        persist: bool = False,
    ) -> bool: ...

    def _finalize_message_route_response(
        self,
        event: MessageEvent,
        response: dict[str, Any],
        *,
        gate_status: str,
        default_target: str,
        default_mode: str,
    ) -> dict[str, Any]: ...


class GMNaturalMessageRouter:
    """Sole production ingress for non-command natural-language traffic.

    This boundary owns transport facts, idempotency and one serialized typed
    agent transaction. It never scans prose for route keywords or reinterprets
    a failed semantic turn.
    """

    def __init__(self, host: GMNaturalMessageHost) -> None:
        self.host = host

    def route(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._external_natural_payload(payload)
        requested_campaign_id = str(payload.get("campaign_id") or "default")
        campaign_id = self.host._resolve_private_campaign_id(
            requested_campaign_id,
            payload,
        )
        raw_turn_messages = payload.get("current_turn_messages")
        turn_payloads = (
            [
                self.host.gm_message_envelope_builder.with_identity_addressing(
                    self._external_natural_payload(item)
                )
                for item in raw_turn_messages
                if isinstance(item, dict)
            ]
            if isinstance(raw_turn_messages, list)
            else []
        )
        primary_payload = self.host.gm_message_envelope_builder.with_identity_addressing(
            dict(turn_payloads[-1] if turn_payloads else payload)
        )
        primary_payload.update(
            {
                "campaign_id": campaign_id,
                "session_id": str(payload.get("session_id") or "default"),
                "channel_id": str(payload.get("channel_id") or ""),
            }
        )
        envelope = self.host.gm_message_envelope_builder.build(
            primary_payload,
            campaign_id=campaign_id,
        )

        routing_payload = envelope.routing_payload(primary_payload)
        event_payloads = turn_payloads or [routing_payload]
        message_events = [
            MessageEvent.from_payload(
                item,
                campaign_id=envelope.campaign_id,
                session_id=envelope.session_id,
                channel_id=envelope.channel_id,
                text=str(item.get("message") or ""),
            )
            for item in event_payloads
            if str(item.get("message") or "").strip()
        ]
        if not message_events:
            message_events = [
                MessageEvent.from_payload(
                    routing_payload,
                    campaign_id=envelope.campaign_id,
                    session_id=envelope.session_id,
                    channel_id=envelope.channel_id,
                    text=envelope.current_message,
                )
            ]
        turn = ConversationTurn.from_events(
            message_events,
            turn_id=str(payload.get("batch_id") or ""),
        )
        message_event = turn.primary_event
        routing_payload["current_turn_events"] = [
            self._source_event_payload(event) for event in turn.events
        ]
        routing_payload["conversation_turn_id"] = turn.turn_id
        routing_payload["turn_force_gm_reply"] = bool(
            payload.get("turn_force_gm_reply")
            or turn.directly_addresses_gm
        )
        runtime = self.host._runtime(envelope.campaign_id)
        # Arrival is transport state, not campaign state. Publish it before
        # waiting for the campaign transaction so an in-flight heartbeat can
        # notice that a newer player message has made its request stale.
        request_freshness_guard = None
        tool_freshness_guard = None
        if not envelope.is_private:
            activity = self.host._message_activity(
                {
                    **routing_payload,
                    "campaign_id": envelope.campaign_id,
                    "session_id": envelope.session_id,
                    "channel_id": envelope.channel_id,
                    "is_private": False,
                }
            )
            if not bool(activity.get("ok")) or not bool(
                activity.get("tracked")
            ):
                return self._activity_registration_failure(
                    envelope,
                    str(
                        activity.get("error")
                        or activity.get("reason")
                        or "群聊消息未能登记输入高水位。"
                    ),
                )
            routing_payload["activity_version"] = int(
                activity.get("activity_version") or 0
            )

            def request_is_current() -> bool:
                return self.host._channel_activity_version_is_current(
                    routing_payload,
                    campaign_id=envelope.campaign_id,
                    session_id=envelope.session_id,
                    channel_id=envelope.channel_id,
                )

            request_freshness_guard = request_is_current
            tool_freshness_guard = lambda *_args: request_is_current()
        with runtime.transaction_lock:
            new_events = [
                event
                for event in turn.events
                if not self.host.reply_ledger.has_event(
                    event.event_id,
                    campaign_id=event.campaign_id,
                )
            ]
            for event in turn.events:
                self.host.reply_ledger.register_event(event)
            if not new_events:
                duplicate = self.host._duplicate_message_route_response(message_event)
                if duplicate is not None:
                    return duplicate

            self.host._mark_astrbot_seen(
                campaign_id=envelope.campaign_id,
                session_id=envelope.session_id,
                channel_id=envelope.channel_id,
                speaker=message_event.speaker,
            )
            self.host._mark_current_campaign(envelope.campaign_id)
            gate = self.host._effective_session_gate(
                runtime,
                envelope.campaign_id,
                envelope.channel_id,
                envelope.session_id,
            )
            if any(event.text.lstrip().startswith("/") for event in turn.events):
                return self.host._finalize_message_route_response(
                    message_event,
                    self._command_protocol_response(envelope, gate),
                    gate_status=gate.status,
                    default_target="fu_gm",
                    default_mode="command_protocol_required",
                )
            table_active = bool(
                str(gate.status or "") != "inactive"
                or runtime.app.session_ledger.active
                or runtime.app.session_zero_manager.state.active
            )
            if table_active:
                for speaker in dict.fromkeys(event.speaker for event in turn.events):
                    self.host._touch_speaker(
                        runtime,
                        speaker,
                        persist=True,
                    )
            if self.host.gm_tool_agent is None:
                return self.host._finalize_message_route_response(
                    message_event,
                    self._agent_unavailable_response(envelope, gate),
                    gate_status=gate.status,
                    default_target="fu_gm",
                    default_mode="gm_agent_unavailable",
                )

            recent_context = runtime.log_manager.format_live_context(
                envelope.campaign_id,
                envelope.session_id,
                limit=8,
            )
            response = self.host.gm_agent_message_coordinator.handle(
                routing_payload,
                gate=gate,
                is_private=envelope.is_private,
                explicitly_addressed=bool(
                    envelope.directly_addressed
                    or turn.directly_addresses_gm
                    or payload.get("turn_force_gm_reply")
                ),
                recent_context=recent_context,
                freshness_guard=tool_freshness_guard,
                request_freshness_guard=request_freshness_guard,
                side_effect_lock=runtime.transaction_lock,
            )
            if response is None:
                response = self._fail_closed_response(envelope, gate)
            authoritative_gate_status = str(
                (response.get("gate") or {}).get("status") or gate.status
            )
            finalized = self.host._finalize_message_route_response(
                message_event,
                response,
                gate_status=authoritative_gate_status,
                default_target="fu_gm",
                default_mode="gm_agent_tool",
            )
            sibling_outcome = (
                "batched_replied"
                if finalized.get("send_reply")
                else "silent"
                if finalized.get("target") == "silent"
                else "delegated"
            )
            for event in turn.events[:-1]:
                self.host.reply_ledger.mark_outcome(
                    event,
                    sibling_outcome,
                    reason="同一桌面轮次已由核心GM统一处理。",
                )
            finalized["conversation_turn_id"] = turn.turn_id
            finalized["batch_event_ids"] = [
                event.event_id for event in turn.events
            ]
            return finalized

    @staticmethod
    def _external_natural_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """隔离仅由服务内部入口产生的系统节拍元数据。"""

        return {
            key: value
            for key, value in dict(payload).items()
            if key != "system_gm_beat_request"
            and not key.startswith("heartbeat_")
            and not key.startswith("_fu_gm_internal_")
        }

    @staticmethod
    def _activity_registration_failure(
        envelope: Any,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "campaign_id": envelope.campaign_id,
            "session_id": envelope.session_id,
            "channel_id": envelope.channel_id,
            "target": "silent",
            "route": "group_activity_registration_failed",
            "send_reply": False,
            "stop_astrbot": True,
            "reply": "",
            "reply_envelopes": [],
            "error_code": "GROUP_ACTIVITY_IDEMPOTENCY_REQUIRED",
            "error": reason,
            "decision": {
                "target": "silent",
                "reason": reason,
                "tags": [
                    "group_activity_registration_failed",
                    "fail_closed",
                ],
            },
        }

    @staticmethod
    def _source_event_payload(event: MessageEvent) -> dict[str, Any]:
        """分离平台投递幂等标识与规则行动的逻辑来源。

        模型服务重试属于新的HTTP或QQ投递，因此回复账本仍使用各自的
        ``MessageEvent.event_id``。规则、工作简报与确定性掷骰则应把重试
        视为同一条尚未提交的玩家声明。测试器可提供作用域内逻辑标识；
        普通平台消息仍直接使用投递事件标识。
        """

        payload = event.to_dict()
        logical_id = str(
            (event.metadata or {}).get("logical_source_event_id") or ""
        ).strip()
        if logical_id:
            payload["delivery_event_id"] = event.event_id
            payload["event_id"] = logical_id
        return payload

    @staticmethod
    def _fail_closed_response(envelope: Any, gate: Any) -> dict[str, Any]:
        must_reply = bool(envelope.directly_addressed or envelope.is_private)
        target = "fu_gm" if must_reply else "silent"
        return {
            "ok": True,
            "campaign_id": envelope.campaign_id,
            "session_id": envelope.session_id,
            "target": target,
            "route": "gm_agent_fail_closed",
            "reply": (
                "刚才这句没有进入团务处理，我也没有改动任何状态。麻烦再发一次。"
                if must_reply
                else ""
            ),
            "send_reply": must_reply,
            "stop_astrbot": True,
            "decision": {
                "target": target,
                "mode": "gm_agent_fail_closed",
                "audience": "gm" if must_reply else "players",
                "reply_required": must_reply,
                "reason": "核心GM没有返回有效事务；已失败关闭且未进入其他解释路径。",
                "confidence": 1.0,
                "stop_astrbot": True,
                "tags": ["gm_agent_fail_closed", "single_agent_path"],
            },
            "gate": asdict(gate),
            "agent_error": "GM agent coordinator returned no outcome.",
        }

    @staticmethod
    def _agent_unavailable_response(envelope: Any, gate: Any) -> dict[str, Any]:
        must_reply = bool(envelope.directly_addressed or envelope.is_private)
        target = "fu_gm" if must_reply else "silent"
        return {
            "ok": True,
            "campaign_id": envelope.campaign_id,
            "session_id": envelope.session_id,
            "target": target,
            "route": "gm_agent_unavailable",
            "reply": (
                "当前主持智能体没有启动，这句话没有写入战役状态。"
                if must_reply
                else ""
            ),
            "send_reply": must_reply,
            "stop_astrbot": True,
            "decision": {
                "target": target,
                "mode": "gm_agent_unavailable",
                "audience": "gm" if must_reply else "players",
                "reply_required": must_reply,
                "reason": "类型化核心GM未配置；失败关闭且不启用其他解释路径。",
                "confidence": 1.0,
                "stop_astrbot": True,
                "tags": ["gm_agent_unavailable", "single_agent_path"],
            },
            "gate": asdict(gate),
            "agent_error": "Typed GM tool agent is not configured.",
        }

    @staticmethod
    def _command_protocol_response(envelope: Any, gate: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "campaign_id": envelope.campaign_id,
            "session_id": envelope.session_id,
            "target": "fu_gm",
            "route": "command_protocol_required",
            "reply": "这条斜杠命令需要由对应的 AstrBot 命令或专用 API 处理。",
            "send_reply": True,
            "stop_astrbot": True,
            "decision": {
                "target": "fu_gm",
                "mode": "command_protocol_required",
                "audience": "gm",
                "reply_required": True,
                "reason": "命令协议与自然语言智能体入口分离。",
                "confidence": 1.0,
                "stop_astrbot": True,
                "tags": ["command_protocol_required", "single_agent_path"],
            },
            "gate": asdict(gate),
        }
