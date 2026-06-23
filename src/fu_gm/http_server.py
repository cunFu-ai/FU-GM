from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from fu_gm.app_factory import build_app
from fu_gm.campaign_importer import CampaignChatLogImporter, import_payload_preview
from fu_gm.casual_chat import CasualChatResponder
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.session_log_manager import HeuristicStorySummarizer, LLMStorySummarizer, SessionLogManager
from fu_gm.components.topic_memory_store import TopicMemoryStore
from fu_gm.config import ImageGenerationConfig, LLMConfig
from fu_gm.gm_guidance import summarize_guidance_for_prompt
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.message_arbiter import HeuristicMessageArbiter, MessageRouteDecision
from fu_gm.play_process_guidance import summarize_play_process_for_prompt
from fu_gm.pre_session_consensus import PreSessionConsensusFacilitator
from fu_gm.safety_parser import extract_safety_declarations
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.session_gate import SessionGateManager, SessionGateSignal, SessionGateState


@dataclass
class CampaignRuntime:
    campaign_id: str
    app: SceneOrchestrator
    log_manager: SessionLogManager
    casual_chat: CasualChatResponder
    loaded_from_disk: bool = False
    last_saved_path: str = ""
    last_loaded_slot: str = ""


@dataclass
class _FilePayload:
    body: bytes
    content_type: str


class FUGMHttpService:
    """FU-GM 的轻量 HTTP 应用层。

    这不是生产级 Web 框架，而是给 AstrBot/本地工具使用的稳定桥接边界。
    """

    def __init__(
        self,
        *,
        data_root: str | Path = "data/campaigns",
        use_llm: bool = True,
        gm_name: str = "时悠",
        gm_style_prompt: str = "",
        deepseek_roleplay_mode: str = "default",
    ) -> None:
        self.data_root = Path(data_root)
        self.use_llm = use_llm
        self.gm_name = gm_name
        self.gm_style_prompt = gm_style_prompt
        self.deepseek_roleplay_mode = deepseek_roleplay_mode
        self.runtimes: dict[str, CampaignRuntime] = {}
        self.current_campaign_id = ""
        self.message_arbiter = HeuristicMessageArbiter(
            gm_aliases=[gm_name, "时悠", "悠老师", "小夜", "织星者", "gm", "GM", "主持"]
        )
        self.session_gates = SessionGateManager(self.data_root)
        self.pre_session_facilitator = PreSessionConsensusFacilitator(gm_name=gm_name)
        self.started_at = datetime.now(timezone.utc)
        self.recent_http_spans: list[dict[str, Any]] = []
        self.astrbot_bridge_state: dict[str, Any] = {
            "last_seen_at": "",
            "last_campaign_id": "",
            "last_session_id": "",
            "last_channel_id": "",
            "last_speaker": "",
            "total_messages": 0,
        }

    def handle(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | str]:
        started_at = time.monotonic()
        payload = payload or {}
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and route in {"/gm", "/audit", "/dashboard"}:
                return self._logged_response(method, route, started_at, 200, self._audit_page())
            if method == "GET" and route == "/health":
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    {
                        "ok": True,
                        "service": "fu-gm",
                        "campaigns": sorted(self.runtimes),
                        "runtime": self._service_status_payload(),
                        "active_runtime": self._runtime_status_payload(
                            self.current_campaign_id,
                            self.runtimes[self.current_campaign_id],
                        )
                        if self.current_campaign_id in self.runtimes
                        else {},
                        "astrbot_bridge": self._astrbot_status_payload(),
                    },
                )
            if method == "GET" and route == "/v1/artifacts/file":
                status, body = self._artifact_file(query.get("path", [""])[0])
                return self._logged_response(method, route, started_at, status, body)
            if method == "GET" and route == "/v1/audit/dashboard":
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    self._audit_dashboard(
                    {
                        "campaign_id": query.get("campaign_id", ["default"])[0],
                        "session_id": query.get("session_id", ["default"])[0],
                        "channel_id": query.get("channel_id", [""])[0],
                        "limit": query.get("limit", ["40"])[0],
                        "include_private": query.get("include_private", ["false"])[0],
                    }
                    ),
                )
            if method == "GET" and route == "/v1/campaigns":
                return self._logged_response(method, route, started_at, 200, self._list_campaigns())
            if method == "GET" and route == "/v1/campaigns/current":
                return self._logged_response(method, route, started_at, 200, self._current_campaign_payload())
            if method == "GET" and route.startswith("/v1/campaigns/") and route.endswith("/snapshot"):
                campaign_id = unquote(route.split("/")[3])
                runtime = self._runtime(campaign_id)
                include_private = query.get("include_private", ["false"])[0].lower() in {"1", "true", "yes"}
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    {"campaign_id": campaign_id, "snapshot": runtime.app.session_zero_summary(include_private=include_private)},
                )
            if method == "GET" and route.startswith("/v1/campaigns/") and route.endswith("/save-slots"):
                campaign_id = unquote(route.split("/")[3])
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    {"ok": True, "campaign_id": campaign_id, "slots": self._memory_store().list_save_slots(campaign_id)},
                )
            if method == "POST" and route == "/v1/campaigns/new":
                return self._logged_response(method, route, started_at, 200, self._new_campaign(payload))
            if method == "POST" and route == "/v1/campaigns/save":
                return self._logged_response(method, route, started_at, 200, self._save_campaign(payload))
            if method == "POST" and route == "/v1/campaigns/load":
                status, body = self._load_campaign(payload)
                return self._logged_response(method, route, started_at, status, body)
            if method == "POST" and route == "/v1/campaigns/import-chat-log":
                status, body = self._import_chat_log(payload)
                return self._logged_response(method, route, started_at, status, body)
            if method == "POST" and route == "/v1/campaigns/delete":
                status, body = self._delete_campaign(payload)
                return self._logged_response(method, route, started_at, status, body)
            if method == "POST" and route == "/v1/chat":
                return self._logged_response(method, route, started_at, 200, self._chat(payload))
            if method == "POST" and route == "/v1/message/route":
                return self._logged_response(method, route, started_at, 200, self._message_route(payload))
            if method == "POST" and route == "/v1/safety/declare":
                return self._logged_response(method, route, started_at, 200, self._safety_declare(payload))
            if method == "POST" and route == "/v1/game/turn":
                return self._logged_response(method, route, started_at, 200, self._game_turn(payload))
            if method == "POST" and route == "/v1/session-zero/start":
                return self._logged_response(method, route, started_at, 200, self._session_zero_start(payload))
            if method == "POST" and route == "/v1/session-zero/message":
                return self._logged_response(method, route, started_at, 200, self._session_zero_message(payload))
            if method == "POST" and route == "/v1/session/end":
                return self._logged_response(method, route, started_at, 200, self._end_session(payload))
            if method == "POST" and route == "/v1/session/away":
                return self._logged_response(method, route, started_at, 200, self._session_away(payload))
            if method == "POST" and route == "/v1/session/back":
                return self._logged_response(method, route, started_at, 200, self._session_back(payload))
            if method == "POST" and route == "/v1/session/status":
                return self._logged_response(method, route, started_at, 200, self._session_status(payload))
            if method == "POST" and route == "/v1/session/gate":
                return self._logged_response(method, route, started_at, 200, self._session_gate(payload))
            if method == "GET" and route == "/v1/session/status":
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    self._session_status({"campaign_id": query.get("campaign_id", ["default"])[0]}),
                )
            if method == "GET" and route == "/v1/session/gate":
                return self._logged_response(
                    method,
                    route,
                    started_at,
                    200,
                    self._session_gate(
                        {
                            "campaign_id": query.get("campaign_id", ["default"])[0],
                            "session_id": query.get("session_id", ["default"])[0],
                            "channel_id": query.get("channel_id", [""])[0],
                        }
                    ),
                )
            return self._logged_response(method, route, started_at, 404, {"ok": False, "error": f"未知路径：{method} {route}"})
        except Exception as exc:
            return self._logged_response(method, route, started_at, 500, {"ok": False, "error": str(exc)})

    def _logged_response(
        self,
        method: str,
        route: str,
        started_at: float,
        status: int,
        body: dict[str, Any] | str | _FilePayload,
    ) -> tuple[int, dict[str, Any] | str | _FilePayload]:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        ok = status < 400 and not (isinstance(body, dict) and body.get("ok") is False)
        self._record_http_span(method=method, route=route, status=status, ok=ok, elapsed_ms=elapsed_ms, body=body)
        print(f"[FU-GM HTTP] {method} {route} {'ok' if ok else 'error'} {elapsed_ms}ms", flush=True)
        return status, body

    def _record_http_span(
        self,
        *,
        method: str,
        route: str,
        status: int,
        ok: bool,
        elapsed_ms: int,
        body: dict[str, Any] | str | _FilePayload,
    ) -> None:
        campaign_id = ""
        session_id = ""
        if isinstance(body, dict):
            campaign_id = str(body.get("campaign_id") or "")
            session_id = str(body.get("session_id") or "")
        self.recent_http_spans.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "route": route,
                "status": status,
                "ok": ok,
                "elapsed_ms": elapsed_ms,
                "campaign_id": campaign_id,
                "session_id": session_id,
            }
        )
        self.recent_http_spans = self.recent_http_spans[-200:]

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        speaker = str(payload.get("speaker") or payload.get("user_name") or "玩家")
        message = str(payload.get("message") or "")
        channel_id = str(payload.get("channel_id") or "")
        mode = str(payload.get("mode") or "auto")
        self._mark_current_campaign(campaign_id)
        runtime = self._runtime(campaign_id)
        resolved_mode = self._resolve_mode(message, mode)
        resolved_mode = self._mode_after_session_gate(
            campaign_id=campaign_id,
            channel_id=channel_id,
            session_id=session_id,
            resolved_mode=resolved_mode,
        )

        if resolved_mode == "safety":
            result = self._safety_declare(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "speaker": speaker,
                    "message": message,
                    "channel_id": channel_id,
                    "anonymous": bool(payload.get("anonymous", False)),
                }
            )
            return {**result, "route": resolved_mode}
        self._touch_speaker(runtime, speaker)

        rules_reference = runtime.casual_chat.try_rules_reference(message)
        if rules_reference is not None:
            runtime.log_manager.append_turn(
                campaign_id,
                session_id,
                speaker=speaker,
                message=message,
                gm_reply=rules_reference.reply,
                channel_id=channel_id,
                metadata={"mode": "rules_reference", "resolved_mode": resolved_mode},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "route": "rules_reference",
                "reply": rules_reference.reply,
                "recalled_memories": rules_reference.recalled_memories,
                "public_memory": rules_reference.public_memory,
            }

        if resolved_mode == "session_zero":
            result = self._session_zero_message(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "speaker": speaker,
                    "message": message,
                    "channel_id": channel_id,
                }
            )
            return {**result, "route": resolved_mode}
        if resolved_mode == "pre_session":
            result = self._pre_session_message(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "speaker": speaker,
                    "message": message,
                    "channel_id": channel_id,
                }
            )
            return {**result, "route": resolved_mode}
        if resolved_mode == "game":
            result = self._game_turn(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "speaker": speaker,
                    "message": message,
                    "channel_id": channel_id,
                }
            )
            return {**result, "route": resolved_mode}

        response = runtime.casual_chat.respond(
            campaign_id=campaign_id,
            session_id=session_id,
            speaker=speaker,
            message=message,
            world_state=runtime.app.world_state,
        )
        if not response.reply:
            runtime.log_manager.append_turn(
                campaign_id,
                session_id,
                speaker=speaker,
                message=message,
                gm_reply="",
                channel_id=channel_id,
                metadata={"mode": "casual", "suppressed": True},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "route": "casual",
                "reply": "",
                "send_reply": False,
                "suppressed": True,
                "recalled_memories": response.recalled_memories,
                "public_memory": response.public_memory,
                "live_context": response.live_context or [],
            }
        runtime.log_manager.append_turn(
            campaign_id,
            session_id,
            speaker=speaker,
            message=message,
            gm_reply=response.reply,
            channel_id=channel_id,
            metadata={"mode": "casual"},
        )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "route": "casual",
            "reply": response.reply,
            "recalled_memories": response.recalled_memories,
            "public_memory": response.public_memory,
            "live_context": response.live_context or [],
        }

    def _message_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        self._mark_astrbot_seen(campaign_id=campaign_id, session_id=session_id, channel_id=channel_id, speaker=speaker)
        self._mark_current_campaign(campaign_id)
        is_private = bool(payload.get("is_private", False))
        is_group = not is_private
        gate = self.session_gates.get(campaign_id, channel_id, session_id)
        signal = None if is_private else self.session_gates.detect_signal(message, current_status=gate.status)
        if signal:
            return self._handle_gate_signal(payload, gate=gate, signal=signal)

        save_control = self._maybe_handle_save_control(payload, gate=gate, is_private=is_private)
        if save_control is not None:
            return save_control

        decision = self._batched_route_decision(payload, gate=gate, is_private=is_private, is_group=is_group)
        if decision is None:
            decision = self.message_arbiter.decide(message, speaker=speaker, is_private=is_private, is_group=is_group)
        decision_payload = asdict(decision)
        if not is_private and gate.status == "pre_session" and decision.mode == "safety":
            decision.target = "fu_gm"
            decision.mode = "pre_session"
            decision.reason = "开团前共识阶段的安全与基调声明。"
            decision.stop_astrbot = True
            decision.tags.append("pre_session_safety")
            decision_payload = asdict(decision)
        if decision.mode == "safety":
            forwarded = dict(payload)
            forwarded["mode"] = "safety"
            forwarded["anonymous"] = bool(payload.get("anonymous", is_private))
            result = self._chat(forwarded)
            return {
                **result,
                "target": "fu_gm",
                "send_reply": bool(result.get("reply")),
                "stop_astrbot": True,
                "decision": decision_payload,
                "gate": asdict(gate),
            }

        if not is_private and gate.status == "inactive":
            decision_payload = {
                **decision_payload,
                "target": "astrbot",
                "mode": "",
                "stop_astrbot": False,
                "reason": "FU-GM 会话未开启；等待明确开团信号。",
            }
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "astrbot",
                "send_reply": False,
                "stop_astrbot": False,
                "decision": decision_payload,
                "gate": asdict(gate),
            }

        if not is_private and gate.status == "paused":
            decision_payload = {
                **decision_payload,
                "target": "astrbot",
                "mode": "",
                "stop_astrbot": False,
                "reason": "FU-GM 会话已暂停；等待继续或收团信号。",
            }
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "astrbot",
                "send_reply": False,
                "stop_astrbot": False,
                "decision": decision_payload,
                "gate": asdict(gate),
            }

        if decision.target == "astrbot":
            if not is_private and gate.status == "pre_session" and self.pre_session_facilitator.is_substantive(message):
                decision.target = "fu_gm"
                decision.mode = "pre_session"
                decision.reason = "开团前共识阶段，识别为实质共识贡献。"
                decision.stop_astrbot = True
                decision.tags.append("pre_session_consensus")
            elif not is_private and gate.status == "session_zero" and self.message_arbiter.should_accept_open_session_zero_input(message):
                decision.target = "fu_gm"
                decision.mode = "session_zero"
                decision.reason = "第零章已开启，识别为实质设定贡献。"
                decision.stop_astrbot = True
                decision.tags.append("open_session_zero_contribution")
            elif not is_private and gate.status in {"pre_session", "session_zero", "adventure"}:
                decision.target = "silent"
                decision.mode = ""
                decision.reason = "跑团会话中的桌边闲聊或玩家间讨论，仅记录，不触发 GM 回复。"
                decision.stop_astrbot = True
                if "table_talk" not in decision.tags:
                    decision.tags.append("table_talk")
            decision_payload = asdict(decision)

        runtime = self._runtime(campaign_id)
        if decision.target == "silent":
            runtime.log_manager.append_message(
                campaign_id,
                session_id,
                speaker=speaker,
                content=message,
                role="table_talk",
                channel_id=channel_id,
                metadata={"mode": "natural_silent", "decision": decision_payload},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "silent",
                "send_reply": False,
                "stop_astrbot": decision.stop_astrbot,
                "decision": decision_payload,
                "gate": asdict(gate),
            }

        forwarded = dict(payload)
        forwarded["mode"] = decision.mode or "casual"
        if gate.status == "pre_session" and forwarded["mode"] == "casual":
            forwarded["mode"] = "pre_session"
        if gate.status == "session_zero" and forwarded["mode"] == "casual":
            forwarded["mode"] = "session_zero"
        if decision.mode == "safety":
            forwarded["anonymous"] = bool(payload.get("anonymous", is_private))
        result = self._chat(forwarded)
        return {
            **result,
            "target": "fu_gm",
            "send_reply": bool(result.get("reply")),
            "stop_astrbot": True,
            "decision": decision_payload,
            "gate": asdict(gate),
        }

    def _mark_astrbot_seen(self, *, campaign_id: str, session_id: str, channel_id: str, speaker: str) -> None:
        self.astrbot_bridge_state.update(
            {
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "last_campaign_id": campaign_id,
                "last_session_id": session_id,
                "last_channel_id": channel_id,
                "last_speaker": speaker,
                "total_messages": int(self.astrbot_bridge_state.get("total_messages") or 0) + 1,
            }
        )

    def _batched_route_decision(
        self,
        payload: dict[str, Any],
        *,
        gate: SessionGateState,
        is_private: bool,
        is_group: bool,
    ) -> MessageRouteDecision | None:
        raw_messages = payload.get("batch_messages")
        if not isinstance(raw_messages, list) or len(raw_messages) <= 1:
            return None

        item_decisions: list[MessageRouteDecision] = []
        messages: list[tuple[str, str]] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            item_speaker = str(raw.get("speaker") or payload.get("speaker") or "玩家")
            item_message = str(raw.get("message") or "")
            if not item_message.strip():
                continue
            messages.append((item_speaker, item_message))
            item_decisions.append(
                self.message_arbiter.decide(
                    item_message,
                    speaker=item_speaker,
                    is_private=is_private,
                    is_group=is_group,
                )
            )
        if not item_decisions:
            return MessageRouteDecision(
                target="silent",
                reason="批次中没有可处理的实质消息。",
                confidence=0.9,
                stop_astrbot=True,
                tags=["batch", "empty"],
            )

        if any(decision.mode == "safety" for decision in item_decisions):
            return MessageRouteDecision(
                target="fu_gm",
                mode="safety",
                reason="批次内包含安全边界声明。",
                confidence=0.95,
                stop_astrbot=True,
                tags=["batch", "safety"],
            )

        if gate.status == "pre_session" and any(
            self.pre_session_facilitator.is_substantive(message)
            or decision.mode in {"pre_session", "session_zero"}
            for (_speaker, message), decision in zip(messages, item_decisions)
        ):
            return MessageRouteDecision(
                target="fu_gm",
                mode="pre_session",
                reason="开团前共识阶段的合并发言包含实质贡献。",
                confidence=0.9,
                stop_astrbot=True,
                tags=["batch", "pre_session"],
            )

        if gate.status == "session_zero" and any(
            self.message_arbiter.should_accept_open_session_zero_input(message)
            or decision.mode == "session_zero"
            for (_speaker, message), decision in zip(messages, item_decisions)
        ):
            return MessageRouteDecision(
                target="fu_gm",
                mode="session_zero",
                reason="第零章阶段的合并发言包含实质设定贡献。",
                confidence=0.9,
                stop_astrbot=True,
                tags=["batch", "session_zero"],
            )

        for mode in ("game", "casual", "session_zero"):
            if any(decision.target == "fu_gm" and decision.mode == mode for decision in item_decisions):
                return MessageRouteDecision(
                    target="fu_gm",
                    mode=mode,
                    reason=f"批次内至少一条消息被判定为 {mode}。",
                    confidence=max(decision.confidence for decision in item_decisions),
                    stop_astrbot=True,
                    tags=["batch", mode],
                )

        if gate.status in {"pre_session", "session_zero", "adventure"} or any(
            decision.target == "silent" for decision in item_decisions
        ):
            return MessageRouteDecision(
                target="silent",
                reason="合并发言均为跑团语境下的桌边讨论，暂不触发 GM 回复。",
                confidence=0.75,
                stop_astrbot=True,
                tags=["batch", "table_talk"],
            )

        return MessageRouteDecision(
            target="astrbot",
            reason="合并发言不属于 FU-GM 接管范围。",
            confidence=0.55,
            stop_astrbot=False,
            tags=["batch"],
        )

    def _handle_gate_signal(
        self,
        payload: dict[str, Any],
        *,
        gate: SessionGateState,
        signal: SessionGateSignal,
    ) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        self._mark_current_campaign(campaign_id)
        runtime = self._runtime(campaign_id)
        if signal.kind == "start":
            if signal.status == "adventure":
                runtime = self._runtime(campaign_id)
                blockers = self._adventure_start_blockers(runtime)
                if blockers:
                    gate_state = self.session_gates.get(campaign_id, channel_id, session_id)
                    return {
                        "ok": True,
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "target": "fu_gm",
                        "route": "session_gate",
                        "send_reply": True,
                        "stop_astrbot": True,
                        "reply": self._format_adventure_blocked_reply(blockers),
                        "gate": asdict(gate_state),
                        "signal": asdict(signal),
                        "blocked": True,
                    }
            state = self.session_gates.activate(
                campaign_id,
                channel_id,
                session_id,
                status=signal.status or "adventure",
                reason=signal.reason,
            )
            runtime.log_manager.append_message(
                campaign_id,
                session_id,
                speaker="系统",
                content=f"{speaker} 开启了 FU-GM 会话：{state.status}。",
                role="system",
                channel_id=channel_id,
                metadata={"mode": "session_gate", "signal": asdict(signal), "gate": asdict(state)},
            )
            if state.status == "session_zero":
                result = self._session_zero_start(
                    {
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "channel_id": channel_id,
                        "participants": [speaker],
                    }
                )
                reply = "第零章已开启。接下来这个群会由 FU-GM 接管世界/角色创建。\n" + str(result.get("reply", ""))
            elif state.status == "pre_session":
                result = self._pre_session_start(
                    {
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "channel_id": channel_id,
                        "speaker": speaker,
                    }
                )
                reply = str(result.get("reply", ""))
            else:
                map_status = runtime.app.ensure_world_map_for_adventure(max_attempts=2)
                if map_status.get("status") == "generated":
                    self._autosave_campaign(runtime, campaign_id)
                reply = (
                    "开团啦。接下来这个群的跑团相关发言会先交给 FU-GM："
                    "行动我会结算，讨论我会记日志，普通吐槽我也会带着故事记忆接话。"
                )
                if map_status.get("status") in {"generated", "ready"}:
                    reply += "\n世界地图已经准备好。"
                elif map_status.get("status") == "failed":
                    reply += "\n世界地图在内部重试后仍未能生成；冒险可以继续，我已保留错误供后台检查。"
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "fu_gm",
                "route": "session_gate",
                "send_reply": True,
                "stop_astrbot": True,
                "reply": reply,
                "gate": asdict(state),
                "signal": asdict(signal),
                "world_map": map_status if state.status == "adventure" else None,
            }

        if signal.kind == "pause":
            state = self.session_gates.pause(campaign_id, channel_id, session_id, reason=signal.reason)
            path = runtime.app.save_campaign_memory(campaign_id)
            runtime.last_saved_path = str(path)
            runtime.log_manager.append_message(
                campaign_id,
                session_id,
                speaker="系统",
                content=f"{speaker} 暂停了 FU-GM 会话，已保存快照。",
                role="system",
                channel_id=channel_id,
                metadata={"mode": "session_gate_pause", "signal": asdict(signal), "gate": asdict(state), "path": str(path)},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "fu_gm",
                "route": "session_gate",
                "send_reply": True,
                "stop_astrbot": True,
                "reply": f"已暂停《{campaign_id}》，并保存当前状态。要继续时直接说“继续跑团”就行。",
                "gate": asdict(state),
                "signal": asdict(signal),
            }

        if signal.kind == "end":
            summary = self._end_session(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "title": str(payload.get("title") or ""),
                }
            )
            state = SessionGateState(**summary.get("gate", {}))
            public_summary = summary.get("summary", {}).get("short_memory") or summary.get("summary", {}).get("public_summary") or ""
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target": "fu_gm",
                "route": "session_gate",
                "send_reply": True,
                "stop_astrbot": True,
                "reply": "本场收团，日志和故事记忆已经整理保存。" + (f"\n{public_summary}" if public_summary else ""),
                "gate": asdict(state),
                "signal": asdict(signal),
                "summary": summary.get("summary", {}),
            }

        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "target": "astrbot",
            "send_reply": False,
            "stop_astrbot": False,
            "gate": asdict(gate),
            "signal": asdict(signal),
        }

    def _maybe_handle_save_control(
        self,
        payload: dict[str, Any],
        *,
        gate: SessionGateState,
        is_private: bool,
    ) -> dict[str, Any] | None:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        parsed = self._parse_save_control(message, gate=gate, is_private=is_private)
        if parsed is None:
            return None

        action = parsed["action"]
        result: dict[str, Any]
        if action == "list":
            runtime = self._runtime(campaign_id)
            runtime.log_manager.append_message(
                campaign_id,
                session_id,
                speaker="系统",
                content=f"{speaker} 调出了 FU-GM 存档列表。",
                role="system",
                channel_id=channel_id,
                metadata={"mode": "save_control", "action": "list"},
            )
            result = {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "route": "save_control",
                "reply": self._format_save_list(current_campaign_id=campaign_id),
            }
        elif action == "save":
            request_payload = dict(payload)
            request_payload["slot"] = parsed.get("slot", "")
            result = self._save_campaign(request_payload)
            result["route"] = "save_control"
        elif action == "load":
            request_payload = dict(payload)
            request_payload["campaign_id"] = parsed.get("campaign_id") or campaign_id
            request_payload["slot"] = parsed.get("slot", "")
            status, loaded = self._load_campaign(request_payload)
            if status == 404 and not parsed.get("slot"):
                result = {
                    "ok": True,
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "route": "save_control",
                    "reply": self._format_save_list(current_campaign_id=campaign_id),
                }
            else:
                result = loaded
                result["route"] = "save_control"
        else:
            return None

        return {
            **result,
            "target": "fu_gm",
            "send_reply": True,
            "stop_astrbot": True,
            "gate": asdict(gate),
            "decision": {
                "target": "fu_gm",
                "mode": "save_control",
                "reason": parsed.get("reason", "存档控制请求"),
                "confidence": 0.95,
                "stop_astrbot": True,
                "tags": ["save_control", action],
            },
        }

    def _parse_save_control(
        self,
        message: str,
        *,
        gate: SessionGateState,
        is_private: bool,
    ) -> dict[str, str] | None:
        text = " ".join(str(message or "").strip().split())
        if not text or text.startswith("/"):
            return None
        direct = self.message_arbiter._directly_addresses_gm(text)
        active_context = gate.active or gate.paused or is_private or direct
        if not active_context:
            return None

        cleaned = text
        for alias in self.message_arbiter.gm_aliases:
            cleaned = cleaned.replace(alias, "")
        cleaned = cleaned.strip(" ，。！？!?:：")
        lowered = cleaned.lower()

        list_tokens = ("存档列表", "读档列表", "读取列表", "有哪些存档", "有什么存档", "调出存档", "查看存档", "列出存档")
        if any(token in cleaned for token in list_tokens):
            return {"action": "list", "reason": "请求查看存档列表"}

        load_tokens = ("读取存档", "读档", "载入存档", "加载存档", "读取战役", "载入战役")
        if any(token in cleaned for token in load_tokens):
            slot = self._extract_control_argument(cleaned, load_tokens)
            if not slot:
                return {"action": "list", "reason": "请求读档但未指定槽位，先展示列表"}
            return {"action": "load", "slot": slot, "reason": "请求读取命名存档"}

        save_tokens = ("新建存档", "创建存档", "保存存档", "手动存档", "快速存档", "存个档", "存档", "保存一下")
        if any(token in cleaned for token in save_tokens):
            # “像存档点一样”这类比喻不应触发真实保存。
            if any(token in lowered for token in ("像存档", "存档点", "读档感", "sl")) and not direct:
                return None
            slot = self._extract_control_argument(cleaned, save_tokens)
            return {"action": "save", "slot": slot, "reason": "请求保存当前战役"}

        return None

    def _extract_control_argument(self, text: str, tokens: tuple[str, ...]) -> str:
        best_index = -1
        best_token = ""
        for token in tokens:
            index = text.find(token)
            if index >= 0 and index >= best_index:
                best_index = index
                best_token = token
        if best_index < 0:
            return ""
        argument = text[best_index + len(best_token) :].strip(" ：:，,。.!！?？「」『』【】[]()（）")
        for prefix in ("到", "为", "成", "一下", "吧", "呢"):
            if argument.startswith(prefix):
                argument = argument[len(prefix) :].strip(" ：:，,。.!！?？")
        if argument in {"一下", "吧", "呢", "当前", "最新", "默认"}:
            return ""
        return argument[:80]

    def _format_save_list(self, *, current_campaign_id: str = "") -> str:
        campaigns = self._list_campaigns().get("campaigns", [])
        current = current_campaign_id or self._current_campaign_id()
        if not campaigns:
            return "目前还没有任何本地存档。可以说“存档”保存当前团，或说“新建存档 boss战前”保存一个命名槽。"
        lines = ["我把 FU-GM 目前知道的存档列出来啦："]
        for item in campaigns:
            marker = " <- 当前" if item.get("campaign_id") == current else ""
            latest = "最新快照" if item.get("has_latest_snapshot") else "无最新快照"
            status = item.get("active_status") or ""
            status_text = f"，{status}" if status else ""
            lines.append(f"- 《{item.get('campaign_id')}》：{latest}{status_text}{marker}")
            slots = item.get("slot_details") or []
            if slots:
                for slot in slots:
                    saved_at = slot.get("saved_at") or ""
                    lines.append(f"  · {slot.get('slot')}（{saved_at}）")
        lines.append("想读取的话可以说：读档 <存档槽名>。想保存当前进度可以说：存档，或 新建存档 <槽名>。")
        return "\n".join(lines)

    def _safety_declare(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        anonymous = bool(payload.get("anonymous", False))
        runtime = self._runtime(campaign_id)
        results = runtime.app.safety_manager.parse_and_declare(speaker, message, anonymous=anonymous)
        declared = [asdict(result) for result in results if result.accepted]

        if declared:
            path = runtime.app.save_campaign_memory(campaign_id)
            runtime.last_saved_path = str(path)
            system_content = "匿名玩家更新了界限与帷幕。" if anonymous else f"{speaker} 更新了界限与帷幕。"
            runtime.log_manager.append_message(
                campaign_id,
                session_id,
                speaker="系统",
                content=system_content,
                role="system",
                channel_id=channel_id,
                metadata={
                    "mode": "safety",
                    "anonymous": anonymous,
                    "declared": declared,
                    "path": str(path),
                },
            )
            if anonymous:
                reply = "已匿名记录并立即应用到当前团。群聊里不会说明是谁提出的，我也不会追问原因。"
            else:
                reply = "已记录并立即应用到当前团。我不会追问原因，只会从现在开始按这个边界处理。"
        else:
            reply = (
                "我收到这条安全声明啦，但还没识别出具体要记录的元素。"
                "可以直接说：我不希望出现 X，或者 X 请淡出处理。"
            )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": reply,
            "declared": declared,
            "anonymous": anonymous,
            "safety_guidance": runtime.app.safety_guidance(),
        }

    def _game_turn(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        runtime = self._runtime(campaign_id)
        self._touch_speaker(runtime, speaker)
        map_status = runtime.app.ensure_world_map_for_adventure(max_attempts=2)
        if map_status.get("status") == "generated":
            self._autosave_campaign(runtime, campaign_id)
        live_context = runtime.log_manager.format_live_context(campaign_id, session_id, limit=18)
        recent_chat = self._format_turn_input(live_context=live_context, speaker=speaker, message=message)
        try:
            reply = runtime.app.run_turn(recent_chat)
        except RuntimeError as exc:
            if "heuristic fallback is disabled" not in str(exc):
                raise
            reply = "模型暂时没有接上，本轮没有推进剧情，也没有写入新的跑团事实。请稍后重试。"
            runtime.log_manager.append_turn(
                campaign_id,
                session_id,
                speaker=speaker,
                message=message,
                gm_reply=reply,
                channel_id=channel_id,
                metadata={"mode": "game", "llm_unavailable": True, "error": str(exc)},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "reply": reply,
                "llm_unavailable": True,
                "error": str(exc),
                "world_map": map_status,
            }
        except (TypeError, ValueError, KeyError) as exc:
            reply = self._format_rules_blocked_reply(exc)
            runtime.log_manager.append_turn(
                campaign_id,
                session_id,
                speaker=speaker,
                message=message,
                gm_reply=reply,
                channel_id=channel_id,
                metadata={"mode": "game", "rules_blocked": True, "error": str(exc)},
            )
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "reply": reply,
                "rules_blocked": True,
                "error": str(exc),
                "world_map": map_status,
            }
        saved_path = self._autosave_campaign(runtime, campaign_id)
        runtime.log_manager.append_turn(
            campaign_id,
            session_id,
            speaker=speaker,
            message=message,
            gm_reply=reply,
            channel_id=channel_id,
            metadata={"mode": "game", "autosave_path": saved_path},
        )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": reply,
            "saved_path": saved_path,
            "live_context_used": bool(live_context),
            "world_map": map_status,
        }

    def _format_turn_input(self, *, live_context: str, speaker: str, message: str) -> str:
        current = f"{speaker}: {message}"
        if not live_context:
            return current
        return (
            f"{live_context}\n\n"
            "当前玩家输入（只把这一段当作本轮新行动；上方内容是已公开上下文）：\n"
            f"{current}"
        )

    def _pre_session_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        channel_id = str(payload.get("channel_id") or "")
        speaker = str(payload.get("speaker") or "玩家")
        runtime = self._runtime(campaign_id)
        self._touch_speaker(runtime, speaker)
        response = self.pre_session_facilitator.opening()
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker=self.gm_name,
            content=response.message,
            role="assistant",
            channel_id=channel_id,
            metadata={"mode": "pre_session_start", "questions": response.questions},
        )
        saved_path = self._autosave_campaign(runtime, campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": response.message + "\n问题：" + "；".join(response.questions),
            "questions": response.questions,
            "ready_to_start_session_zero": response.ready_to_start_session_zero,
            "saved_path": saved_path,
        }

    def _pre_session_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        runtime = self._runtime(campaign_id)
        self._touch_speaker(runtime, speaker)
        response = self.pre_session_facilitator.handle(runtime.app.world_state.world_profile, speaker, message)
        runtime.app.world_state.apply_world_profile(runtime.app.world_state.world_profile)
        self._record_setup_facts(
            runtime,
            campaign_id=campaign_id,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            facts=response.accepted_facts,
            kind="pre_session_consensus",
            source="pre_session",
        )
        runtime.log_manager.append_turn(
            campaign_id,
            session_id,
            speaker=speaker,
            message=message,
            gm_reply=response.message,
            channel_id=channel_id,
            metadata={
                "mode": "pre_session",
                "questions": response.questions,
                "accepted_facts": response.accepted_facts,
                "ready_to_start_session_zero": response.ready_to_start_session_zero,
            },
        )
        saved_path = self._autosave_campaign(runtime, campaign_id)
        if response.ready_to_start_session_zero:
            state = self.session_gates.activate(
                campaign_id,
                channel_id,
                session_id,
                status="session_zero",
                reason="开团前共识已达成，进入第零章。",
            )
            start = self._session_zero_start(
                {
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "participants": runtime.app.world_state.present_players or [speaker],
                }
            )
            reply = response.message + "\n第零章已开启。\n" + str(start.get("reply", ""))
            return {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "reply": reply,
                "questions": start.get("questions", []),
                "accepted_facts": response.accepted_facts,
                "ready_to_start_session_zero": True,
                "gate": asdict(state),
                "saved_path": saved_path,
            }
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": response.message + ("\n问题：" + "；".join(response.questions) if response.questions else ""),
            "questions": response.questions,
            "accepted_facts": response.accepted_facts,
            "ready_to_start_session_zero": response.ready_to_start_session_zero,
            "saved_path": saved_path,
        }

    def _session_zero_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "session-zero")
        channel_id = str(payload.get("channel_id") or "")
        participants = [str(item) for item in payload.get("participants", []) if str(item).strip()]
        runtime = self._runtime(campaign_id)
        for participant in participants:
            runtime.app.world_state.mark_player_present(participant)
        response = runtime.app.start_session_zero(participants=participants or None)
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker="AI GM",
            content=response.message,
            role="assistant",
            channel_id=channel_id,
            metadata={"mode": "session_zero_start", "stage": str(response.stage.value)},
        )
        saved_path = self._autosave_campaign(runtime, campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": response.message,
            "stage": response.stage.value,
            "questions": response.questions,
            "saved_path": saved_path,
        }

    def _session_zero_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, message, channel_id = self._message_fields(payload)
        runtime = self._runtime(campaign_id)
        self._touch_speaker(runtime, speaker)
        response = runtime.app.discuss_session_zero(speaker, message)
        self._record_setup_facts(
            runtime,
            campaign_id=campaign_id,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            facts=response.accepted_facts,
            kind="session_zero_fact",
            source="session_zero",
        )
        runtime.log_manager.append_turn(
            campaign_id,
            session_id,
            speaker=speaker,
            message=message,
            gm_reply=response.message,
            channel_id=channel_id,
            metadata={
                "mode": "session_zero",
                "stage": str(response.stage.value),
                "questions": response.questions,
                "accepted_facts": response.accepted_facts,
                "suggestions": response.suggestions,
            },
        )
        saved_path = self._autosave_campaign(runtime, campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "reply": response.message,
            "stage": response.stage.value,
            "questions": response.questions,
            "accepted_facts": response.accepted_facts,
            "suggestions": response.suggestions,
            "saved_path": saved_path,
        }

    def _end_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        channel_id = str(payload.get("channel_id") or "")
        title = str(payload.get("title") or "")
        runtime = self._runtime(campaign_id)
        summary = runtime.log_manager.finalize_session(
            campaign_id,
            session_id,
            world_state=runtime.app.world_state,
            title=title,
        )
        runtime.app.story_arc_manager.update_from_session_summary(summary)
        if runtime.app.scene_manager.current_scene is not None:
            runtime.app.scene_manager.end_scene("本场已收团，等待下一场准备。")
        if runtime.app.conflict_manager.state.active:
            runtime.app.conflict_manager.end_scene()
        path = runtime.app.save_campaign_memory(campaign_id)
        runtime.last_saved_path = str(path)
        gate = self.session_gates.deactivate(campaign_id, channel_id, session_id, reason="session_end")
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "path": str(path),
            "summary": asdict(summary),
            "gate": asdict(gate),
        }

    def _list_campaigns(self) -> dict[str, Any]:
        by_id = {item["campaign_id"]: item for item in self._memory_store().list_campaigns()}
        for campaign_id, runtime in self.runtimes.items():
            item = by_id.setdefault(
                campaign_id,
                {
                    "campaign_id": campaign_id,
                    "has_latest_snapshot": False,
                    "slots": [],
                    "updated_at": "",
                },
            )
            item["loaded_in_memory"] = True
            item["loaded_from_disk"] = runtime.loaded_from_disk
            item["last_saved_path"] = runtime.last_saved_path
            item["last_loaded_slot"] = runtime.last_loaded_slot
            item["slot_details"] = self._memory_store().list_save_slots(campaign_id)
        for item in by_id.values():
            item.setdefault("loaded_in_memory", False)
            item.setdefault("loaded_from_disk", False)
            item.setdefault("last_saved_path", "")
            item.setdefault("last_loaded_slot", "")
            item.setdefault("slot_details", self._memory_store().list_save_slots(str(item.get("campaign_id") or "")))
            item.setdefault("active_status", "")
            item.setdefault("active_updated_at", "")
        for state in self._session_gate_states():
            item = by_id.setdefault(
                state.campaign_id,
                {
                    "campaign_id": state.campaign_id,
                    "has_latest_snapshot": self._memory_store().snapshot_exists(state.campaign_id),
                    "slots": [slot["slot"] for slot in self._memory_store().list_save_slots(state.campaign_id)],
                    "updated_at": "",
                    "loaded_in_memory": state.campaign_id in self.runtimes,
                    "loaded_from_disk": bool(self.runtimes.get(state.campaign_id, None) and self.runtimes[state.campaign_id].loaded_from_disk),
                    "last_saved_path": self.runtimes.get(state.campaign_id).last_saved_path if state.campaign_id in self.runtimes else "",
                    "last_loaded_slot": self.runtimes.get(state.campaign_id).last_loaded_slot if state.campaign_id in self.runtimes else "",
                    "slot_details": self._memory_store().list_save_slots(state.campaign_id),
                },
            )
            if state.status != "inactive" and (
                not item.get("active_updated_at") or str(state.updated_at) > str(item.get("active_updated_at") or "")
            ):
                item["active_status"] = state.status
                item["active_updated_at"] = state.updated_at
                item["active_session_id"] = state.session_id
                item["active_channel_id"] = state.channel_id
        current = self._current_campaign_id()
        return {
            "ok": True,
            "current_campaign_id": current,
            "campaigns": sorted(
                by_id.values(),
                key=lambda item: (
                    0 if item.get("campaign_id") == current else 1,
                    0 if item.get("active_status") in {"pre_session", "session_zero", "adventure"} else 1,
                    str(item.get("campaign_id")),
                ),
            ),
        }

    def _current_campaign_payload(self) -> dict[str, Any]:
        campaign_id = self._current_campaign_id()
        runtime = self.runtimes.get(campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "loaded_in_memory": runtime is not None,
            "loaded_from_disk": bool(runtime and runtime.loaded_from_disk),
            "last_saved_path": runtime.last_saved_path if runtime else "",
            "last_loaded_slot": runtime.last_loaded_slot if runtime else "",
        }

    def _new_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or payload.get("name") or "").strip()
        if not campaign_id:
            raise ValueError("新建战役需要 campaign_id。")
        runtime = self._runtime(campaign_id, auto_load=False)
        path = runtime.app.save_campaign_memory(campaign_id)
        runtime.loaded_from_disk = False
        runtime.last_saved_path = str(path)
        runtime.last_loaded_slot = ""
        self._mark_current_campaign(campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "path": str(path),
            "reply": f"已新建战役《{campaign_id}》，并保存为最新快照。",
        }

    def _save_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        slot = str(payload.get("slot") or payload.get("save_slot") or "").strip() or None
        session_id = str(payload.get("session_id") or "default")
        speaker = str(payload.get("speaker") or "系统")
        channel_id = str(payload.get("channel_id") or "")
        self._mark_current_campaign(campaign_id)
        runtime = self._runtime(campaign_id)
        path = runtime.app.save_campaign_memory(campaign_id, slot=slot)
        runtime.last_saved_path = str(path)
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker="系统",
            content=f"{speaker} 保存了战役存档" + (f"：{slot}" if slot else "。"),
            role="system",
            channel_id=channel_id,
            metadata={"mode": "campaign_save", "slot": slot or "", "path": str(path)},
        )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "slot": slot or "",
            "path": str(path),
            "reply": f"战役《{campaign_id}》已保存" + (f"到存档槽「{slot}」。" if slot else "为最新快照。"),
        }

    def _load_campaign(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        campaign_id = str(payload.get("campaign_id") or "default")
        slot = str(payload.get("slot") or payload.get("save_slot") or "").strip() or None
        store = self._memory_store()
        if not store.snapshot_exists(campaign_id, slot=slot):
            return 404, {
                "ok": False,
                "campaign_id": campaign_id,
                "slot": slot or "",
                "error": f"没有找到战役《{campaign_id}》" + (f"的存档槽「{slot}」。" if slot else "的最新快照。"),
            }
        runtime = self._runtime(campaign_id, auto_load=False)
        snapshot = runtime.app.load_campaign_memory(campaign_id, slot=slot)
        runtime.loaded_from_disk = True
        runtime.last_saved_path = str(store._snapshot_path(campaign_id, slot=slot))
        runtime.last_loaded_slot = slot or ""
        self._mark_current_campaign(campaign_id)
        return 200, {
            "ok": True,
            "campaign_id": campaign_id,
            "slot": slot or "",
            "saved_at": snapshot.get("saved_at", ""),
            "loaded_sections": self._snapshot_loaded_sections(snapshot),
            "reply": f"战役《{campaign_id}》已读档" + (f"：{slot}。" if slot else "最新快照。"),
            "attendance": runtime.app.world_state.attendance_snapshot(),
        }

    def _import_chat_log(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        campaign_id = str(payload.get("campaign_id") or "default").strip() or "default"
        session_id = str(payload.get("session_id") or "default").strip() or "default"
        channel_id = str(payload.get("channel_id") or "").strip()
        speaker = str(payload.get("speaker") or "迁移导入").strip() or "迁移导入"
        chat_log = str(payload.get("chat_log") or payload.get("transcript") or "").strip()
        target_slot = str(payload.get("target_slot") or payload.get("slot") or payload.get("save_slot") or "").strip()
        base_slot = str(payload.get("base_slot") or "").strip()
        dry_run = self._truthy(payload.get("dry_run"))
        store_raw_log = self._truthy(payload.get("store_raw_log"))
        import_payload = payload.get("import_payload")

        if not chat_log and not isinstance(import_payload, dict):
            return 400, {"ok": False, "campaign_id": campaign_id, "error": "需要 chat_log，或传入 import_payload。"}

        store = self._memory_store()
        runtime = self._runtime(campaign_id, auto_load=not bool(base_slot))
        if base_slot:
            if not store.snapshot_exists(campaign_id, slot=base_slot):
                return 404, {
                    "ok": False,
                    "campaign_id": campaign_id,
                    "slot": base_slot,
                    "error": f"没有找到作为导入基底的存档槽「{base_slot}」。",
                }
            snapshot = runtime.app.load_campaign_memory(campaign_id, slot=base_slot)
            runtime.loaded_from_disk = True
            runtime.last_loaded_slot = base_slot
            runtime.last_saved_path = str(store._snapshot_path(campaign_id, slot=base_slot))
        else:
            snapshot = (
                store.load_campaign(
                    campaign_id,
                    world_state=runtime.app.world_state,
                    character_manager=runtime.app.character_manager,
                    clock_manager=runtime.app.clock_manager,
                    conflict_manager=runtime.app.conflict_manager,
                    scene_manager=runtime.app.scene_manager,
                    ritual_manager=runtime.app.ritual_manager,
                    project_manager=runtime.app.project_manager,
                )
                if store.snapshot_exists(campaign_id) and not runtime.loaded_from_disk
                else {}
            )
            if snapshot:
                runtime.loaded_from_disk = True

        importer = self._chat_log_importer()
        extraction_warnings: list[str] = []
        source = "provided"
        fallback_used = False
        if isinstance(import_payload, dict):
            normalized = importer.normalize_payload(import_payload)
        else:
            result = importer.extract(
                chat_log=chat_log,
                campaign_id=campaign_id,
                existing_context=self._import_existing_context(runtime),
            )
            normalized = result.import_payload
            extraction_warnings = result.warnings
            source = result.source
            fallback_used = result.fallback_used

        preview = import_payload_preview(normalized)
        preview["warnings"] = [*preview.get("warnings", []), *extraction_warnings]
        if dry_run:
            return 200, {
                "ok": True,
                "dry_run": True,
                "campaign_id": campaign_id,
                "source": source,
                "fallback_used": fallback_used,
                "preview": preview,
                "import_payload": normalized,
                "reply": "已完成迁移预览；尚未写入存档。",
            }

        counts = importer.apply_to_app(runtime.app, normalized, source="chat_log_import")
        path = runtime.app.save_campaign_memory(campaign_id, slot=target_slot or None)
        runtime.loaded_from_disk = True
        runtime.last_saved_path = str(path)
        runtime.last_loaded_slot = target_slot or runtime.last_loaded_slot
        self._mark_current_campaign(campaign_id)
        artifact_path = self._write_import_artifact(
            campaign_id,
            session_id=session_id,
            channel_id=channel_id,
            speaker=speaker,
            import_payload=normalized,
            preview=preview,
            source=source,
            fallback_used=fallback_used,
            warnings=preview.get("warnings", []),
            chat_log=chat_log if store_raw_log else "",
        )
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker="系统",
            content=f"{speaker} 导入了一份迁移聊天记录，已写入结构化存档。",
            role="system",
            channel_id=channel_id,
            metadata={
                "mode": "campaign_import",
                "source": source,
                "fallback_used": fallback_used,
                "slot": target_slot,
                "path": str(path),
                "artifact_path": artifact_path,
                "counts": counts,
            },
        )
        return 200, {
            "ok": True,
            "dry_run": False,
            "campaign_id": campaign_id,
            "slot": target_slot,
            "path": str(path),
            "artifact_path": artifact_path,
            "source": source,
            "fallback_used": fallback_used,
            "preview": preview,
            "counts": counts,
            "reply": f"已把聊天记录导入《{campaign_id}》" + (f"的存档槽「{target_slot}」。" if target_slot else "的最新快照。"),
        }

    def _delete_campaign(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        campaign_id = str(payload.get("campaign_id") or "default")
        slot = str(payload.get("slot") or payload.get("save_slot") or "").strip() or None
        delete_all = self._truthy(payload.get("delete_all") or payload.get("all"))
        confirm = str(payload.get("confirm") or "").strip()
        store = self._memory_store()

        if delete_all:
            if confirm not in {"确认删除", f"确认删除{campaign_id}", campaign_id}:
                return 400, {
                    "ok": False,
                    "campaign_id": campaign_id,
                    "error": "删除整个战役需要 confirm=\"确认删除\"。这个操作会同时删除日志、故事记忆和所有存档槽。",
                }
            result = store.delete_campaign(campaign_id)
            self.runtimes.pop(campaign_id, None)
            if self.current_campaign_id == campaign_id:
                self.current_campaign_id = ""
            if not result["deleted"]:
                return 404, {
                    "ok": False,
                    **result,
                    "error": f"没有找到战役《{campaign_id}》的本地目录。",
                }
            return 200, {
                "ok": True,
                **result,
                "reply": f"战役《{campaign_id}》的本地目录已删除。日志、故事记忆、最新快照和命名存档都已经移除。",
            }

        result = store.delete_save(campaign_id, slot=slot)
        runtime = self.runtimes.get(campaign_id)
        if runtime and runtime.last_saved_path == result["path"]:
            runtime.last_saved_path = ""
        if not result["deleted"]:
            return 404, {
                "ok": False,
                **result,
                "error": f"没有找到战役《{campaign_id}》" + (f"的存档槽「{slot}」。" if slot else "的最新快照。"),
            }
        return 200, {
            "ok": True,
            **result,
            "reply": f"已删除《{campaign_id}》" + (f"的存档槽「{slot}」。" if slot else "的最新快照。"),
        }

    def _session_away(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, _message, channel_id = self._message_fields(payload)
        player = str(payload.get("player") or speaker or "玩家")
        reason = str(payload.get("reason") or payload.get("message") or "").strip()
        runtime = self._runtime(campaign_id)
        runtime.app.world_state.mark_player_absent(player, reason)
        runtime.app.world_state.record_memory_event(
            f"桌面状态：{player} 临时离席" + (f"（{reason}）" if reason else "。"),
            kind="attendance",
            entities=[player],
            tags=["attendance", "away"],
            source="http",
        )
        path = runtime.app.save_campaign_memory(campaign_id)
        runtime.last_saved_path = str(path)
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker="系统",
            content=f"{player} 临时离席，已自动保存最新快照。",
            role="system",
            channel_id=channel_id,
            metadata={"mode": "session_away", "player": player, "reason": reason, "path": str(path)},
        )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "player": player,
            "attendance": runtime.app.world_state.attendance_snapshot(),
            "path": str(path),
            "reply": f"已记录 {player} 临时离席，并自动保存《{campaign_id}》。需要她回来后用读档或继续当前团都可以。",
        }

    def _session_back(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id, session_id, speaker, _message, channel_id = self._message_fields(payload)
        player = str(payload.get("player") or speaker or "玩家")
        runtime = self._runtime(campaign_id)
        runtime.app.world_state.mark_player_present(player)
        runtime.app.world_state.record_memory_event(
            f"桌面状态：{player} 回到本场。",
            kind="attendance",
            entities=[player],
            tags=["attendance", "back"],
            source="http",
        )
        path = runtime.app.save_campaign_memory(campaign_id)
        runtime.last_saved_path = str(path)
        runtime.log_manager.append_message(
            campaign_id,
            session_id,
            speaker="系统",
            content=f"{player} 回到本场，已自动保存最新快照。",
            role="system",
            channel_id=channel_id,
            metadata={"mode": "session_back", "player": player, "path": str(path)},
        )
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "player": player,
            "attendance": runtime.app.world_state.attendance_snapshot(),
            "path": str(path),
            "reply": f"欢迎回来，{player}。我已经把你标记为在场，并保存了《{campaign_id}》当前状态。",
        }

    def _session_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        channel_id = str(payload.get("channel_id") or "")
        runtime = self._runtime(campaign_id)
        scene = runtime.app.scene_manager.current_scene
        gate = self.session_gates.get(campaign_id, channel_id, session_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "gate": asdict(gate),
            "attendance": runtime.app.world_state.attendance_snapshot(),
            "current_scene": scene.name if scene else "",
            "game_phase": runtime.app.conflict_manager.format_phase()
            if runtime.app.conflict_manager.state.active
            else runtime.app.scene_manager.format_phase(),
            "current_actor": runtime.app.conflict_manager.state.current_actor(),
            "loaded_from_disk": runtime.loaded_from_disk,
            "last_saved_path": runtime.last_saved_path,
        }

    def _session_gate(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        channel_id = str(payload.get("channel_id") or "")
        status = str(payload.get("status") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if status in {"pre_session", "session_zero", "adventure"}:
            runtime = self._runtime(campaign_id)
            if status == "adventure":
                blockers = self._adventure_start_blockers(runtime)
                if blockers:
                    state = self.session_gates.get(campaign_id, channel_id, session_id)
                    return {
                        "ok": True,
                        "campaign_id": campaign_id,
                        "session_id": session_id,
                        "gate": asdict(state),
                        "reply": self._format_adventure_blocked_reply(blockers),
                        "blockers": blockers,
                        "hero_creation": blockers.get("hero_creation", {}),
                        "world_map": None,
                        "blocked": True,
                    }
            state = self.session_gates.activate(campaign_id, channel_id, session_id, status=status, reason=reason)
        elif status == "paused":
            state = self.session_gates.pause(campaign_id, channel_id, session_id, reason=reason)
        elif status == "inactive":
            state = self.session_gates.deactivate(campaign_id, channel_id, session_id, reason=reason)
        else:
            state = self.session_gates.get(campaign_id, channel_id, session_id)
        map_status = None
        if state.status == "adventure":
            runtime = self._runtime(campaign_id)
            map_status = runtime.app.ensure_world_map_for_adventure(max_attempts=2)
            if map_status.get("status") == "generated":
                self._autosave_campaign(runtime, campaign_id)
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "session_id": session_id,
            "gate": asdict(state),
            "reply": self._gate_reply(state),
            "world_map": map_status,
        }

    def _adventure_start_blockers(self, runtime: CampaignRuntime) -> dict[str, Any]:
        session_zero_state = runtime.app.session_zero_manager.state
        has_session_zero_character_context = bool(
            session_zero_state.active
            or session_zero_state.participants
            or session_zero_state.world.hero_drafts
        )
        if not has_session_zero_character_context:
            return {}
        world = session_zero_state.world
        participants = [participant.name for participant in session_zero_state.participants]
        if not participants:
            participants = list(runtime.app.world_state.present_players)
        if not participants:
            participants = [draft.player_name or key for key, draft in world.hero_drafts.items()]

        draft_items: list[tuple[str, Any]] = []
        missing_by_player: dict[str, list[str]] = {}
        for player in participants:
            draft_key = ""
            draft = None
            if player in world.hero_drafts:
                draft_key = player
                draft = world.hero_drafts[player]
            else:
                for key, candidate in world.hero_drafts.items():
                    if candidate.player_name == player or candidate.hero_name == player:
                        draft_key = key
                        draft = candidate
                        break
            if draft is None:
                missing_by_player[player or "未命名玩家"] = ["完整角色草稿"]
                continue
            draft_items.append((draft_key, draft))

        if not participants and not world.hero_drafts:
            missing_by_player["玩家角色"] = ["完整角色草稿"]
        if missing_by_player:
            return {
                "reason": "character_creation_incomplete",
                "hero_creation": {"ready": False, "missing_by_player": missing_by_player},
            }

        for draft_key, draft in draft_items:
            label = draft.hero_name or draft.player_name or draft_key
            if draft.hero_name and runtime.app.character_manager.exists(draft.hero_name):
                character = runtime.app.character_manager.get(draft.hero_name)
                if "pc" in character.traits:
                    continue
            validation = runtime.app.validate_hero_draft(draft_key)
            if not validation.ready:
                missing_by_player[label] = list(validation.missing_fields) + list(validation.errors)
                continue
            if not draft.confirmed:
                missing_by_player[label] = ["确认角色并正式建卡"]
                continue
            try:
                runtime.app.create_player_character_from_draft(draft_key)
            except ValueError as exc:
                missing_by_player[label] = [str(exc)]

        if missing_by_player:
            return {
                "reason": "character_creation_incomplete",
                "hero_creation": {"ready": False, "missing_by_player": missing_by_player},
            }

        missing_formal = {
            (draft.hero_name or draft.player_name or draft_key): ["正式 PC 未创建"]
            for draft_key, draft in draft_items
            if not (draft.hero_name and runtime.app.character_manager.exists(draft.hero_name))
        }
        if missing_formal:
            return {
                "reason": "character_creation_incomplete",
                "hero_creation": {"ready": False, "missing_by_player": missing_formal},
            }
        return {}

    def _format_adventure_blocked_reply(self, blockers: dict[str, Any]) -> str:
        missing = blockers.get("hero_creation", {}).get("missing_by_player", {})
        lines = ["还不能进入第一章：至少所有玩家角色都需要完整创建并可用于规则结算。"]
        if isinstance(missing, dict) and missing:
            for player, fields in missing.items():
                field_text = "、".join(str(field) for field in fields) if isinstance(fields, list) else str(fields)
                lines.append(f"- {player}：缺 {field_text}")
        lines.append("请先补完这些角色项；角色未创建完不能开启跑团。")
        return "\n".join(lines)

    def _format_rules_blocked_reply(self, exc: Exception) -> str:
        text = str(exc)
        if "尚未掌握【" in text:
            skill_match = re.search(r"尚未掌握【([^】]+)】", text)
            skill_text = f"【{skill_match.group(1)}】" if skill_match else "对应仪式技能"
            return (
                f"规则结算拦截：当前行动还不满足规则前提，需要先掌握{skill_text}。\n"
                "这不是角色行动失败；请改用已掌握的法术、普通检定，或重新描述一个不需要该技能前提的做法。"
            )
        if "is not a valid RitualPotency" in text:
            invalid = text.split(" is not a valid RitualPotency", 1)[0].strip("'\" ")
            return (
                f"规则结算拦截：当前仪式参数不符合规则，{invalid or '这个值'}不是有效的仪式效力。\n"
                "仪式效力请使用：轻微、中等、强大、极强。"
                "如果你想表达“小范围”，那属于仪式范围，请使用：个人、小范围、大范围、巨大范围。"
            )
        if "is not a valid RitualScope" in text:
            invalid = text.split(" is not a valid RitualScope", 1)[0].strip("'\" ")
            return (
                f"规则结算拦截：当前仪式参数不符合规则，{invalid or '这个值'}不是有效的仪式范围。\n"
                "仪式范围请使用：个人、小范围、大范围、巨大范围。"
                "效力则使用：轻微、中等、强大、极强。"
            )
        if isinstance(exc, KeyError):
            missing = text.strip("'\" ")
            if "内部恢复重试" in missing or "npc_action_type" in missing:
                missing = "执行者、目标、动作类型或命刻名称"
            return (
                f"规则结算拦截：当前行动缺少规则结算所需字段：{missing}。\n"
                "这不是角色行动失败，而是动作参数不完整。请重新描述行动，或改成普通检定、已掌握法术或已满足前提的技能。"
            )
        return (
            "规则结算拦截：当前行动还不满足规则前提，暂不结算为失败。\n"
            "请换成已掌握的法术、普通检定，或重新描述一个不需要该技能前提的做法。"
        )

    def _gate_reply(self, state: SessionGateState) -> str:
        if state.status == "pre_session":
            return "FU-GM 当前正在接管开团前共识对齐。"
        if state.status == "session_zero":
            return "FU-GM 当前正在接管第零章。"
        if state.status == "adventure":
            return "FU-GM 当前正在接管跑团会话。"
        if state.status == "paused":
            return "FU-GM 当前已暂停，等待继续或收团信号。"
        return "FU-GM 当前未接管该会话。"

    def _autosave_campaign(self, runtime: CampaignRuntime, campaign_id: str) -> str:
        try:
            path = runtime.app.save_campaign_memory(campaign_id)
        except Exception as first_error:
            try:
                time.sleep(0.05)
                path = runtime.app.save_campaign_memory(campaign_id)
            except Exception as second_error:
                runtime.last_saved_path = f"autosave_failed: {second_error}"
                return ""
            runtime.last_saved_path = str(path)
            return str(path)
        runtime.last_saved_path = str(path)
        return str(path)

    def _record_setup_facts(
        self,
        runtime: CampaignRuntime,
        *,
        campaign_id: str,
        session_id: str,
        channel_id: str,
        speaker: str,
        facts: list[str],
        kind: str,
        source: str,
    ) -> None:
        existing = {
            (event.kind, event.summary)
            for event in runtime.app.world_state.memory_events
            if "setup" in event.tags or event.kind in {"pre_session_consensus", "session_zero_fact"}
        }
        for fact in facts:
            summary = str(fact or "").strip()
            if not summary or (kind, summary) in existing:
                continue
            runtime.app.world_state.record_memory_event(
                summary,
                kind=kind,
                visibility="public",
                entities=[speaker] if speaker else [],
                tags=["setup", kind],
                source=source,
                payload={
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "speaker": speaker,
                },
            )

    def _latest_gate_for_campaign(self, campaign_id: str) -> SessionGateState | None:
        preferred_status = {"pre_session": 0, "session_zero": 1, "adventure": 2, "paused": 3, "inactive": 4}
        candidates = [
            state
            for state in self._session_gate_states()
            if state.campaign_id == campaign_id
            and state.status in preferred_status
            and (state.status != "inactive" or state.reason == "session_end")
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda state: (
                preferred_status.get(state.status, 9),
                state.updated_at or state.started_at,
            ),
            reverse=False,
        )
        candidates.sort(key=lambda state: state.updated_at or state.started_at, reverse=True)
        return candidates[0]

    def _resolve_audit_scope(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_campaign = str(payload.get("campaign_id") or "default")
        requested_session = str(payload.get("session_id") or "default")
        requested_channel = str(payload.get("channel_id") or "")
        campaign_id = requested_campaign
        session_id = requested_session
        channel_id = requested_channel
        reason = "request"

        if not requested_channel and requested_session in {"", "default"}:
            latest_gate = self._latest_gate_for_campaign(campaign_id)
            if latest_gate is None and requested_campaign == "default":
                current_campaign = self._current_campaign_id()
                if current_campaign and current_campaign != "default":
                    campaign_id = current_campaign
                    latest_gate = self._latest_gate_for_campaign(campaign_id)
                    reason = "current_campaign"
            if latest_gate is not None:
                session_id = latest_gate.session_id or latest_gate.channel_id or "default"
                channel_id = latest_gate.channel_id
                if reason == "request":
                    reason = "latest_active_gate"
        elif not requested_channel:
            for state in self._session_gate_states():
                if state.campaign_id == campaign_id and state.session_id == requested_session:
                    channel_id = state.channel_id
                    reason = "matched_session_gate"
                    break

        gate = self.session_gates.get(campaign_id, channel_id, session_id)
        return {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "gate": gate,
            "requested": {
                "campaign_id": requested_campaign,
                "session_id": requested_session,
                "channel_id": requested_channel,
            },
            "resolved_from": reason,
        }

    def _setup_audit_payload(
        self,
        app: SceneOrchestrator,
        transcript_entries: list,
        *,
        limit: int,
    ) -> dict[str, Any]:
        world = app.world_state.world_profile
        recorded_consensus = {
            "tone_preferences": list(world.tone_preferences),
            "playstyle_themes": list(world.playstyle_themes),
            "party_dynamic": world.party_dynamic,
            "description_style": world.description_style,
            "violence_guideline": world.violence_guideline,
            "evil_guidelines": list(world.evil_guidelines),
            "romance_guideline": world.romance_guideline,
            "consensus_notes": list(world.consensus_notes),
            "safety_lines": list(world.safety_lines),
            "safety_veils": list(world.safety_veils),
        }
        world_records = {
            "campaign_title": world.campaign_title,
            "world_style": world.world_style,
            "map_card": world.map_card,
            "magic_tech_role": world.magic_tech_role,
            "group_concept": world.group_concept,
            "starting_region": world.starting_region,
            "core_themes": list(world.core_themes),
            "major_locations": dict(world.major_locations),
            "kingdoms": dict(world.kingdoms),
            "kingdom_contributors": dict(world.kingdom_contributors),
            "historical_events": list(world.historical_events),
            "historical_event_contributors": dict(world.historical_event_contributors),
            "factions": dict(world.factions),
            "villain_seeds": list(world.villain_seeds),
            "villain_mirrors": list(world.villain_mirrors),
            "mysteries": list(world.mysteries),
            "mystery_contributors": dict(world.mystery_contributors),
            "world_threats": list(world.world_threats),
            "threat_contributors": dict(world.threat_contributors),
            "selected_first_act_summary": world.selected_first_act_summary,
            "starting_bond_suggestions": list(world.starting_bond_suggestions),
        }
        checklist = [
            {"name": "基调偏好", "ready": bool(world.tone_preferences), "value": "；".join(world.tone_preferences[:3])},
            {"name": "描述风格", "ready": bool(world.description_style), "value": world.description_style},
            {"name": "队伍关系", "ready": bool(world.party_dynamic), "value": world.party_dynamic},
            {
                "name": "安全边界",
                "ready": bool(world.safety_lines or world.safety_veils or world.consensus_notes or world.violence_guideline or world.romance_guideline),
                "value": "；".join(
                    item
                    for item in (
                        world.violence_guideline,
                        world.romance_guideline,
                        *world.safety_lines[:2],
                        *world.safety_veils[:2],
                    )
                    if item
                ),
            },
            {"name": "地图卡", "ready": bool(world.map_card), "value": world.map_card},
            {"name": "魔法与科技", "ready": bool(world.magic_tech_role), "value": world.magic_tech_role},
            {"name": "主要王国/国家", "ready": bool(world.kingdoms), "value": "、".join(world.kingdoms.keys())},
            {"name": "重大历史事件", "ready": bool(world.historical_events), "value": "；".join(world.historical_events[:2])},
            {"name": "世界奥秘", "ready": bool(world.mysteries), "value": "；".join(world.mysteries[:2])},
            {"name": "世界性威胁", "ready": bool(world.world_threats), "value": "；".join([*world.world_threats[:2], *world.villain_seeds[:1]])},
            {"name": "世界风貌", "ready": bool(world.world_style or world.core_themes), "value": world.world_style or "；".join(world.core_themes[:3])},
            {"name": "小队原型", "ready": bool(world.group_concept), "value": world.group_concept},
            {"name": "起始区域", "ready": bool(world.starting_region), "value": world.starting_region},
        ]
        recent_facts: list[dict[str, str]] = []
        for entry in transcript_entries:
            if not isinstance(entry.metadata, dict):
                continue
            for fact in entry.metadata.get("accepted_facts", []) or []:
                text = str(fact or "").strip()
                if text:
                    recent_facts.append(
                        {
                            "created_at": entry.created_at,
                            "speaker": entry.speaker,
                            "mode": str(entry.metadata.get("mode") or ""),
                            "fact": text,
                        }
                    )
        return {
            "pre_session_ready": world.pre_session_ready,
            "completed": world.completed,
            "checklist": checklist,
            "recorded_consensus": recorded_consensus,
            "world_records": world_records,
            "hero_drafts": {
                key: {
                    "player_name": draft.player_name,
                    "hero_name": draft.hero_name,
                    "identity": draft.identity,
                    "theme": draft.theme,
                    "origin": draft.origin,
                    "classes": dict(draft.classes),
                    "attributes": dict(draft.attributes),
                    "skills": dict(draft.skills),
                    "spells": list(draft.spells),
                    "bound_arcana": list(draft.bound_arcana),
                    "equipment": list(draft.equipment),
                    "bonds": list(draft.bonds),
                    "notes": list(draft.notes),
                    "open_questions": list(draft.open_questions),
                    "concept_notes": list(draft.notes),
                    "missing_fields": list(draft.open_questions),
                    "confirmed": draft.confirmed,
                }
                for key, draft in world.hero_drafts.items()
            },
            "recent_accepted_facts": recent_facts[-limit:],
            "open_questions": list(world.open_questions),
        }

    def _gm_guidance_audit_payload(self, app: SceneOrchestrator) -> dict[str, Any]:
        world = app.world_state.world_profile
        guidance = summarize_guidance_for_prompt(
            world,
            location_limit=None,
            detailed_locations=True,
            include_all_locations=True,
        )
        known_location_names = {name for name in world.major_locations if name}
        known_location_names.update(name for name in app.world_state.map_locations if name)
        known_location_names.update(
            location.name
            for location in app.world_state.map_locations.values()
            if getattr(location, "name", "")
        )
        prepared_locations: list[dict[str, Any]] = []
        for raw in guidance.get("prepared_locations", []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
            is_public = name in known_location_names
            prepared_locations.append(
                {
                    **raw,
                    "status": "public" if is_public else "backstage_candidate",
                    "status_label": "已公开/已登记" if is_public else "后台候选",
                    "can_reveal_when": "玩家追踪相关线索、使用物语点引入，或剧情自然需要时。",
                }
            )
        return {
            "inspiration_tags": list(guidance.get("inspiration_tags", [])),
            "stored_inspiration_tags": list(world.gm_inspiration_tags),
            "principles": list(guidance.get("principles", [])),
            "stored_guidance_notes": list(world.gm_guidance_notes),
            "question_angles": list(guidance.get("question_angles", [])),
            "story_beats": list(guidance.get("story_beats", [])),
            "stored_story_beats": list(world.gm_story_beats),
            "hero_creation_prompts": list(guidance.get("hero_creation_prompts", [])),
            "prepared_locations": prepared_locations,
            "stored_prepared_locations": dict(world.gm_prepared_locations),
            "known_location_names": sorted(known_location_names),
            "usage_note": (
                "这些内容是 GM 后台创作指导，不是玩家已知事实；不要要求玩家选择扩展或世界类型。"
                "预备地点只有被玩家追踪、物语点引入或剧情自然需要时才写入公开世界。"
            ),
        }

    def _play_process_audit_payload(self, app: SceneOrchestrator) -> dict[str, Any]:
        return summarize_play_process_for_prompt(
            app.scene_manager.current_scene,
            conflict_active=app.conflict_manager.state.active,
        )

    def _story_arc_audit_payload(self, app: SceneOrchestrator, *, include_private: bool = False) -> dict[str, Any]:
        return app.story_arc_manager.audit_payload(include_private=include_private)

    def _audit_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = self._resolve_audit_scope(payload)
        campaign_id = scope["campaign_id"]
        session_id = scope["session_id"]
        channel_id = scope["channel_id"]
        limit = self._int_value(payload.get("limit"), default=40, minimum=1, maximum=200)
        include_private = self._truthy(payload.get("include_private"))
        runtime = self._runtime(campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        conflict_state = app.conflict_manager.state
        gate = scope["gate"]
        transcript_entries = runtime.log_manager.load_transcript(campaign_id, session_id)
        visible_transcript = [
            asdict(entry)
            for entry in transcript_entries[-limit:]
            if include_private or entry.role not in {"gm_private", "system_private", "private"}
        ]
        memory_events = [
            event
            for event in app.world_state.memory_events[-limit:]
            if include_private or event.visibility.value == "public"
        ]
        gm_secrets = []
        for secret in app.world_state.gm_secrets.values():
            secret_payload = asdict(secret)
            if not include_private:
                secret_payload.pop("content", None)
                secret_payload.pop("revisions", None)
            gm_secrets.append(secret_payload)
        phase_display = (
            app.conflict_manager.format_phase()
            if conflict_state.active
            else app.scene_manager.format_phase()
        )
        if not conflict_state.active and scene is None and gate.status == "inactive" and gate.reason == "session_end":
            phase_display = "已收团，等待下一场准备"

        return self._json_safe(
            {
                "ok": True,
                "campaign_id": campaign_id,
                "session_id": session_id,
                "channel_id": channel_id,
                "scope": {
                    "requested": scope["requested"],
                    "resolved_from": scope["resolved_from"],
                    "available_sessions": [
                        {
                            "campaign_id": state.campaign_id,
                            "session_id": state.session_id,
                            "channel_id": state.channel_id,
                            "status": state.status,
                            "updated_at": state.updated_at,
                        }
                        for state in self._session_gate_states()
                        if state.campaign_id == campaign_id
                    ],
                },
                "private_included": include_private,
                "runtime": self._runtime_status_payload(campaign_id, runtime),
                "gate": asdict(gate),
                "attendance": app.world_state.attendance_snapshot(),
                "phase": {
                    "current_scene": asdict(scene) if scene else None,
                    "display": phase_display,
                    "current_actor": conflict_state.current_actor(),
                },
                "conflict": {
                    "active": conflict_state.active,
                    "scene_name": conflict_state.scene_name,
                    "round_number": conflict_state.round_number,
                    "turn_order": list(conflict_state.turn_order),
                    "current_turn_index": conflict_state.current_turn_index,
                    "current_bonus_actor": conflict_state.current_bonus_actor,
                    "queued_turns": list(conflict_state.queued_turns),
                    "acted_this_round": list(conflict_state.acted_this_round),
                    "pending_assists": {leader: list(helpers) for leader, helpers in conflict_state.pending_assists.items()},
                    "held_actions": list(conflict_state.held_actions),
                    "enemy_action_counts": dict(conflict_state.enemy_action_counts),
                    "ultima_points": dict(conflict_state.ultima_points),
                    "enemy_ranks": dict(conflict_state.enemy_ranks),
                    "villains": list(conflict_state.villains),
                    "active_effects": [asdict(effect) for effect in conflict_state.active_effects],
                    "combat_log": [asdict(entry) for entry in conflict_state.combat_log[-limit:]],
                },
                "clocks": [asdict(clock) for clock in app.clock_manager.all()],
                "characters": [self._character_audit_payload(app, character.name) for character in app.character_manager.all()],
                "setup": self._setup_audit_payload(app, transcript_entries, limit=limit),
                "gm_guidance": self._gm_guidance_audit_payload(app),
                "play_process": self._play_process_audit_payload(app),
                "story_arc": self._story_arc_audit_payload(app, include_private=include_private),
                "world": {
                    "profile": asdict(app.world_state.world_profile),
                    "pillars": list(app.world_state.session_pillars),
                    "map_locations": [asdict(location) for location in app.world_state.map_locations.values()],
                    "map_routes": [asdict(route) for route in app.world_state.map_routes.values()],
                    "party_sheet": asdict(app.world_state.party_sheet) if app.world_state.party_sheet else None,
                    "world_sheet": asdict(app.world_state.world_sheet) if app.world_state.world_sheet else None,
                    "safety": app.safety_guidance(),
                    "public_memory_count": len(app.world_state.memories),
                    "memory_event_count": len(app.world_state.memory_events),
                    "map_artifacts": self._map_artifacts(
                        app.world_state.memory_events,
                        limit=limit,
                        include_private=include_private,
                    ),
                    "recent_public_memories": app.world_state.memories[-limit:],
                    "recent_memory_events": [asdict(event) for event in memory_events],
                    "world_profile_update_audit": [
                        asdict(event)
                        for event in app.world_state.world_profile_update_audit(
                            limit=limit,
                            include_private=include_private,
                        )
                    ],
                    "gm_secrets": gm_secrets,
                    "persistent_changes": [asdict(change) for change in app.world_state.persistent_changes[-limit:]],
                },
                "logs": {
                    "transcript_path": str(runtime.log_manager.transcript_path(campaign_id, session_id)),
                    "transcript_txt_path": str(runtime.log_manager.transcript_txt_path(campaign_id, session_id)),
                    "summary_path": str(runtime.log_manager.summary_path(campaign_id, session_id)),
                    "memory_path": str(runtime.log_manager.memory_path(campaign_id, session_id)),
                    "recent_transcript": visible_transcript,
                    "story_summaries": [asdict(summary) for summary in runtime.log_manager.load_story_summaries(campaign_id)[-10:]],
                    "save_slots": self._memory_store().list_save_slots(campaign_id),
                    "saved_memory_events": self._read_saved_memory_events(campaign_id, limit=limit, include_private=include_private),
                },
                "llm": {
                    "use_llm": self.use_llm,
                    "gm_name": self.gm_name,
                    "deepseek_roleplay_mode": self.deepseek_roleplay_mode,
                    "action_brain": app.action_brain.__class__.__name__,
                    "expressor": app.expressor.__class__.__name__,
                    "action_last_error": str(getattr(app.action_brain, "last_error", "") or ""),
                    "action_last_used_fallback": bool(getattr(app.action_brain, "last_used_fallback", False)),
                    "action_recovery_attempts": list(getattr(app.action_brain, "last_recovery_attempts", []) or []),
                    "action_recent_recoveries": list(getattr(app.action_brain, "recent_recoveries", []) or []),
                    "session_zero_facilitator": app.session_zero_facilitator.__class__.__name__,
                    "session_zero_last_error": str(
                        getattr(app.session_zero_facilitator, "last_error", "") or ""
                    ),
                    "session_zero_last_used_fallback": bool(
                        getattr(app.session_zero_facilitator, "last_used_fallback", False)
                    ),
                    "session_zero_recovery_attempts": list(
                        getattr(app.session_zero_facilitator, "last_recovery_attempts", []) or []
                    ),
                    "session_zero_recent_recoveries": list(
                        getattr(app.session_zero_facilitator, "recent_recoveries", []) or []
                    ),
                    "action_client": self._component_client_payload(app.action_brain),
                    "expressor_client": self._component_client_payload(app.expressor),
                    "session_zero_client": self._component_client_payload(app.session_zero_facilitator),
                    "casual_client": self._component_client_payload(runtime.casual_chat),
                    "summarizer_client": self._component_client_payload(runtime.log_manager.summarizer),
                },
            }
        )

    def _service_status_payload(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": int((now - self.started_at).total_seconds()),
            "data_root": str(self.data_root),
            "use_llm": self.use_llm,
            "current_campaign_id": self.current_campaign_id,
            "loaded_campaigns": sorted(self.runtimes),
        }

    def _runtime_status_payload(self, campaign_id: str, runtime: CampaignRuntime) -> dict[str, Any]:
        app = runtime.app
        snapshot = app.memory_store.build_snapshot(
            campaign_id,
            world_state=app.world_state,
            character_manager=app.character_manager,
            clock_manager=app.clock_manager,
            conflict_manager=app.conflict_manager,
            scene_manager=app.scene_manager,
            ritual_manager=app.ritual_manager,
            project_manager=app.project_manager,
            story_arc_manager=app.story_arc_manager,
        )
        conflict_state = app.conflict_manager.state
        return {
            "loaded_in_memory": campaign_id in self.runtimes,
            "loaded_from_disk": runtime.loaded_from_disk,
            "last_saved_path": runtime.last_saved_path,
            "last_loaded_slot": runtime.last_loaded_slot,
            "data_root": str(self.data_root),
            "service": self._service_status_payload(),
            "astrbot_bridge": self._astrbot_status_payload(),
            "http": self._http_telemetry_payload(),
            "pipeline": app.pipeline_telemetry(),
            "world_map": app.world_map_generation_status(),
            "conflict_queue": {
                "active": conflict_state.active,
                "current_actor": conflict_state.current_actor(),
                "acted_this_round": list(conflict_state.acted_this_round),
                "pending_assists": {leader: list(helpers) for leader, helpers in conflict_state.pending_assists.items()},
                "held_actions": list(conflict_state.held_actions),
                "queued_turns": list(conflict_state.queued_turns),
            },
            "loaded_sections": self._snapshot_loaded_sections(snapshot),
        }

    def _astrbot_status_payload(self) -> dict[str, Any]:
        last_seen = str(self.astrbot_bridge_state.get("last_seen_at") or "")
        seconds_since = None
        connected_recently = False
        if last_seen:
            try:
                parsed = datetime.fromisoformat(last_seen)
                seconds_since = int((datetime.now(timezone.utc) - parsed).total_seconds())
                connected_recently = seconds_since <= 300
            except ValueError:
                seconds_since = None
        return {
            **self.astrbot_bridge_state,
            "connected_recently": connected_recently,
            "seconds_since_last_seen": seconds_since,
            "status_label": "最近接入" if connected_recently else "未检测到近期 AstrBot 消息",
        }

    def _http_telemetry_payload(self) -> dict[str, Any]:
        recent = self.recent_http_spans[-20:]
        slowest = sorted(self.recent_http_spans, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:10]
        averages: dict[str, dict[str, int]] = {}
        grouped: dict[str, list[int]] = {}
        for span in self.recent_http_spans:
            key = f"{span.get('method')} {span.get('route')}"
            grouped.setdefault(key, []).append(int(span.get("elapsed_ms", 0)))
        for key, values in grouped.items():
            averages[key] = {
                "count": len(values),
                "average_ms": int(sum(values) / len(values)) if values else 0,
                "max_ms": max(values) if values else 0,
            }
        return {
            "recent_requests": recent,
            "slowest_recent": slowest,
            "by_route": averages,
        }

    def _component_client_payload(self, component: Any) -> dict[str, Any]:
        client = getattr(component, "client", None)
        if client is None or not hasattr(client, "telemetry_payload"):
            return {}
        return client.telemetry_payload()

    def _character_audit_payload(self, app: SceneOrchestrator, name: str) -> dict[str, Any]:
        character = app.character_manager.get(name)
        conflict_state = app.conflict_manager.state
        role = "pc" if "pc" in character.traits else "npc"
        if name in conflict_state.enemy_ranks or name in conflict_state.villains or "villain" in character.traits:
            role = "enemy"
        return {
            "name": character.name,
            "role": role,
            "level": character.level,
            "hp": character.hp,
            "max_hp": character.max_hp,
            "crisis_threshold": character.crisis_threshold or character.max_hp // 2,
            "in_crisis": character.in_crisis,
            "mp": character.mp,
            "max_mp": character.max_mp,
            "inventory_points": character.inventory_points,
            "max_inventory_points": character.max_inventory_points,
            "fabula_points": character.fabula_points,
            "zenit": character.zenit,
            "attributes": dict(character.attributes),
            "statuses": [status.value for status in character.statuses],
            "traits": list(character.traits),
            "identity": character.identity,
            "theme": character.theme,
            "origin": character.origin,
            "classes": dict(character.classes),
            "skills": dict(character.skills),
            "hero_skills": list(character.hero_skills),
            "spells": list(character.spells),
            "defenses": {
                "physical": app.character_manager.effective_defense(name, "physical"),
                "magic": app.character_manager.effective_defense(name, "magic"),
            },
            "base_defenses": dict(character.defenses),
            "affinities": dict(character.affinities),
            "temporary_affinities": dict(character.temporary_affinities),
            "equipment_affinities": dict(character.equipment_affinities),
            "equipment": {
                "armor": character.equipped_armor,
                "shield": character.equipped_shield,
                "main_hand": character.equipped_main_hand,
                "off_hand": character.equipped_off_hand,
                "accessory": character.equipped_accessory,
                "inventory": list(character.equipment),
                "templates": dict(character.equipment_templates),
                "notes": list(character.equipment_notes),
            },
            "guarding": character.guarding,
            "guarded_target": character.guarded_target,
            "active_arcanum": character.active_arcanum,
            "bound_arcana": list(character.bound_arcana),
        }

    def _read_saved_memory_events(self, campaign_id: str, *, limit: int, include_private: bool) -> list[dict[str, Any]]:
        path = self.data_root / self._safe_name(campaign_id) / "events.jsonl"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not include_private and data.get("visibility") != "public":
                continue
            rows.append(data)
        return rows

    def _map_artifacts(self, events: list[Any], *, limit: int, include_private: bool) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for event in reversed(events):
            visibility = getattr(event, "visibility", None)
            if not include_private and getattr(visibility, "value", visibility) != "public":
                continue
            payload = getattr(event, "payload", {}) or {}
            tags = set(getattr(event, "tags", []) or [])
            kind = getattr(event, "kind", "")
            if kind != "world_map_visual" and not {"map", "visual"}.issubset(tags):
                continue
            output_path = str(payload.get("output_path") or "")
            remote_url = str(payload.get("remote_url") or "")
            if not output_path and not remote_url:
                continue
            image_url = remote_url or self._artifact_url(output_path)
            artifacts.append(
                {
                    "event_id": getattr(event, "event_id", ""),
                    "created_at": getattr(event, "created_at", ""),
                    "summary": getattr(event, "summary", ""),
                    "model": payload.get("model", ""),
                    "renderer": payload.get("renderer", payload.get("model", "")),
                    "output_path": output_path,
                    "remote_url": remote_url,
                    "brief_path": payload.get("brief_path", ""),
                    "settings_path": payload.get("settings_path", ""),
                    "image_url": image_url,
                }
            )
            if len(artifacts) >= limit:
                break
        return list(reversed(artifacts))

    def _artifact_url(self, path: str) -> str:
        return f"/v1/artifacts/file?path={quote(path)}" if path else ""

    def _artifact_file(self, raw_path: str) -> tuple[int, dict[str, Any] | _FilePayload]:
        if not raw_path:
            return 400, {"ok": False, "error": "缺少 path 参数。"}
        try:
            path = Path(raw_path).expanduser().resolve()
        except OSError:
            return 400, {"ok": False, "error": "path 参数无效。"}
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return 403, {"ok": False, "error": "只允许读取图片产物。"}
        if not path.exists() or not path.is_file():
            return 404, {"ok": False, "error": "图片文件不存在。"}
        if not self._is_allowed_artifact_path(path):
            return 403, {"ok": False, "error": "该路径不在 FU-GM 产物目录内。"}
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return 200, _FilePayload(path.read_bytes(), content_type)

    def _is_allowed_artifact_path(self, path: Path) -> bool:
        project_dir = Path(os.environ.get("FU_GM_PROJECT_DIR", Path.cwd())).resolve()
        roots = [
            self.data_root.resolve(),
            (project_dir / ".runtime" / ".fu-gm").resolve(),
            (project_dir / "data").resolve(),
            Path(ImageGenerationConfig.from_env().output_dir).resolve(),
        ]
        return any(self._is_relative_to(path, root) for root in roots)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _audit_page(self) -> str:
        gm_name = html.escape(self.gm_name)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FU-GM 审计面板</title>
  <style>
    :root {{
      --bg: #f5efe5;
      --ink: #241b12;
      --muted: #7f6f5c;
      --card: rgba(255, 252, 245, 0.92);
      --line: #decdb5;
      --accent: #a8422d;
      --accent-2: #2d6f73;
      --shadow: 0 18px 50px rgba(83, 55, 25, 0.16);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: ui-serif, "Songti SC", "Noto Serif CJK SC", Georgia, serif;
      background:
        radial-gradient(circle at 15% 10%, rgba(239, 183, 116, 0.48), transparent 28rem),
        radial-gradient(circle at 85% 0%, rgba(45, 111, 115, 0.24), transparent 26rem),
        linear-gradient(135deg, #f5efe5, #eadcc6);
      min-height: 100vh;
    }}
    header {{
      padding: 28px clamp(18px, 4vw, 48px) 16px;
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-end;
      flex-wrap: wrap;
    }}
    h1 {{ margin: 0; font-size: clamp(30px, 5vw, 58px); letter-spacing: -0.05em; }}
    .subtitle {{ margin-top: 8px; color: var(--muted); font-size: 15px; }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      padding: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.46);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    .toolbar.secondary {{
      margin-top: 10px;
      align-items: center;
    }}
    input, select, button, textarea {{
      border: 1px solid var(--line);
      padding: 10px 14px;
      font: inherit;
      background: #fffaf1;
      color: var(--ink);
    }}
    input, select, button {{ border-radius: 999px; }}
    textarea {{
      width: 100%;
      min-height: 220px;
      border-radius: 14px;
      resize: vertical;
      line-height: 1.5;
    }}
    button {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 700;
    }}
    label {{ color: var(--muted); font-size: 14px; }}
    main {{
      padding: 0 clamp(18px, 4vw, 48px) 48px;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 18px;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 18px; }}
    .card {{
      grid-column: span 4;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--card);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .wide {{ grid-column: span 8; }}
    .full {{ grid-column: 1 / -1; }}
    h2 {{ margin: 0 0 12px; font-size: 19px; }}
    .pill {{
      display: inline-flex;
      gap: 6px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      margin: 3px 4px 3px 0;
      background: rgba(255,255,255,0.62);
      color: var(--muted);
      font-size: 13px;
    }}
    .meter {{
      height: 10px;
      background: #ead9bf;
      border-radius: 999px;
      overflow: hidden;
      margin: 8px 0 12px;
    }}
    .meter > span {{ display: block; height: 100%; background: linear-gradient(90deg, var(--accent), #d88b45); }}
    .list {{ display: grid; gap: 10px; }}
    .row {{
      padding: 12px;
      border: 1px solid rgba(222,205,181,0.8);
      border-radius: 16px;
      background: rgba(255,255,255,0.45);
    }}
    .muted {{ color: var(--muted); }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      font-size: 12px;
      max-height: 360px;
      overflow: auto;
    }}
    .danger {{ color: var(--accent); font-weight: 700; }}
    .ok {{ color: var(--accent-2); font-weight: 700; }}
    .checklist {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
    .check {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 11px;
      background: rgba(255,255,255,0.56);
      font-size: 13px;
    }}
    .check.ready {{ border-color: rgba(45,111,115,0.45); color: var(--accent-2); }}
    .check.todo {{ color: var(--muted); }}
    .columns {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .import-controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin: 10px 0;
    }}
    .import-controls input {{ min-width: 180px; }}
    .map-gallery {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }}
    .map-image {{
      width: 100%;
      max-height: 520px;
      object-fit: contain;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #efe4cf;
    }}
    @media (max-width: 980px) {{ .columns {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 980px) {{ .card, .wide {{ grid-column: 1 / -1; }} }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>FU-GM 审计面板</h1>
      <div class="subtitle">{gm_name} 的后台状态、跑团日志与 GM 暗线审计。默认隐藏私密内容。</div>
      <div class="toolbar secondary">
        <input id="newCampaignName" placeholder="新战役名" />
        <button id="newCampaign">新建战役</button>
        <select id="slotSelect" title="命名存档槽"></select>
        <input id="slotName" placeholder="新存档槽名，如 boss战前" />
        <button id="saveLatest">保存最新</button>
        <button id="saveNamed">新建命名存档</button>
        <button id="loadSlot">读取选中存档</button>
      </div>
    </div>
    <div class="toolbar">
      <select id="campaignSelect" title="已保存或已载入的战役"></select>
      <input id="campaign" placeholder="campaign_id" value="" />
      <input id="session" placeholder="session_id" value="default" />
      <input id="channel" placeholder="channel_id 可选" value="" />
      <label><input id="private" type="checkbox" /> 显示私密 GM 内容</label>
      <label><input id="autoRefresh" type="checkbox" checked /> 自动刷新</label>
      <button id="refresh">刷新</button>
      <span id="refreshState" class="muted"></span>
    </div>
  </header>
  <main>
    <section class="grid">
      <div class="card" id="status"></div>
      <div class="card" id="gate"></div>
      <div class="card" id="llm"></div>
      <div class="card full" id="runtimeTelemetry"></div>
      <div class="card full" id="importer">
        <h2>迁移导入</h2>
        <textarea id="importChatLog" placeholder="粘贴旧群聊记录。先点预览，确认提取出的世界共识、界限与帷幕、世界设定、人物草稿等内容，再导入到当前 campaign。"></textarea>
        <div class="import-controls">
          <input id="importBaseSlot" placeholder="基于存档槽，可空" />
          <input id="importTargetSlot" placeholder="导入后保存槽，可空" />
          <label><input id="importRawLog" type="checkbox" /> 保存原始聊天记录到导入审计文件</label>
          <button id="previewImport">预览导入</button>
          <button id="applyImport">导入并保存</button>
          <span id="importState" class="muted"></span>
        </div>
        <div id="importPreview" class="mono"></div>
      </div>
      <div class="card full" id="setup"></div>
      <div class="card full" id="mapArtifacts"></div>
      <div class="card full" id="guidance"></div>
      <div class="card full" id="playProcess"></div>
      <div class="card full" id="storyArc"></div>
      <div class="card wide" id="clocks"></div>
      <div class="card" id="saves"></div>
      <div class="card full" id="characters"></div>
      <div class="card wide" id="logs"></div>
      <div class="card" id="memory"></div>
      <div class="card full" id="raw"></div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const params = new URLSearchParams(window.location.search);
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
    const pill = (v) => `<span class="pill">${{esc(v)}}</span>`;
    let autoRefreshTimer = null;
    let campaignsCache = [];

    function displayText(value) {{
      return String(value ?? "").replace(/\\s+/g, " ").trim();
    }}
    function compactKey(value) {{
      return displayText(value)
        .replace(/[【】「」『』《》〈〉“”‘’"'`]/g, "")
        .replace(/[：:，,。.!！?？；;、\\s]/g, "")
        .toLowerCase();
    }}
    function dedupeItems(items) {{
      const result = [];
      const keys = [];
      for (const value of items || []) {{
        const text = displayText(value);
        if (!text) continue;
        const key = compactKey(text);
        if (!key) continue;
        const duplicateIndex = keys.findIndex(existing => existing === key || existing.includes(key) || key.includes(existing));
        if (duplicateIndex >= 0) {{
          if (key.length > keys[duplicateIndex].length || text.length > result[duplicateIndex].length) {{
            result[duplicateIndex] = text;
            keys[duplicateIndex] = key;
          }}
        }} else {{
          result.push(text);
          keys.push(key);
        }}
      }}
      return result;
    }}
    function row(title, body = "") {{
      return `<div class="row"><strong>${{esc(title)}}</strong>${{body ? `<div class="muted">${{body}}</div>` : ""}}</div>`;
    }}
    function renderList(items, empty = "无") {{
      const clean = dedupeItems(items);
      return clean.length ? clean.map(item => `<div>· ${{esc(item)}}</div>`).join("") : esc(empty);
    }}
    function renderDict(obj, empty = "无") {{
      const entries = Object.entries(obj || {{}}).filter(([, value]) => String(value ?? "").trim());
      return entries.length ? entries.map(([key, value]) => `<div>· <strong>${{esc(key)}}</strong>：${{esc(value)}}</div>`).join("") : esc(empty);
    }}
    function renderDictPills(obj, suffix = "") {{
      return Object.entries(obj || {{}})
        .filter(([key, value]) => String(key ?? "").trim() && String(value ?? "").trim())
        .map(([key, value]) => pill(`${{key}}${{suffix}}${{value}}`))
        .join("");
    }}
    function renderHeroDrafts(drafts) {{
      const entries = Object.entries(drafts || {{}}).filter(([, draft]) => draft && typeof draft === "object");
      if (!entries.length) return "";
      return entries.map(([key, draft]) => {{
        const title = displayText(draft.hero_name || key || "未命名角色");
        const basics = [
          draft.player_name ? `玩家：${{draft.player_name}}` : "",
          draft.identity ? `身份：${{draft.identity}}` : "",
          draft.theme ? `主题：${{draft.theme}}` : "",
          draft.origin ? `故乡：${{draft.origin}}` : ""
        ].filter(Boolean).join(" · ");
        const notes = dedupeItems([...(draft.notes || []), ...(draft.concept_notes || [])]).slice(0, 8);
        const questions = dedupeItems([...(draft.open_questions || []), ...(draft.missing_fields || [])]).slice(0, 8);
        const bonds = dedupeItems(draft.bonds || []).slice(0, 6);
        return `<div class="row">
          <strong>${{esc(title)}} ${{pill("角色草稿")}} ${{draft.confirmed ? pill("已确认") : pill("未定稿")}}</strong>
          ${{basics ? `<div class="muted">${{esc(basics)}}</div>` : ""}}
          ${{Object.keys(draft.classes || {{}}).length ? `<div>${{renderDictPills(draft.classes, " Lv.")}}</div>` : ""}}
          ${{Object.keys(draft.attributes || {{}}).length ? `<div>${{renderDictPills(draft.attributes, " d")}}</div>` : ""}}
          ${{Object.keys(draft.skills || {{}}).length ? `<div class="muted">技能：${{esc(Object.entries(draft.skills || {{}}).map(([k, v]) => `${{k}}${{v ? " Lv." + v : ""}}`).join("、"))}}</div>` : ""}}
          ${{(draft.spells || []).length ? `<div class="muted">法术：${{esc(dedupeItems(draft.spells).join("、"))}}</div>` : ""}}
          ${{(draft.equipment || []).length ? `<div class="muted">装备草稿：${{esc(dedupeItems(draft.equipment).join("、"))}}</div>` : ""}}
          ${{bonds.length ? `<div class="muted">羁绊草稿</div>${{renderList(bonds)}}` : ""}}
          ${{notes.length ? `<div class="muted">角色笔记</div>${{renderList(notes)}}` : ""}}
          ${{questions.length ? `<div class="muted">待确认</div>${{renderList(questions)}}` : ""}}
        </div>`;
      }}).join("");
    }}
    function renderMapArtifacts(items) {{
      const maps = items || [];
      if (!maps.length) {{
        return row("暂无地图图片", "第零章完成并生成世界地图后会显示在这里。");
      }}
      return `<div class="map-gallery">${{maps.map(item => `
        <div class="row">
          <strong>${{esc(item.summary || "世界地图")}}</strong>
          ${{item.image_url ? `<img class="map-image" src="${{esc(item.image_url)}}" alt="${{esc(item.summary || "世界地图")}}" loading="lazy" />` : ""}}
          <div class="muted">生成：${{esc(item.created_at || "未知")}} · 渲染器：${{esc(item.renderer || item.model || "未知")}}</div>
          ${{item.output_path ? `<div class="muted">图片：${{esc(item.output_path)}}</div>` : ""}}
          ${{item.brief_path ? `<div class="muted">Brief：${{esc(item.brief_path)}}</div>` : ""}}
        </div>`).join("")}}</div>`;
    }}
    function chooseInitialCampaign(campaigns) {{
      const requested = params.get("campaign_id") || "";
      if (requested) return requested;
      const current = window.__currentCampaignId || "";
      if (current) return current;
      const loaded = campaigns.find(c => c.loaded_in_memory);
      if (loaded) return loaded.campaign_id;
      const saved = [...campaigns].filter(c => c.updated_at || c.has_latest_snapshot || (c.slots || []).length);
      saved.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
      return (saved[0] || campaigns[0] || {{ campaign_id: "default" }}).campaign_id || "default";
    }}
    async function loadCampaigns() {{
      const response = await fetch("/v1/campaigns");
      const data = await response.json();
      const campaigns = data.campaigns || [];
      campaignsCache = campaigns;
      window.__currentCampaignId = data.current_campaign_id || "";
      const select = $("campaignSelect");
      select.innerHTML = "";
      if (!campaigns.length) {{
        select.add(new Option("default（暂无本地存档）", "default"));
      }} else {{
        for (const item of campaigns) {{
          const flags = [
            item.loaded_in_memory ? "内存中" : "",
            item.has_latest_snapshot ? "最新快照" : "",
            (item.slots || []).length ? `${{item.slots.length}}槽` : ""
          ].filter(Boolean).join(" / ") || "未保存";
          select.add(new Option(`${{item.campaign_id}}（${{flags}}）`, item.campaign_id));
        }}
      }}
      const chosen = chooseInitialCampaign(campaigns);
      $("campaign").value = chosen;
      select.value = [...select.options].some(option => option.value === chosen) ? chosen : "default";
      populateSlotSelect(chosen);
    }}
    function populateSlotSelect(campaign) {{
      const slotSelect = $("slotSelect");
      const item = campaignsCache.find(c => c.campaign_id === campaign) || {{}};
      const slots = item.slot_details || (item.slots || []).map(slot => ({{ slot }}));
      slotSelect.innerHTML = "";
      slotSelect.add(new Option("最新快照", ""));
      for (const slot of slots) {{
        const label = `${{slot.slot}}${{slot.saved_at ? "（" + slot.saved_at + "）" : ""}}`;
        slotSelect.add(new Option(label, slot.slot || ""));
      }}
    }}
    function syncCampaignFromSelect() {{
      if ($("campaignSelect").value) $("campaign").value = $("campaignSelect").value;
      populateSlotSelect($("campaign").value || "default");
    }}
    function updateUrl(campaign, session, channel) {{
      const url = new URL(window.location.href);
      url.searchParams.set("campaign_id", campaign);
      url.searchParams.set("session_id", session);
      if (channel) url.searchParams.set("channel_id", channel);
      else url.searchParams.delete("channel_id");
      window.history.replaceState(null, "", url);
    }}
    function render(data) {{
      if (!data || data.ok === false) {{
        const message = data?.error || data?.reply || "仪表盘接口返回异常。";
        $("refreshState").textContent = `刷新失败：${{message}}`;
        $("status").innerHTML = `<h2>当前状态</h2><div class="row danger">${{esc(message)}}</div>`;
        $("raw").innerHTML = `<h2>原始 JSON</h2><div class="mono">${{esc(JSON.stringify(data || {{}}, null, 2))}}</div>`;
        return;
      }}
      const phase = data.phase || {{}};
      const runtime = data.runtime || {{}};
      const loadedSections = runtime.loaded_sections || {{}};
      $("refreshState").textContent = `已刷新 ${{new Date().toLocaleTimeString()}}`;
      $("session").value = data.session_id || $("session").value || "default";
      $("channel").value = data.channel_id || $("channel").value || "";
      $("status").innerHTML = `<h2>当前状态</h2>
        ${{row("战役", data.campaign_id)}}
        ${{row("场次", data.session_id)}}
        ${{row("频道", data.channel_id || "未指定")}}
        ${{row("审计范围", `${{data.scope?.resolved_from || "request"}}；请求：${{data.scope?.requested?.session_id || ""}} / ${{data.scope?.requested?.channel_id || ""}}`)}}
        ${{row("阶段", phase.display || "未开始")}}
        ${{row("当前行动者", phase.current_actor || "无")}}
        ${{row("最近保存", runtime.last_saved_path || "尚无")}}
        ${{row("读档字段", [
          `world_state(${{(loadedSections.world_state_keys || []).length}})`,
          `characters:${{loadedSections.characters || 0}}`,
          `clocks:${{loadedSections.clocks || 0}}`,
          `rituals:${{loadedSections.rituals || 0}}`,
          `projects:${{loadedSections.projects || 0}}`
        ].join(" / "))}}`;
      $("gate").innerHTML = `<h2>会话门控</h2>
        ${{row("状态", data.gate.status)}}
        ${{row("理由", data.gate.reason || "无")}}
        ${{row("在场", (data.attendance.active_players || []).map(pill).join("") || "无")}}
        ${{row("离席", Object.entries(data.attendance.absent_players || {{}}).map(([k,v]) => pill(`${{k}}：${{v || "临时离席"}}`)).join("") || "无")}}`;
      $("llm").innerHTML = `<h2>模型与路由</h2>
        ${{row("LLM", data.llm.use_llm ? "启用" : "离线兜底")}}
        ${{row("Action Brain", data.llm.action_brain)}}
        ${{row("Expressor", data.llm.expressor)}}
        ${{row("Fallback", data.llm.action_last_used_fallback ? "最近使用过" : "无记录")}}
        ${{data.llm.action_last_error ? row("最近错误", `<span class="danger">${{esc(data.llm.action_last_error)}}</span>`) : ""}}`;
      const service = runtime.service || {{}};
      const bridge = runtime.astrbot_bridge || {{}};
      const http = runtime.http || {{}};
      const pipeline = runtime.pipeline || {{}};
      const slowHttp = http.slowest_recent || [];
      const slowTurns = pipeline.slowest_turns || [];
      const clientRows = [
        ["Action API", data.llm.action_client],
        ["Expressor API", data.llm.expressor_client],
        ["Casual API", data.llm.casual_client],
        ["Summarizer API", data.llm.summarizer_client]
      ].filter(([, value]) => value && Object.keys(value).length);
      $("runtimeTelemetry").innerHTML = `<h2>运行监控</h2>
        <div class="columns">
          <div>
            ${{row("服务", `启动 ${{esc(service.started_at || "未知")}} · 运行 ${{esc(service.uptime_seconds || 0)}} 秒`)}}
            ${{row("AstrBot 桥接", `${{esc(bridge.status_label || "未知")}} · 消息数 ${{esc(bridge.total_messages || 0)}}`)}}
            ${{row("最近桥接", [bridge.last_campaign_id, bridge.last_session_id, bridge.last_channel_id, bridge.last_speaker].filter(Boolean).map(esc).join(" / ") || "无")}}
            ${{row("当前战役", esc(service.current_campaign_id || data.campaign_id || "default"))}}
          </div>
          <div>
            ${{row("最近最慢 HTTP", slowHttp.length ? slowHttp.slice(0, 5).map(s => esc(`${{s.method}} ${{s.route}} · ${{s.elapsed_ms}}ms · ${{s.status}}`)).join("<br>") : "暂无")}}
            ${{row("最近最慢回合", slowTurns.length ? slowTurns.slice(0, 5).map(t => esc(`${{t.action_type || "turn"}} · total ${{t.total_ms || 0}}ms · brain ${{t.action_brain_ms || 0}} / rules ${{t.rules_ms || 0}} / express ${{t.expressor_ms || 0}}`)).join("<br>") : "暂无")}}
          </div>
        </div>
        ${{clientRows.length ? `<div class="columns">${{clientRows.map(([name, value]) => `<div class="row"><strong>${{esc(name)}}</strong><div class="muted">调用 ${{esc(value.total_calls || 0)}} 次 · 最近均值 ${{esc(value.average_recent_elapsed_ms || 0)}}ms</div>${{(value.slowest_recent || []).slice(0, 3).map(c => `<div class="muted">${{esc(c.model || "")}} · ${{esc(c.elapsed_ms || 0)}}ms · chars ${{esc(c.prompt_chars || 0)}} ${{c.ok ? "" : "· error"}}</div>`).join("")}}</div>`).join("")}}</div>` : row("模型调用", "暂无真实 API 调用记录")}}`;
      const setup = data.setup || {{}};
      const consensus = setup.recorded_consensus || {{}};
      const worldRecords = setup.world_records || {{}};
      const facts = setup.recent_accepted_facts || [];
      const safetyItems = dedupeItems([
        consensus.violence_guideline,
        consensus.romance_guideline,
        ...(consensus.evil_guidelines || []),
        ...(consensus.consensus_notes || []),
        ...(consensus.safety_lines || []).map(v => "界限：" + v),
        ...(consensus.safety_veils || []).map(v => "帷幕：" + v)
      ]);
      $("setup").innerHTML = `<h2>开团前 / 第零章记录</h2>
        <div class="checklist">
          ${{(setup.checklist || []).map(item => `<span class="check ${{item.ready ? "ready" : "todo"}}">${{item.ready ? "已记：" : "待补："}}${{esc(item.name)}}${{item.value ? " · " + esc(item.value) : ""}}</span>`).join("")}}
        </div>
        <div class="columns">
          <div>
            ${{row("基调偏好", renderList(consensus.tone_preferences))}}
            ${{row("想探索的主题", renderList(consensus.playstyle_themes))}}
            ${{row("队伍关系共识", esc(consensus.party_dynamic || "无"))}}
            ${{row("描述风格", esc(consensus.description_style || "无"))}}
            ${{row("安全与尺度", safetyItems.map(esc).join("<br>") || "无")}}
          </div>
          <div>
            ${{row("世界与小队", [
              worldRecords.campaign_title ? "标题：" + worldRecords.campaign_title : "",
              worldRecords.world_style ? "风貌：" + worldRecords.world_style : "",
              worldRecords.map_card ? "地图：" + worldRecords.map_card : "",
              worldRecords.magic_tech_role ? "魔法与科技：" + worldRecords.magic_tech_role : "",
              worldRecords.group_concept ? "小队：" + worldRecords.group_concept : "",
              worldRecords.starting_region ? "起点：" + worldRecords.starting_region : "",
              worldRecords.selected_first_act_summary ? "第一幕：" + worldRecords.selected_first_act_summary : ""
            ].filter(Boolean).map(esc).join("<br>") || "无")}}
            ${{row("国家", renderDict(worldRecords.kingdoms))}}
            ${{row("历史事件", renderList(worldRecords.historical_events))}}
            ${{row("地点", renderDict(worldRecords.major_locations))}}
            ${{row("势力", renderDict(worldRecords.factions))}}
            ${{row("威胁与反派种子", renderList([...(worldRecords.world_threats || []), ...(worldRecords.villain_seeds || []), ...(worldRecords.villain_mirrors || [])]))}}
            ${{row("谜团", renderList(worldRecords.mysteries))}}
          </div>
        </div>
        ${{row("最近确认事实", facts.length ? facts.slice(-12).map(f => `· ${{esc(f.speaker)}}：${{esc(f.fact)}}`).join("<br>") : "暂无")}}
        ${{row("待补问题", renderList(setup.open_questions))}}`;
      $("mapArtifacts").innerHTML = `<h2>地图图片</h2>${{renderMapArtifacts(data.world?.map_artifacts || [])}}`;
      const gmGuidance = data.gm_guidance || {{}};
      const preparedLocations = gmGuidance.prepared_locations || [];
      $("guidance").innerHTML = `<h2>GM 创作指导</h2>
        <div class="columns">
          <div>
            ${{row("后台使用原则", esc(gmGuidance.usage_note || "这些内容只给 GM 作为创作辅助。"))}}
            ${{row("灵感标签", (gmGuidance.inspiration_tags || []).map(pill).join("") || "暂无")}}
            ${{row("创作原则", renderList(gmGuidance.principles))}}
            ${{row("追问角度", renderList(gmGuidance.question_angles))}}
            ${{row("故事节奏", renderList(gmGuidance.story_beats))}}
            ${{row("角色创建追问", renderList(gmGuidance.hero_creation_prompts))}}
          </div>
          <div>
            <div class="row">
              <strong>预备地点候选</strong>
              <div class="muted">后台地点库。只有玩家追踪、物语点引入或剧情自然需要时，才登记为公开地点。</div>
            </div>
            ${{preparedLocations.length ? preparedLocations.map(loc => `
              <div class="row">
                <strong>${{esc(loc.name || "未命名地点")}} ${{pill(loc.status_label || loc.status || "后台候选")}}</strong>
                <div class="muted">${{esc(loc.archetype || "")}}</div>
                <div>${{esc(loc.brief || "")}}</div>
                <div class="muted">调用时机：${{esc(loc.use_when || "")}}</div>
                ${{(loc.questions || []).length ? `<div class="muted">可追问：${{esc((loc.questions || []).join(" / "))}}</div>` : ""}}
                ${{(loc.hooks || []).length ? `<div>${{(loc.hooks || []).map(pill).join("")}}</div>` : ""}}
              </div>`).join("") : row("暂无预备地点", "当前世界信息太少，先继续共创。")}}
          </div>
        </div>
        ${{(gmGuidance.stored_inspiration_tags || []).length ? row("已保存标签", (gmGuidance.stored_inspiration_tags || []).map(pill).join("")) : ""}}`;
      const playProcess = data.play_process || {{}};
      $("playProcess").innerHTML = `<h2>游玩流程</h2>
        <div class="columns">
          <div>
            ${{row("当前镜头", esc(playProcess.current_focus || "尚未建立明确场景。"))}}
            ${{row("场景流程", renderList(playProcess.scene_flow))}}
            ${{row("收束条件", renderList(playProcess.scene_end_triggers))}}
            ${{row("当前场景类型提示", renderList(playProcess.scene_type_guidance))}}
          </div>
          <div>
            ${{row("场次节奏", renderList(playProcess.session_guidance))}}
            ${{row("战役节奏", renderList(playProcess.campaign_guidance))}}
            ${{row("主持原则", renderList(playProcess.principles))}}
          </div>
        </div>`;
      const storyArc = data.story_arc || {{}};
      const agenda = storyArc.agenda || {{}};
      const activeThreads = (storyArc.threads || []).filter(t => !["resolved", "retired"].includes(t.status)).slice(0, 6);
      const pressureTracks = (storyArc.villain_pressure || []).slice(0, 5);
      const revealRows = (storyArc.reveals || []).slice(0, 5);
      const locationRows = (storyArc.locations || []).slice(0, 5);
      $("storyArc").innerHTML = `<h2>长期故事节奏</h2>
        <div class="columns">
          <div>
            ${{row("战役阶段", `${{esc(storyArc.phase || "opening")}} · 已整理 ${{esc(storyArc.session_count || 0)}} 场`)}}
            ${{row("下一场开场画面", esc(agenda.opening_image || "暂无"))}}
            ${{row("推荐焦点", renderList(agenda.recommended_focus))}}
            ${{row("可问玩家", renderList(agenda.questions))}}
            ${{row("建议场景类型", esc(agenda.suggested_scene_type || "standard"))}}
          </div>
          <div>
            ${{row("后台说明", esc(storyArc.usage_note || "长期故事节奏只供 GM 后台使用。"))}}
            ${{row("反派压力动作", renderList(agenda.pressure_moves))}}
            ${{row("警告", renderList(agenda.warnings))}}
          </div>
        </div>
        <div class="columns">
          <div>
            <div class="row"><strong>活跃故事线</strong></div>
            ${{activeThreads.length ? activeThreads.map(t => `<div class="row">
              <strong>${{esc(t.title || "未命名故事线")}} ${{pill(t.thread_type || "plot")}} ${{pill(t.status || "seeded")}}</strong>
              <div class="muted">进展 ${{esc(t.progress || 0)}} · 优先级 ${{esc(t.priority || 1)}} · 来源 ${{esc(t.source || "")}}</div>
              <div>${{esc(t.summary || "")}}</div>
              ${{(t.public_clues || []).length ? `<div class="muted">公开线索：${{esc((t.public_clues || []).slice(-3).join(" / "))}}</div>` : ""}}
            </div>`).join("") : row("暂无活跃故事线", "继续第零章共创或结束第一场后会自动生成。")}}
          </div>
          <div>
            <div class="row"><strong>反派压力</strong></div>
            ${{pressureTracks.length ? pressureTracks.map(p => `<div class="row">
              <strong>${{esc(p.villain || "威胁")}} ${{pill(p.stage || "seeded")}}</strong>
              <div class="meter"><span style="width:${{Math.round(((p.current || 0) / Math.max(1, p.segments || 1)) * 100)}}%"></span></div>
              <div class="muted">${{esc(p.current || 0)}}/${{esc(p.segments || 0)}} · ${{esc(p.goal || "")}}</div>
              <div>${{esc(p.visible_consequence || "")}}</div>
              ${{p.last_action ? `<div class="muted">最近动作：${{esc(p.last_action)}}</div>` : ""}}
            </div>`).join("") : row("暂无反派压力", "第零章记录反派种子或世界威胁后会出现。")}}
          </div>
        </div>
        <div class="columns">
          <div>
            <div class="row"><strong>揭示候选</strong><div class="muted">未勾选私密内容时不显示秘密正文。</div></div>
            ${{revealRows.length ? revealRows.map(r => `<div class="row">
              <strong>${{esc(r.title || "未命名真相")}} ${{pill(r.status || "seeded")}}</strong>
              <div class="muted">线索 ${{esc((r.public_clues || []).length)}}/${{esc(r.required_clues || 0)}} · 适合阶段：${{esc(r.best_phase || "midpoint")}}</div>
              ${{r.secret ? `<div>${{esc(r.secret)}}</div>` : ""}}
              ${{(r.public_clues || []).length ? `<div class="muted">线索：${{esc((r.public_clues || []).join(" / "))}}</div>` : ""}}
            </div>`).join("") : row("暂无揭示候选", "谜团和 GM 私密暗线会在这里形成揭示候选。")}}
          </div>
          <div>
            <div class="row"><strong>地点回访</strong></div>
            ${{locationRows.length ? locationRows.map(loc => `<div class="row">
              <strong>${{esc(loc.location || "未命名地点")}} ${{pill(loc.status || "stable")}}</strong>
              <div class="muted">上次出现：${{esc(loc.last_seen || "尚未登场")}} · 来源 ${{esc(loc.source || "")}}</div>
              <div>${{esc(loc.next_prompt || "")}}</div>
              ${{(loc.changes || []).length ? `<div class="muted">变化：${{esc((loc.changes || []).slice(-3).join(" / "))}}</div>` : ""}}
            </div>`).join("") : row("暂无地点", "共创地点或旅行发现会进入这里。")}}
          </div>
        </div>`;
      $("clocks").innerHTML = `<h2>命刻</h2>` + (data.clocks.length ? data.clocks.map(c => `
        <div class="row">
          <strong>${{esc(c.name)}} ${{c.current}}/${{c.max_segments}}</strong>
          <div class="meter"><span style="width:${{Math.round((c.current / Math.max(1, c.max_segments)) * 100)}}%"></span></div>
          <div>${{pill(c.clock_type)}} ${{c.auto_advance ? pill("自动：" + c.auto_advance) : ""}}</div>
          <div class="muted">${{esc(c.stakes || c.gm_note || "")}}</div>
        </div>`).join("") : row("暂无命刻", "命刻应由 GM 在需要节奏压力或复杂目标时建立。"));
      $("saves").innerHTML = `<h2>存档</h2>` + (data.logs.save_slots.length ? data.logs.save_slots.map(s => row(s.slot || "latest", s.path || s.saved_at || "")).join("") : row("暂无存档"));
      const characterCards = (data.characters || []).map(ch => `
        <div class="row">
          <strong>${{esc(ch.name)}} ${{pill(ch.role)}} ${{ch.in_crisis ? '<span class="danger">危机</span>' : '<span class="ok">稳定</span>'}}</strong>
          <div class="muted">HP ${{ch.hp}}/${{ch.max_hp}} · MP ${{ch.mp}}/${{ch.max_mp}} · DEF ${{ch.defenses.physical}} / MDEF ${{ch.defenses.magic}}</div>
          <div>${{Object.entries(ch.classes || {{}}).map(([k,v]) => pill(`${{k}} Lv.${{v}}`)).join("")}}</div>
          <div class="muted">${{esc([ch.identity, ch.theme, ch.origin].filter(Boolean).join(" / "))}}</div>
          <div class="muted">装备：${{esc([ch.equipment.main_hand, ch.equipment.off_hand, ch.equipment.armor, ch.equipment.shield, ch.equipment.accessory].filter(Boolean).join("、") || "无")}}</div>
        </div>`).join("");
      const heroDraftCards = renderHeroDrafts(setup.hero_drafts);
      $("characters").innerHTML = `<h2>角色与实体</h2><div class="list">` + (characterCards || heroDraftCards ? characterCards + heroDraftCards : row("暂无角色", "还没有正式角色或角色草稿。")) + `</div>`;
      $("logs").innerHTML = `<h2>最近对话</h2><div class="list">` + (data.logs.recent_transcript.length ? data.logs.recent_transcript.map(e => row(`${{e.speaker}} · ${{e.role}}`, esc(e.content))).join("") : row("暂无 transcript")) + `</div>`;
      $("memory").innerHTML = `<h2>记忆</h2>
        ${{row("公开短记忆", data.world.recent_public_memories.map(esc).join("<br>") || "无")}}
        ${{row("记忆事件", data.world.recent_memory_events.map(e => esc(`${{e.kind}}：${{e.summary}}`)).join("<br>") || "无")}}
        ${{data.private_included ? row("GM 暗线", data.world.gm_secrets.map(s => esc(`${{s.title}}：${{s.content || "(内容隐藏)"}}`)).join("<br>") || "无") : row("GM 暗线", "已隐藏。勾选私密内容后才显示。")}}`;
      $("raw").innerHTML = `<h2>原始 JSON</h2><div class="mono">${{esc(JSON.stringify(data, null, 2))}}</div>`;
    }}
    async function postJson(path, payload) {{
      const response = await fetch(path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || data.reply || "请求失败");
      $("refreshState").textContent = data.reply || "操作完成";
      await loadCampaigns();
      await refresh();
      return data;
    }}
    async function importChatLog(dryRun) {{
      const chatLog = $("importChatLog").value.trim();
      if (!chatLog) {{
        $("importState").textContent = "请先粘贴聊天记录。";
        return;
      }}
      $("importState").textContent = dryRun ? "正在整理预览..." : "正在导入并保存...";
      const response = await fetch("/v1/campaigns/import-chat-log", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          campaign_id: $("campaign").value || "default",
          session_id: $("session").value || "default",
          channel_id: $("channel").value || "",
          speaker: "仪表盘",
          chat_log: chatLog,
          base_slot: $("importBaseSlot").value || "",
          target_slot: $("importTargetSlot").value || "",
          store_raw_log: $("importRawLog").checked,
          dry_run: dryRun
        }})
      }});
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || data.reply || "导入失败");
      $("importPreview").textContent = JSON.stringify(data.preview || data, null, 2);
      $("importState").textContent = data.reply || "导入完成";
      if (!dryRun) {{
        await loadCampaigns();
        try {{
          await refresh();
        }} catch (err) {{
          $("importState").textContent = `${{data.reply || "导入已保存"}}；但刷新仪表盘失败：${{err.message}}`;
        }}
      }}
      return data;
    }}
    async function refresh() {{
      const campaign = $("campaign").value || "default";
      const session = $("session").value || "default";
      const channel = $("channel").value || "";
      const includePrivate = $("private").checked ? "true" : "false";
      updateUrl(campaign, session, channel);
      const response = await fetch(`/v1/audit/dashboard?campaign_id=${{encodeURIComponent(campaign)}}&session_id=${{encodeURIComponent(session)}}&channel_id=${{encodeURIComponent(channel)}}&include_private=${{includePrivate}}&limit=60`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        render(data);
        throw new Error(data.error || data.reply || `HTTP ${{response.status}}`);
      }}
      render(data);
    }}
    function resetAutoRefresh() {{
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
      if ($("autoRefresh").checked) {{
        autoRefreshTimer = setInterval(() => refresh().catch(err => $("refreshState").textContent = `刷新失败：${{err.message}}`), 5000);
      }}
    }}
    $("session").value = params.get("session_id") || "default";
    $("channel").value = params.get("channel_id") || "";
    $("private").checked = ["1", "true", "yes", "on"].includes((params.get("include_private") || "").toLowerCase());
    $("campaignSelect").addEventListener("change", () => {{ syncCampaignFromSelect(); refresh(); }});
    $("campaign").addEventListener("change", () => {{ populateSlotSelect($("campaign").value || "default"); refresh(); }});
    $("session").addEventListener("change", refresh);
    $("channel").addEventListener("change", refresh);
    $("private").addEventListener("change", refresh);
    $("autoRefresh").addEventListener("change", resetAutoRefresh);
    $("refresh").addEventListener("click", refresh);
    $("newCampaign").addEventListener("click", () => {{
      const campaign = $("newCampaignName").value.trim() || $("campaign").value.trim();
      if (!campaign) return;
      $("campaign").value = campaign;
      postJson("/v1/campaigns/new", {{ campaign_id: campaign }}).catch(err => $("refreshState").textContent = `新建失败：${{err.message}}`);
    }});
    $("saveLatest").addEventListener("click", () => {{
      postJson("/v1/campaigns/save", {{
        campaign_id: $("campaign").value || "default",
        session_id: $("session").value || "default",
        speaker: "仪表盘"
      }}).catch(err => $("refreshState").textContent = `保存失败：${{err.message}}`);
    }});
    $("saveNamed").addEventListener("click", () => {{
      const slot = $("slotName").value.trim();
      if (!slot) {{
        $("refreshState").textContent = "请先填写新存档槽名。";
        return;
      }}
      postJson("/v1/campaigns/save", {{
        campaign_id: $("campaign").value || "default",
        session_id: $("session").value || "default",
        speaker: "仪表盘",
        slot
      }}).catch(err => $("refreshState").textContent = `保存失败：${{err.message}}`);
    }});
    $("loadSlot").addEventListener("click", () => {{
      postJson("/v1/campaigns/load", {{
        campaign_id: $("campaign").value || "default",
        slot: $("slotSelect").value || ""
      }}).catch(err => $("refreshState").textContent = `读档失败：${{err.message}}`);
    }});
    $("previewImport").addEventListener("click", () => {{
      importChatLog(true).catch(err => $("importState").textContent = `预览失败：${{err.message}}`);
    }});
    $("applyImport").addEventListener("click", () => {{
      importChatLog(false).catch(err => $("importState").textContent = `导入失败：${{err.message}}`);
    }});
    loadCampaigns()
      .then(refresh)
      .then(resetAutoRefresh)
      .catch(err => $("raw").innerHTML = `<h2>载入失败</h2><div class="danger">${{esc(err.message)}}</div>`);
  </script>
</body>
</html>"""

    def _chat_log_importer(self) -> CampaignChatLogImporter:
        if not self.use_llm:
            return CampaignChatLogImporter(gm_name=self.gm_name)
        llm_config = LLMConfig.from_env()
        if not llm_config.api_key:
            return CampaignChatLogImporter(gm_name=self.gm_name)
        model = (
            str(getattr(llm_config, "action_model", "") or "").strip()
            or str(getattr(llm_config, "expressor_model", "") or "").strip()
        )
        return CampaignChatLogImporter(
            client=OpenAICompatibleClient(llm_config),
            model=model,
            gm_name=self.gm_name,
        )

    def _import_existing_context(self, runtime: CampaignRuntime) -> dict[str, Any]:
        app = runtime.app
        world = app.world_state.world_profile
        return {
            "campaign_id": runtime.campaign_id,
            "campaign_title": world.campaign_title,
            "world_style": world.world_style,
            "group_concept": world.group_concept,
            "starting_region": world.starting_region,
            "safety_lines": list(world.safety_lines),
            "safety_veils": list(world.safety_veils),
            "hero_drafts": {
                key: {
                    "player_name": draft.player_name,
                    "hero_name": draft.hero_name,
                    "identity": draft.identity,
                    "theme": draft.theme,
                    "origin": draft.origin,
                    "classes": dict(draft.classes),
                    "attributes": dict(draft.attributes),
                    "skills": dict(draft.skills),
                    "spells": list(draft.spells),
                    "bound_arcana": list(draft.bound_arcana),
                    "equipment": list(draft.equipment),
                    "confirmed": draft.confirmed,
                }
                for key, draft in world.hero_drafts.items()
            },
            "characters": [character.name for character in app.character_manager.all()],
            "loaded_sections": self._snapshot_loaded_sections(
                app.memory_store.build_snapshot(
                    runtime.campaign_id,
                    world_state=app.world_state,
                    character_manager=app.character_manager,
                    clock_manager=app.clock_manager,
                    conflict_manager=app.conflict_manager,
                    scene_manager=app.scene_manager,
                    ritual_manager=app.ritual_manager,
                    project_manager=app.project_manager,
                    story_arc_manager=app.story_arc_manager,
                )
            ),
        }

    def _write_import_artifact(
        self,
        campaign_id: str,
        *,
        session_id: str,
        channel_id: str,
        speaker: str,
        import_payload: dict[str, Any],
        preview: dict[str, Any],
        source: str,
        fallback_used: bool,
        warnings: list[str],
        chat_log: str,
    ) -> str:
        campaign_dir = self._memory_store()._campaign_dir(campaign_id)
        import_dir = campaign_dir / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        path = import_dir / f"chat_log_import_{int(time.time())}.json"
        artifact = {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "speaker": speaker,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source,
            "fallback_used": fallback_used,
            "warnings": list(warnings),
            "preview": preview,
            "import_payload": import_payload,
        }
        if chat_log:
            artifact["chat_log"] = chat_log
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def _snapshot_loaded_sections(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        world_state = snapshot.get("world_state") if isinstance(snapshot.get("world_state"), dict) else {}
        return {
            "schema_version": snapshot.get("schema_version"),
            "world_state_keys": [
                key
                for key in (
                    "session_pillars",
                    "map_notes",
                    "map_locations",
                    "npc_relationships",
                    "memories",
                    "npc_personas",
                    "subject_facts",
                    "persistent_changes",
                    "memory_events",
                    "memory_relations",
                    "gm_secrets",
                    "world_profile",
                    "party_sheet",
                    "world_sheet",
                    "present_players",
                    "absent_players",
                )
                if key in world_state
            ],
            "characters": len(snapshot.get("characters", []) or []),
            "clocks": len(snapshot.get("clocks", []) or []),
            "conflict_state": bool(snapshot.get("conflict_state")),
            "scene_manager": bool(snapshot.get("scene_manager")),
            "rituals": len(snapshot.get("rituals", {}).get("active_rituals", []) if isinstance(snapshot.get("rituals"), dict) else []),
            "projects": len(snapshot.get("projects", {}).get("projects", []) if isinstance(snapshot.get("projects"), dict) else []),
            "story_arc": bool(snapshot.get("story_arc")),
        }

    def _truthy(self, value: Any) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _int_value(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    def _safe_name(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip()) or "default"

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        if isinstance(value, set):
            return [self._json_safe(item) for item in sorted(value, key=str)]
        if hasattr(value, "value"):
            return value.value
        return value

    def _runtime(self, campaign_id: str, *, auto_load: bool = True) -> CampaignRuntime:
        if campaign_id in self.runtimes:
            return self.runtimes[campaign_id]
        app = build_app(
            use_llm=self.use_llm,
            gm_style_prompt=self.gm_style_prompt,
            deepseek_roleplay_mode=self.deepseek_roleplay_mode,
        )
        app.memory_store = self._memory_store()
        app.topic_memory_store = TopicMemoryStore(self.data_root)
        app.set_campaign_id(campaign_id)
        llm_config = LLMConfig.from_env()
        summarizer = HeuristicStorySummarizer()
        casual_client = None
        casual_model = ""
        if self.use_llm and llm_config.api_key:
            client = OpenAICompatibleClient(llm_config)
            summarizer = LLMStorySummarizer(client=client, model=llm_config.action_model, fallback=summarizer)
            casual_client = client
            casual_model = llm_config.expressor_model or llm_config.action_model
        log_manager = SessionLogManager(self.data_root, summarizer=summarizer)
        casual_chat = CasualChatResponder(
            log_manager=log_manager,
            client=casual_client,
            model=casual_model,
            gm_name=self.gm_name,
            style_prompt=self.gm_style_prompt,
            topic_memory_store=app.topic_memory_store,
        )
        loaded_from_disk = False
        last_saved_path = ""
        if auto_load and app.memory_store.snapshot_exists(campaign_id):
            app.load_campaign_memory(campaign_id)
            loaded_from_disk = True
            last_saved_path = str(app.memory_store._snapshot_path(campaign_id))
        runtime = CampaignRuntime(
            campaign_id=campaign_id,
            app=app,
            log_manager=log_manager,
            casual_chat=casual_chat,
            loaded_from_disk=loaded_from_disk,
            last_saved_path=last_saved_path,
        )
        self.runtimes[campaign_id] = runtime
        return runtime

    def _memory_store(self) -> CampaignMemoryStore:
        return CampaignMemoryStore(self.data_root)

    def _mark_current_campaign(self, campaign_id: str) -> None:
        campaign_id = str(campaign_id or "").strip()
        if campaign_id:
            self.current_campaign_id = campaign_id

    def _current_campaign_id(self) -> str:
        if self.current_campaign_id:
            return self.current_campaign_id
        active = [
            state
            for state in self._session_gate_states()
            if state.status in {"pre_session", "session_zero", "adventure"}
        ]
        if active:
            active.sort(key=lambda state: state.updated_at or state.started_at, reverse=True)
            return active[0].campaign_id
        paused = [state for state in self._session_gate_states() if state.status == "paused"]
        if paused:
            paused.sort(key=lambda state: state.updated_at or state.started_at, reverse=True)
            return paused[0].campaign_id
        if self.runtimes:
            return next(reversed(self.runtimes))
        campaigns = self._memory_store().list_campaigns()
        if campaigns:
            campaigns.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return str(campaigns[0].get("campaign_id") or "default")
        return "default"

    def _session_gate_states(self) -> list[SessionGateState]:
        try:
            raw = self.session_gates._load()
        except Exception:
            return []
        states: list[SessionGateState] = []
        for item in raw.values():
            if isinstance(item, dict):
                try:
                    states.append(SessionGateState(**item))
                except TypeError:
                    continue
        return states

    def _touch_speaker(self, runtime: CampaignRuntime, speaker: str) -> None:
        speaker = speaker.strip()
        if not speaker or speaker == "AI GM":
            return
        if speaker not in runtime.app.world_state.present_players:
            runtime.app.world_state.present_players.append(speaker)

    def _message_fields(self, payload: dict[str, Any]) -> tuple[str, str, str, str, str]:
        campaign_id = str(payload.get("campaign_id") or "default")
        session_id = str(payload.get("session_id") or "default")
        speaker = str(payload.get("speaker") or payload.get("user_name") or "玩家")
        message = str(payload.get("message") or "")
        channel_id = str(payload.get("channel_id") or "")
        return campaign_id, session_id, speaker, message, channel_id

    def _resolve_mode(self, message: str, mode: str) -> str:
        if mode in {"casual", "game", "pre_session", "session_zero", "safety"}:
            return mode
        lowered = message.lower()
        if extract_safety_declarations(message):
            return "safety"
        if any(token in message for token in ("开团前共识", "基调", "桌面共识", "安全准则")):
            return "pre_session"
        if any(token in message for token in ("第零章", "Session 0", "世界创建", "创建角色", "界限", "帷幕")):
            return "session_zero"
        if any(token in message for token in ("攻击", "施法", "防御", "调查", "推进命刻", "检定", "进入战斗", "跑团行动")):
            return "game"
        if lowered.startswith(("/game", "/turn", "行动:")):
            return "game"
        return "casual"

    def _mode_after_session_gate(
        self,
        *,
        campaign_id: str,
        channel_id: str,
        session_id: str,
        resolved_mode: str,
    ) -> str:
        if resolved_mode == "safety" or not channel_id:
            return resolved_mode
        gate = self.session_gates.get(campaign_id, channel_id, session_id)
        if gate.status == "session_zero" and resolved_mode in {"casual", "game"}:
            return "session_zero"
        if gate.status == "pre_session" and resolved_mode in {"casual", "game"}:
            return "pre_session"
        return resolved_mode


class _RequestHandler(BaseHTTPRequestHandler):
    service: FUGMHttpService

    def do_GET(self) -> None:
        self._respond(*self.service.handle("GET", self.path))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self._respond(400, {"ok": False, "error": "请求体不是合法 JSON。"})
            return
        self._respond(*self.service.handle("POST", self.path, payload))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, payload: dict[str, Any] | str | _FilePayload) -> None:
        if isinstance(payload, _FilePayload):
            body = payload.body
            content_type = payload.content_type
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = (
                "text/html; charset=utf-8"
                if payload.lstrip().lower().startswith("<!doctype") or "<html" in payload[:300].lower()
                else "text/plain; charset=utf-8"
            )
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    service: FUGMHttpService | None = None,
) -> ThreadingHTTPServer:
    service = service or FUGMHttpService()

    class Handler(_RequestHandler):
        pass

    Handler.service = service
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 FU-GM 轻量 HTTP 服务。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", default="data/campaigns")
    parser.add_argument("--offline", action="store_true", help="禁用真实 LLM，使用本地兜底。")
    args = parser.parse_args()
    service = FUGMHttpService(data_root=args.data_root, use_llm=not args.offline)
    server = make_server(args.host, args.port, service=service)
    print(f"FU-GM HTTP 服务已启动：http://{args.host}:{args.port}")
    print("健康检查：GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFU-GM HTTP 服务已停止。")


if __name__ == "__main__":
    main()
