from __future__ import annotations

import json
import asyncio
import re
import time
from pathlib import Path
from sys import maxsize
from typing import Any
from urllib import request
from urllib.error import HTTPError

from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api import message_components as Comp
except Exception:  # 旧版 AstrBot 或插件独立测试时回退为纯文本。
    Comp = None

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # 兼容插件单独测试或旧版 AstrBot。
    get_astrbot_data_path = None

try:
    from .campaign_binding import (
        apply_confirmed_campaign_binding,
        bind_known_channel_members,
        is_fugm_command_message,
    )
    from .message_buffer import DebouncedMessageBuffer
    from .delivery import ReplyDeliveryCoordinator, reply_delivery_specs
    from .heartbeat import (
        HeartbeatDeliveryJournal,
        HeartbeatTaskRegistry,
        heartbeat_committed_state_change,
    )
    from .request_coordinator import ChannelRequestCoordinator
    from .state_storage import write_json_atomic, write_json_map_atomic
except ImportError:  # AstrBot 有时会把插件目录直接加入 sys.path。
    from campaign_binding import (
        apply_confirmed_campaign_binding,
        bind_known_channel_members,
        is_fugm_command_message,
    )
    from message_buffer import DebouncedMessageBuffer
    from delivery import ReplyDeliveryCoordinator, reply_delivery_specs
    from heartbeat import (
        HeartbeatDeliveryJournal,
        HeartbeatTaskRegistry,
        heartbeat_committed_state_change,
    )
    from request_coordinator import ChannelRequestCoordinator
    from state_storage import write_json_atomic, write_json_map_atomic


@register("fu_gm_bridge", "cunfu", "把 AstrBot 群聊消息桥接到 FU-GM HTTP 服务。", "0.2.5")
class FuGmBridgePlugin(Star):
    """AstrBot 薄插件。

    FU-GM 继续作为独立服务运行；本插件只负责把群消息转发过去，并把结果发回群里。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        config = config or {}
        self.server_url = str(config.get("server_url") or "http://127.0.0.1:8765").rstrip("/")
        self.campaign_id = str(config.get("campaign_id") or "default")
        self.default_session_id = str(config.get("default_session_id") or "main")
        self.enable_private_safety_auto = self._config_bool(config.get("enable_private_safety_auto", True))
        self.anonymous_private_safety = self._config_bool(config.get("anonymous_private_safety", True))
        self.enable_natural_routing = self._config_bool(config.get("enable_natural_routing", True))
        self.natural_route_group_messages = self._config_bool(config.get("natural_route_group_messages", True))
        self.natural_route_private_messages = self._config_bool(config.get("natural_route_private_messages", True))
        self.block_silent_table_talk = self._config_bool(config.get("block_silent_table_talk", True))
        self.enable_exact_reply_quotes = self._config_bool(config.get("enable_exact_reply_quotes", True))
        self.http_timeout_seconds = self._config_float(config.get("http_timeout_seconds", 120), default=120.0)
        self.log_http_timing = self._config_bool(config.get("log_http_timing", True))
        self.enable_message_buffer = self._config_bool(config.get("enable_message_buffer", True))
        self.enable_idle_monitor = self._config_bool(config.get("enable_idle_monitor", True))
        self.idle_monitor_auto_reply = self._config_bool(config.get("idle_monitor_auto_reply", True))
        self.idle_monitor_interval_seconds = self._config_float(
            config.get("idle_monitor_interval_seconds", 60),
            default=60.0,
        )
        self.idle_monitor_cooldown_seconds = self._config_int(
            config.get("idle_monitor_cooldown_seconds", 180),
            default=180,
        )
        self.idle_monitor_thresholds = {
            "pre_session_idle_seconds": self._config_int(
                config.get("pre_session_idle_seconds", 600),
                default=600,
            ),
            "session_zero_idle_seconds": self._config_int(
                config.get("session_zero_idle_seconds", 600),
                default=600,
            ),
            "adventure_idle_seconds": self._config_int(
                config.get("adventure_idle_seconds", 240),
                default=240,
            ),
            "pc_turn_idle_seconds": self._config_int(
                config.get("pc_turn_idle_seconds", 300),
                default=300,
            ),
            "npc_turn_grace_seconds": self._config_int(
                config.get("npc_turn_grace_seconds", 45),
                default=45,
            ),
            "setup_nudge_followup_seconds": self._config_int(
                config.get("setup_nudge_followup_seconds", 1200),
                default=1200,
            ),
            "setup_nudge_limit": self._config_int(
                config.get("setup_nudge_limit", 1),
                default=1,
            ),
        }
        self._idle_monitor_task: asyncio.Task | None = None
        self._channel_activity_versions: dict[str, int] = {}
        self._channel_sessions: dict[str, str] = {}
        self._heartbeat_tasks = HeartbeatTaskRegistry()
        self.message_buffer = DebouncedMessageBuffer(
            debounce_seconds=self._config_float(config.get("buffer_debounce_seconds", 3.0), default=3.0),
            max_wait_seconds=self._config_float(config.get("buffer_max_wait_seconds", 12.0), default=12.0),
            max_messages=self._config_int(config.get("buffer_max_messages", 5), default=5),
        )
        self._request_coordinator = ChannelRequestCoordinator()
        self._reply_confirmation_recovery_lock = asyncio.Lock()
        self.plugin_data_dir = self._plugin_data_dir()
        self._heartbeat_delivery_journal = HeartbeatDeliveryJournal(
            self.plugin_data_dir / "heartbeat_sent_unconfirmed.json"
        )
        self._reply_delivery_journal = HeartbeatDeliveryJournal(
            self.plugin_data_dir / "reply_sent_unconfirmed.json"
        )
        self._reply_delivery_coordinator = ReplyDeliveryCoordinator(
            self._reply_delivery_journal
        )
        self.state_path = self._state_path_from_config(config, "campaign_bindings_path", "channel_campaigns.json")
        self.user_state_path = self._state_path_from_config(
            config,
            "user_campaign_bindings_path",
            "user_campaigns.json",
        )
        self.channel_members_path = self._state_path_from_config(
            config,
            "channel_members_path",
            "channel_members.json",
        )
        self.channel_campaigns = self._load_json_map(self.state_path)
        self.user_campaigns = self._load_json_map(self.user_state_path)
        self.channel_members = self._load_json_list_map(
            self.channel_members_path
        )

    @filter.command("fugm")
    async def fugm_turn(self, event: AstrMessageEvent) -> MessageEventResult:
        """跑团回合：/fugm 我攻击宝箱王"""
        message = self._command_tail(event, "fugm")
        payload = self._command_payload(event, message=message, mode="game")
        response = await self._post_stateful("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_beat")
    async def fugm_gm_beat(self, event: AstrMessageEvent) -> MessageEventResult:
        """让时悠主动推进当前场景：/fugm_beat 或 /fugm_beat 让 NPC 回答。"""
        message = self._command_tail(event, "fugm_beat")
        payload = self._command_payload(event, message=message, mode="game")
        response = await self._post_stateful("/v1/game/gm-beat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_chat")
    async def fugm_chat(self, event: AstrMessageEvent) -> MessageEventResult:
        """水群聊天：/fugm_chat 还记得上次宝箱王那段吗"""
        message = self._command_tail(event, "fugm_chat")
        payload = self._command_payload(event, message=message, mode="casual")
        response = await self._post_stateful("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_s0")
    async def fugm_session_zero(self, event: AstrMessageEvent) -> MessageEventResult:
        """Session 0：/fugm_s0 我想要地下城宝箱和奇遇"""
        message = self._command_tail(event, "fugm_s0")
        payload = self._command_payload(
            event,
            message=message,
            mode="session_zero",
        )
        response = await self._post_stateful("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_safety")
    async def fugm_safety(self, event: AstrMessageEvent) -> MessageEventResult:
        """设置界限与帷幕：私聊使用时默认匿名。"""
        message = self._command_tail(event, "fugm_safety")
        if not message:
            yield event.plain_result("可以直接说：/fugm_safety 我不希望出现 X，或 /fugm_safety X 请淡出处理。")
            return
        payload = self._command_payload(event, message=message, mode="safety")
        response = await self._post_stateful("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_end")
    async def fugm_end_session(self, event: AstrMessageEvent) -> MessageEventResult:
        """结束并整理本场：/fugm_end 星尘迷宫第一夜"""
        title = self._command_tail(event, "fugm_end")
        payload = self._command_payload(event, message="", mode="end")
        payload["title"] = title
        response = await self._post_stateful("/v1/session/end", payload)
        if not bool(response.get("ok", True)):
            yield event.plain_result(
                str(response.get("error") or self._reply_text(response) or "本场暂时无法收团。")
            )
            return
        if response.get("already_ended"):
            yield event.plain_result("这场已经收过团了，存档没有重复结算。")
            return
        summary = response.get("summary", {})
        reply = summary.get("short_memory") or summary.get("public_summary") or self._reply_text(response)
        yield event.plain_result(f"本场已经整理好啦：{reply}")

    @filter.command("fugm_campaign")
    async def fugm_campaign(self, event: AstrMessageEvent) -> MessageEventResult:
        """查看或切换当前群绑定的团：/fugm_campaign 星尘宝箱谭"""
        self._mark_channel_activity(event)
        campaign_id = self._command_tail(event, "fugm_campaign")
        channel_id = self._channel_id(event)
        if not campaign_id:
            yield event.plain_result(f"当前群绑定的 FU-GM 团：{self._campaign_id(event)}")
            return
        self.channel_campaigns[channel_id] = campaign_id
        self._save_channel_campaigns()
        self._remember_user_campaign(event, campaign_id)
        self._bind_known_channel_members(channel_id, campaign_id)
        yield event.plain_result(f"已将当前群绑定到《{campaign_id}》。如果本地有同名快照，FU-GM 会在使用时自动读档。")

    @filter.command("fugm_campaigns")
    async def fugm_campaigns(self, event: AstrMessageEvent) -> MessageEventResult:
        """列出 FU-GM 服务已知团。"""
        self._mark_channel_activity(event)
        response = await self._get("/v1/campaigns")
        if response.get("ok") is False:
            yield event.plain_result(self._reply_text(response))
            return
        campaigns = response.get("campaigns", [])
        if not campaigns:
            yield event.plain_result("FU-GM 目前还没有保存过任何团。")
            return
        lines = []
        for item in campaigns:
            slots = "、".join(item.get("slots") or []) or "无命名存档"
            loaded = "，已载入内存" if item.get("loaded_in_memory") else ""
            lines.append(f"- {item.get('campaign_id')}（存档槽：{slots}{loaded}）")
        yield event.plain_result("FU-GM 已知团：\n" + "\n".join(lines))

    @filter.command("fugm_save")
    async def fugm_save(self, event: AstrMessageEvent) -> MessageEventResult:
        """保存当前团：/fugm_save 或 /fugm_save boss战前"""
        slot = self._command_tail(event, "fugm_save")
        payload = self._command_payload(event, message="", mode="save")
        if slot:
            payload["slot"] = slot
        response = await self._post_stateful("/v1/campaigns/save", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_load")
    async def fugm_load(self, event: AstrMessageEvent) -> MessageEventResult:
        """读档：/fugm_load，/fugm_load 团名，或 /fugm_load 团名 存档槽"""
        args = self._command_tail(event, "fugm_load").split()
        campaign_id = self._campaign_id(event)
        slot = ""
        if len(args) == 1:
            campaign_id = args[0]
        elif len(args) >= 2:
            campaign_id = self._campaign_id(event) if args[0] == "." else args[0]
            slot = " ".join(args[1:])
        payload = self._command_payload(event, message="", mode="load")
        payload["campaign_id"] = campaign_id
        if slot:
            payload["slot"] = slot
        response = await self._post_stateful("/v1/campaigns/load", payload)
        if response.get("ok"):
            channel_id = self._channel_id(event)
            self.channel_campaigns[channel_id] = campaign_id
            self._save_channel_campaigns()
            self._remember_user_campaign(event, campaign_id)
            self._bind_known_channel_members(channel_id, campaign_id)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_delete_save")
    async def fugm_delete_save(self, event: AstrMessageEvent) -> MessageEventResult:
        """删除当前团的最新快照或命名存档槽：/fugm_delete_save boss战前"""
        slot = self._command_tail(event, "fugm_delete_save")
        payload = self._command_payload(
            event,
            message="",
            mode="delete_save",
        )
        if slot:
            payload["slot"] = slot
        response = await self._post_stateful("/v1/campaigns/delete", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_delete_campaign")
    async def fugm_delete_campaign(self, event: AstrMessageEvent) -> MessageEventResult:
        """删除当前群绑定的整个 FU-GM 战役目录：/fugm_delete_campaign 确认删除"""
        confirm = self._command_tail(event, "fugm_delete_campaign")
        payload = self._command_payload(
            event,
            message="",
            mode="delete_campaign",
        )
        payload["delete_all"] = True
        payload["confirm"] = confirm
        response = await self._post_stateful("/v1/campaigns/delete", payload)
        if response.get("ok"):
            channel_id = self._channel_id(event)
            if channel_id in self.channel_campaigns:
                del self.channel_campaigns[channel_id]
                self._save_channel_campaigns()
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_away")
    async def fugm_away(self, event: AstrMessageEvent) -> MessageEventResult:
        """标记自己临时离席并自动保存：/fugm_away 去吃饭"""
        reason = self._command_tail(event, "fugm_away")
        payload = self._command_payload(event, message=reason, mode="away")
        payload["reason"] = reason
        response = await self._post_stateful("/v1/session/away", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_back")
    async def fugm_back(self, event: AstrMessageEvent) -> MessageEventResult:
        """标记自己回到本场。"""
        payload = self._command_payload(event, message="", mode="back")
        response = await self._post_stateful("/v1/session/back", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_status")
    async def fugm_status(self, event: AstrMessageEvent) -> MessageEventResult:
        """查看当前团、场景与离席状态。"""
        payload = self._command_payload(event, message="", mode="status")
        response = await self._post("/v1/session/status", payload)
        yield event.plain_result(self._format_status_response(response))

    @filter.command("fugm_health")
    async def fugm_health(self, event: AstrMessageEvent) -> MessageEventResult:
        """检查 FU-GM 服务。"""
        self._mark_channel_activity(event)
        response = await self._get("/health")
        yield event.plain_result("FU-GM 服务状态：" + json.dumps(response, ensure_ascii=False))

    @filter.command("fugm_heartbeat")
    async def fugm_heartbeat(self, event: AstrMessageEvent) -> MessageEventResult:
        """手动触发当前团心跳检查。"""
        message = self._command_tail(event, "fugm_heartbeat")
        payload = self._command_payload(
            event,
            message=message,
            mode="heartbeat",
        )
        payload.update(self.idle_monitor_thresholds)
        payload["auto_respond"] = True
        payload["force"] = "force" in message.lower() or "强制" in message
        payload["cooldown_seconds"] = self.idle_monitor_cooldown_seconds
        response = await self._post("/v1/session/heartbeat", payload)
        if response.get("send_reply") and response.get("reply"):
            yield event.plain_result(self._reply_text(response))
            return
        reason = response.get("reason") or "当前不需要主动推进。"
        yield event.plain_result(f"心跳检查：暂时不需要时悠主动推进。原因：{reason}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize - 20)
    async def passive_prefix_router(self, event: AstrMessageEvent) -> MessageEventResult:
        """把普通消息原样交给 FU-GM 的单一语义智能体。"""
        self._ensure_idle_monitor_started()
        self._mark_channel_activity(event)
        await self._recover_unconfirmed_reply_deliveries()
        raw = event.message_str.strip()
        if not self._natural_routing_enabled_for(event, raw):
            return
        payload = self._payload(event, message=raw, mode="auto")
        payload["is_private"] = self._is_private_event(event)
        if await self._should_buffer_natural_message(event, raw):
            if self.block_silent_table_talk:
                event.stop_event()
            batch = await self.message_buffer.add(self._buffer_key(event), payload)
            if batch is None:
                return
            response = await self._route_buffered_payload(batch.key, batch.payload)
        else:
            response = await self._post_stateful("/v1/message/route", payload)
        self._apply_active_campaign_from_response(event, response)
        if response.get("ok") is False:
            return
        if response.get("send_reply") and (
            response.get("reply")
            or response.get("reply_envelopes")
            or response.get("reply_media")
        ):
            delivered = await self._deliver_reply_results(event, response)
            if delivered:
                event.stop_event()
            return
        if response.get("stop_astrbot") and self.block_silent_table_talk:
            event.stop_event()
            return

    def _command_payload(
        self,
        event: AstrMessageEvent,
        *,
        message: str,
        mode: str,
    ) -> dict:
        self._ensure_idle_monitor_started()
        self._mark_channel_activity(event)
        return self._payload(event, message=message, mode=mode)

    def _payload(self, event: AstrMessageEvent, *, message: str, mode: str) -> dict:
        campaign_id = self._campaign_id(event)
        if not self._is_private_event(event):
            self._remember_channel_member(event)
            self._ensure_channel_campaign_binding(event, campaign_id)
            self._remember_user_campaign(event, campaign_id)
        astrbot_context = self._astrbot_context(event)
        message_id = str(astrbot_context.get("message_id") or "")
        quoted_message = astrbot_context.get("quoted_message") if isinstance(astrbot_context.get("quoted_message"), dict) else {}
        return {
            "campaign_id": campaign_id,
            "session_id": self._session_id(event),
            "speaker": event.get_sender_name() or str(event.get_sender_id()),
            "speaker_id": self._user_key(event),
            "message": message,
            "message_id": message_id,
            "received_at": astrbot_context.get("timestamp"),
            "channel_id": self._channel_id(event),
            "activity_version": self._channel_activity_versions.get(self._channel_id(event), 0),
            "mode": mode,
            "anonymous": bool(
                self.enable_private_safety_auto
                and self.anonymous_private_safety
                and self._is_private_event(event)
            ),
            "is_at_bot": bool(astrbot_context.get("is_at_bot")),
            "is_reply_to_bot": bool(astrbot_context.get("is_reply_to_bot")),
            "quoted_message": quoted_message,
            "astrbot_context": astrbot_context,
        }

    def _command_tail(self, event: AstrMessageEvent, command: str) -> str:
        """Return command arguments whether AstrBot keeps or strips the leading slash.

        Some AstrBot command events expose ``message_str`` as ``fugm_load 1``
        instead of ``/fugm_load 1``.  Parsing both forms prevents the command
        name itself from being mistaken for a campaign id.
        """
        text = str(event.message_str or "").strip()
        for prefix in (f"/{command}", command):
            if text == prefix:
                return ""
            if text.startswith(prefix + " "):
                return text[len(prefix) :].strip()
        return text

    def _astrbot_context(self, event: AstrMessageEvent) -> dict[str, Any]:
        """Extract non-text context AstrBot already knows about this message.

        Different AstrBot adapters expose reply/at/image segments in slightly
        different shapes. Keep this adapter-side extraction defensive: FU-GM
        can use the normalized fields when present, while plain text routing
        continues to work if an adapter omits them.
        """

        context: dict[str, Any] = {
            "sender_id": self._user_key(event),
            "sender_name": self._safe_event_value(event, "get_sender_name"),
            "group_id": self._safe_event_value(event, "get_group_id"),
            "self_id": self._safe_event_value(event, "get_self_id"),
            "is_private": self._is_private_event(event),
            "mentions": [],
            "attachments": [],
            "segment_types": [],
        }
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            context["message_id"] = self._string_attr(message_obj, "message_id", "id")
            context["raw_message"] = self._truncate(self._string_attr(message_obj, "raw_message", "raw"), 1200)
            context["timestamp"] = self._string_attr(message_obj, "timestamp", "time")
            context["platform"] = self._string_attr(message_obj, "platform")
            if not context.get("self_id"):
                context["self_id"] = self._string_attr(message_obj, "self_id")
            if not context.get("group_id"):
                context["group_id"] = self._string_attr(message_obj, "group_id")
            raw = getattr(message_obj, "raw_message", None)
            self._merge_raw_message_context(context, raw)
            self._merge_message_chain_context(context, getattr(message_obj, "message", None))
        self._merge_raw_message_context(context, getattr(event, "raw_message", None))
        self._merge_cq_context(context, str(getattr(event, "message_str", "") or ""))
        self_id = str(context.get("self_id") or "")
        if self_id:
            context["is_at_bot"] = any(str(item.get("target") or "") == self_id for item in context["mentions"])
            quoted = context.get("quoted_message")
            if isinstance(quoted, dict):
                context["is_reply_to_bot"] = str(quoted.get("sender_id") or "") == self_id
        else:
            context["is_at_bot"] = bool(context.get("is_at_bot", False))
            context["is_reply_to_bot"] = bool(context.get("is_reply_to_bot", False))
        context["mentions"] = context["mentions"][:8]
        context["attachments"] = context["attachments"][:8]
        context["segment_types"] = sorted(set(str(item) for item in context["segment_types"] if item))
        return {key: value for key, value in context.items() if value not in ("", None, [], {})}

    def _safe_event_value(self, event: AstrMessageEvent, method_name: str) -> str:
        try:
            method = getattr(event, method_name, None)
            if method:
                value = method()
                return str(value) if value is not None else ""
        except Exception:
            return ""
        return ""

    def _string_attr(self, obj: object, *names: str) -> str:
        for name in names:
            try:
                value = getattr(obj, name, None)
            except Exception:
                continue
            if value is None:
                continue
            if isinstance(value, (str, int, float)):
                return str(value)
        return ""

    def _merge_message_chain_context(self, context: dict[str, Any], chain: object) -> None:
        if not chain:
            return
        try:
            segments = list(chain)
        except Exception:
            segments = [chain]
        for segment in segments:
            type_name = self._segment_type(segment)
            if type_name:
                context.setdefault("segment_types", []).append(type_name)
            lowered = type_name.lower()
            if "at" == lowered or lowered.endswith(".at") or lowered.endswith("at"):
                target = self._segment_value(segment, "qq", "id", "user_id", "target", "uin")
                if target:
                    context.setdefault("mentions", []).append({"target": str(target), "source": "message_chain"})
            elif any(token in lowered for token in ("reply", "quote", "source")):
                quoted = context.setdefault("quoted_message", {})
                message_id = self._segment_value(segment, "id", "message_id", "msg_id", "source_id")
                sender_id = self._segment_value(segment, "sender_id", "user_id", "qq")
                text = self._segment_value(segment, "text", "content", "message", "raw_message")
                if message_id:
                    quoted["message_id"] = str(message_id)
                if sender_id:
                    quoted["sender_id"] = str(sender_id)
                if text:
                    quoted["text"] = self._truncate(str(text), 800)
                quoted["source"] = "message_chain"
            elif any(token in lowered for token in ("image", "file", "record", "voice", "video")):
                context.setdefault("attachments", []).append(
                    {
                        "type": lowered,
                        "file": str(self._segment_value(segment, "file", "filename", "url", "path") or ""),
                    }
                )

    def _merge_raw_message_context(self, context: dict[str, Any], raw: object) -> None:
        if not raw:
            return
        if isinstance(raw, str):
            context.setdefault("raw_message", self._truncate(raw, 1200))
            self._merge_cq_context(context, raw)
            return
        if isinstance(raw, dict):
            for source_key, target_key in (
                ("message_id", "message_id"),
                ("self_id", "self_id"),
                ("group_id", "group_id"),
                ("user_id", "sender_id"),
                ("time", "timestamp"),
            ):
                if raw.get(source_key) and not context.get(target_key):
                    context[target_key] = str(raw.get(source_key))
            sender = raw.get("sender")
            if isinstance(sender, dict):
                if sender.get("user_id") and not context.get("sender_id"):
                    context["sender_id"] = str(sender.get("user_id"))
                if sender.get("nickname") and not context.get("sender_name"):
                    context["sender_name"] = str(sender.get("nickname"))
            self._merge_raw_segments(context, raw.get("message"))
            if raw.get("raw_message"):
                context.setdefault("raw_message", self._truncate(str(raw.get("raw_message")), 1200))
                self._merge_cq_context(context, str(raw.get("raw_message")))
            return
        self._merge_message_chain_context(context, raw)

    def _merge_raw_segments(self, context: dict[str, Any], segments: object) -> None:
        if not isinstance(segments, list):
            return
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if seg_type:
                context.setdefault("segment_types", []).append(seg_type)
            if seg_type == "at":
                target = data.get("qq") or data.get("id") or data.get("user_id")
                if target:
                    context.setdefault("mentions", []).append({"target": str(target), "source": "raw_message"})
            elif seg_type in {"reply", "quote"}:
                quoted = context.setdefault("quoted_message", {})
                if data.get("id"):
                    quoted["message_id"] = str(data.get("id"))
                if data.get("text") or data.get("content"):
                    quoted["text"] = self._truncate(str(data.get("text") or data.get("content")), 800)
                quoted["source"] = "raw_message"
            elif seg_type in {"image", "file", "record", "voice", "video"}:
                context.setdefault("attachments", []).append(
                    {"type": seg_type, "file": str(data.get("file") or data.get("url") or data.get("name") or "")}
                )

    def _merge_cq_context(self, context: dict[str, Any], text: str) -> None:
        if not text or "[CQ:" not in text:
            return
        for match in re.finditer(r"\[CQ:(?P<type>[a-zA-Z0-9_]+),(?P<data>[^\]]*)\]", text):
            seg_type = match.group("type")
            data = self._parse_cq_data(match.group("data"))
            context.setdefault("segment_types", []).append(seg_type)
            if seg_type == "at":
                target = data.get("qq") or data.get("id")
                if target:
                    context.setdefault("mentions", []).append({"target": str(target), "source": "cq"})
            elif seg_type in {"reply", "quote"}:
                quoted = context.setdefault("quoted_message", {})
                if data.get("id"):
                    quoted["message_id"] = str(data.get("id"))
                quoted["source"] = "cq"
            elif seg_type in {"image", "file", "record", "video"}:
                context.setdefault("attachments", []).append(
                    {"type": seg_type, "file": str(data.get("file") or data.get("url") or "")}
                )

    def _parse_cq_data(self, data: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for piece in str(data or "").split(","):
            if "=" not in piece:
                continue
            key, value = piece.split("=", 1)
            parsed[key.strip()] = value.strip()
        return parsed

    def _segment_type(self, segment: object) -> str:
        raw_type = self._segment_value(segment, "type")
        if raw_type:
            return str(raw_type)
        return segment.__class__.__name__

    def _segment_value(self, segment: object, *names: str) -> object:
        if isinstance(segment, dict):
            for name in names:
                if name in segment:
                    return segment.get(name)
            data = segment.get("data")
            if isinstance(data, dict):
                for name in names:
                    if name in data:
                        return data.get(name)
            return None
        for name in names:
            try:
                value = getattr(segment, name, None)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    def _truncate(self, value: str, limit: int) -> str:
        text = str(value or "")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _session_id(self, event: AstrMessageEvent) -> str:
        return self._channel_id(event) or self.default_session_id

    def _format_status_response(self, response: dict) -> str:
        if response.get("ok") is False:
            return self._reply_text(response)
        gate = response.get("gate", {})
        gate_status = gate.get("status") or "inactive"
        attendance = response.get("attendance", {})
        absent = attendance.get("absent_players", {})
        active = "、".join(attendance.get("active_players", [])) or "暂无"
        absent_text = "、".join(f"{name}({reason or '临时离席'})" for name, reason in absent.items()) or "暂无"
        return (
            f"当前团：《{response.get('campaign_id')}》\n"
            f"FU-GM 接管：{gate_status}\n"
            f"阶段：{response.get('game_phase') or '未知'}\n"
            f"当前行动者：{response.get('current_actor') or '无'}\n"
            f"在场：{active}\n"
            f"离席：{absent_text}"
        )

    def _channel_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or event.get_session_id() or "")
        except Exception:
            return self.default_session_id

    def _campaign_id(self, event: AstrMessageEvent) -> str:
        if self._is_private_event(event):
            user_key = self._user_key(event)
            if user_key and user_key in self.user_campaigns:
                return self.user_campaigns[user_key]
        return self.channel_campaigns.get(self._channel_id(event), self.campaign_id)

    def _ensure_channel_campaign_binding(self, event: AstrMessageEvent, campaign_id: str) -> None:
        channel_id = self._channel_id(event)
        if not channel_id or not campaign_id:
            return
        if channel_id in self.channel_campaigns:
            return
        self.channel_campaigns[channel_id] = campaign_id
        self._save_channel_campaigns()

    def _load_json_map(self, path: Path) -> dict[str, str]:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return {str(key): str(value) for key, value in data.items()}
        except Exception:
            return {}
        return {}

    def _load_json_list_map(self, path: Path) -> dict[str, list[str]]:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        str(key): sorted(
                            {
                                str(item)
                                for item in value
                                if str(item).strip()
                            }
                        )
                        for key, value in data.items()
                        if isinstance(value, list)
                    }
        except Exception:
            return {}
        return {}

    def _plugin_data_dir(self) -> Path:
        if get_astrbot_data_path is not None:
            try:
                return Path(get_astrbot_data_path()) / "plugin_data" / "fu_gm_bridge"
            except Exception:
                pass
        return Path("data") / "plugin_data" / "fu_gm_bridge"

    def _state_path_from_config(self, config: dict, key: str, filename: str) -> Path:
        raw = str(config.get(key) or "").strip()
        if not raw or self._is_legacy_state_default(raw):
            return self.plugin_data_dir / filename
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.plugin_data_dir / path
        return path

    def _is_legacy_state_default(self, path_text: str) -> bool:
        normalized = path_text.replace("\\", "/").rstrip("/")
        return normalized in {
            "~/.astrbot/data/plugin_data/fu_gm_bridge/channel_campaigns.json",
            "~/.astrbot/data/plugin_data/fu_gm_bridge/user_campaigns.json",
        }

    def _save_channel_campaigns(self) -> None:
        write_json_map_atomic(self.state_path, self.channel_campaigns)

    def _save_user_campaigns(self) -> None:
        write_json_map_atomic(self.user_state_path, self.user_campaigns)

    def _save_channel_members(self) -> None:
        write_json_atomic(
            self.channel_members_path,
            {
                str(channel_id): sorted(
                    {
                        str(user_key)
                        for user_key in members
                        if str(user_key).strip()
                    }
                )
                for channel_id, members in self.channel_members.items()
            },
        )

    def _remember_user_campaign(self, event: AstrMessageEvent, campaign_id: str) -> None:
        user_key = self._user_key(event)
        if not user_key or not campaign_id:
            return
        if self.user_campaigns.get(user_key) == campaign_id:
            return
        self.user_campaigns[user_key] = campaign_id
        self._save_user_campaigns()

    def _remember_channel_member(self, event: AstrMessageEvent) -> None:
        channel_id = self._channel_id(event)
        user_key = self._user_key(event)
        if not channel_id or not user_key:
            return
        members = self.channel_members.setdefault(channel_id, [])
        if user_key in members:
            return
        members.append(user_key)
        members.sort()
        self._save_channel_members()

    def _bind_known_channel_members(
        self,
        channel_id: str,
        campaign_id: str,
    ) -> None:
        if bind_known_channel_members(
            channel_id=channel_id,
            campaign_id=campaign_id,
            channel_members=self.channel_members,
            user_campaigns=self.user_campaigns,
        ):
            self._save_user_campaigns()

    def _apply_active_campaign_from_response(self, event: AstrMessageEvent, response: dict) -> None:
        """Apply a backend-confirmed campaign switch after an agent tool call.

        Natural-language save/load requests are resolved by FU-GM.  The bridge
        must never infer a target from message text, but it must preserve a
        successful backend switch for the next QQ message.
        """

        update = apply_confirmed_campaign_binding(
            response,
            is_private=self._is_private_event(event),
            channel_id=self._channel_id(event),
            user_key=self._user_key(event),
            confirmed_user_key=str(
                response.get("active_campaign_speaker_id") or ""
            ),
            channel_campaigns=self.channel_campaigns,
            user_campaigns=self.user_campaigns,
        )
        if update.channel_changed:
            self._save_channel_campaigns()
        known_members_changed = False
        if update.channel_changed:
            known_members_changed = bind_known_channel_members(
                channel_id=self._channel_id(event),
                campaign_id=update.campaign_id,
                channel_members=self.channel_members,
                user_campaigns=self.user_campaigns,
            )
        if update.user_changed or known_members_changed:
            self._save_user_campaigns()

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        try:
            return not bool(event.get_group_id())
        except Exception:
            return False

    def _user_key(self, event: AstrMessageEvent) -> str:
        for method_name in ("get_sender_id", "get_user_id"):
            try:
                method = getattr(event, method_name, None)
                if method:
                    value = method()
                    if value:
                        return str(value)
            except Exception:
                pass
        try:
            return str(event.get_sender_name() or "")
        except Exception:
            return ""

    def _natural_routing_enabled_for(self, event: AstrMessageEvent, message: str) -> bool:
        if not self.enable_natural_routing:
            return False
        if message.startswith("/") or is_fugm_command_message(message):
            return False
        if not message and not self._astrbot_context(event).get("is_at_bot"):
            return False
        if self._is_private_event(event):
            return self.natural_route_private_messages
        return self.natural_route_group_messages

    async def _should_buffer_natural_message(self, event: AstrMessageEvent, message: str) -> bool:
        if not self.enable_message_buffer:
            return False
        if self._is_private_event(event):
            return False
        context = self._astrbot_context(event)
        if context.get("is_at_bot") or context.get("is_reply_to_bot"):
            return False
        payload = self._payload(event, message="", mode="status")
        response = await self._post("/v1/session/gate", payload)
        gate = response.get("gate", {}) if response.get("ok") else {}
        return gate.get("status") in {"adventure", "session_zero"}

    def _buffer_key(self, event: AstrMessageEvent) -> str:
        return f"{self._campaign_id(event)}::{self._session_id(event)}::{self._channel_id(event)}"

    async def _route_buffered_payload(self, key: str, payload: dict) -> dict:
        return await self._post_stateful(
            "/v1/message/route",
            payload,
            serialization_key=key,
        )

    def _ensure_idle_monitor_started(self) -> None:
        if not self.enable_idle_monitor:
            return
        if self._idle_monitor_task is not None and not self._idle_monitor_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_monitor_task = loop.create_task(self._idle_monitor_loop())

    async def _idle_monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(max(10.0, self.idle_monitor_interval_seconds))
            if not self.idle_monitor_auto_reply or not self.channel_campaigns:
                continue
            if not self._has_channel_sender():
                continue
            for channel_id, campaign_id in list(self.channel_campaigns.items()):
                if not channel_id or not campaign_id:
                    continue
                payload = {
                    "campaign_id": campaign_id,
                    "session_id": channel_id,
                    "channel_id": channel_id,
                    "speaker": "系统心跳",
                    "message": "",
                    "mode": "heartbeat",
                    "activity_version": self._channel_activity_versions.get(channel_id, 0),
                    "auto_respond": True,
                    "defer_delivery_log": True,
                    "cooldown_seconds": self.idle_monitor_cooldown_seconds,
                    **self.idle_monitor_thresholds,
                }
                activity_version = self._channel_activity_versions.get(channel_id, 0)
                self._heartbeat_tasks.start(
                    channel_id,
                    lambda cid=channel_id, body=payload, version=activity_version: self._run_channel_heartbeat(
                        cid,
                        body,
                        version,
                    ),
                )

    async def _run_channel_heartbeat(self, channel_id: str, payload: dict, activity_version: int) -> None:
        try:
            response = await self._post("/v1/session/heartbeat", payload)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        activity_changed = (
            self._channel_activity_versions.get(channel_id, 0)
            != activity_version
        )
        if activity_changed and not heartbeat_committed_state_change(response):
            return
        if not response.get("send_reply") or not response.get("reply"):
            return
        delivery_id = str(response.get("delivery_id") or "").strip()
        if not (
            delivery_id
            and self._heartbeat_delivery_journal.was_sent(delivery_id)
        ):
            delivered = await self._send_channel_text(
                channel_id,
                self._reply_text(response),
            )
            if not delivered:
                return
            if delivery_id:
                self._heartbeat_delivery_journal.mark_sent(delivery_id)
        if delivery_id:
            confirmed = await self._confirm_heartbeat_delivery(
                {
                    "campaign_id": str(response.get("campaign_id") or ""),
                    "session_id": str(response.get("session_id") or ""),
                    "channel_id": str(response.get("channel_id") or channel_id),
                    "delivery_id": delivery_id,
                }
            )
            if confirmed:
                self._heartbeat_delivery_journal.mark_confirmed(delivery_id)

    async def _confirm_heartbeat_delivery(self, payload: dict) -> bool:
        for attempt in range(3):
            try:
                result = await self._post(
                    "/v1/session/heartbeat/delivered",
                    payload,
                )
            except Exception:
                result = {}
            if bool(result.get("ok")):
                return True
            if attempt < 2:
                await asyncio.sleep(0.2 * (2**attempt))
        return False

    def _mark_channel_activity(self, event: AstrMessageEvent) -> None:
        if self._is_private_event(event):
            return
        channel_id = self._channel_id(event)
        if not channel_id:
            return
        self._channel_activity_versions[channel_id] = self._channel_activity_versions.get(channel_id, 0) + 1
        unified_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if unified_origin:
            self._channel_sessions[channel_id] = unified_origin
        # Do not cancel an HTTP request already running in ``asyncio.to_thread``:
        # cancelling the coroutine cannot stop the backend thread and may hide a
        # GM move that has already committed. The activity version makes an
        # uncommitted beat stale; a committed beat is still delivered.

    def _has_channel_sender(self) -> bool:
        return bool(
            getattr(self.context, "send_message", None)
            or getattr(self.context, "send_msg", None)
            or getattr(self.context, "send_group_message", None)
        )

    async def _send_channel_text(self, channel_id: str, text: str) -> bool:
        if not text:
            return False
        session = self._channel_sessions.get(channel_id, "")
        send_message = getattr(self.context, "send_message", None)
        if session and send_message:
            try:
                result = send_message(session, MessageChain().message(text))
                if hasattr(result, "__await__"):
                    result = await result
                if result is not False:
                    return True
            except Exception:
                pass

        try:
            await StarTools.send_message_by_id(
                "GroupMessage",
                channel_id,
                MessageChain().message(text),
            )
            return True
        except Exception:
            pass

        candidates = (
            ("send_msg", (channel_id, text)),
            ("send_group_message", (channel_id, text)),
        )
        for method_name, args in candidates:
            method = getattr(self.context, method_name, None)
            if not method:
                continue
            try:
                result = method(*args)
                if hasattr(result, "__await__"):
                    await result
                return True
            except TypeError:
                try:
                    result = method(int(channel_id), text)
                    if hasattr(result, "__await__"):
                        await result
                    return True
                except Exception:
                    continue
            except Exception:
                continue
        return False

    async def terminate(self) -> None:
        if self._idle_monitor_task is not None:
            self._idle_monitor_task.cancel()
            try:
                await self._idle_monitor_task
            except asyncio.CancelledError:
                pass
        await self._heartbeat_tasks.close()

    def _config_bool(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "启用", "是"}
        return bool(value)

    def _config_float(self, value: object, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _config_int(self, value: object, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _reply_text(self, response: dict) -> str:
        if response.get("ok") is False:
            return "FU-GM 调用失败：" + str(response.get("error", "未知错误"))
        return str(response.get("reply") or response.get("message") or "FU-GM 没有返回文本。")

    async def _deliver_reply_results(
        self,
        event: AstrMessageEvent,
        response: dict,
    ) -> bool:
        """Send ordinary replies synchronously and confirm each envelope upstream."""

        campaign_id = str(response.get("campaign_id") or "")
        return await self._reply_delivery_coordinator.deliver(
            reply_delivery_specs(response),
            self._reply_results(event, response),
            already_confirmed=bool(response.get("delivery_confirmed")),
            send=event.send,
            confirm=lambda envelope_id: self._confirm_reply_delivery(
                envelope_id,
                campaign_id=campaign_id,
            ),
        )

    async def _recover_unconfirmed_reply_deliveries(self) -> None:
        if not self._reply_delivery_journal.sent:
            return
        async with self._reply_confirmation_recovery_lock:
            await self._reply_delivery_coordinator.recover(
                lambda envelope_id: self._confirm_reply_delivery(
                    envelope_id,
                    campaign_id="",
                )
            )

    async def _confirm_reply_delivery(
        self,
        envelope_id: str,
        *,
        campaign_id: str,
    ) -> bool:
        for attempt in range(3):
            try:
                result = await self._post(
                    "/v1/message/delivered",
                    {
                        "envelope_id": envelope_id,
                        "campaign_id": campaign_id,
                        "platform": "astrbot",
                    },
                )
            except Exception:
                result = {}
            if bool(result.get("ok")):
                return True
            if attempt < 2:
                await asyncio.sleep(0.2 * (2**attempt))
        return False

    def _reply_results(self, event: AstrMessageEvent, response: dict) -> list[MessageEventResult]:
        """Build one AstrBot result per exact FU-GM reply target."""

        if response.get("ok") is False:
            return [event.plain_result(self._reply_text(response))]
        results: list[MessageEventResult] = []
        for spec in reply_delivery_specs(response):
            text = str(spec.get("text") or "").strip()
            target_message_id = str(spec.get("target_message_id") or "").strip()
            media = [
                item
                for item in list(spec.get("media") or [])
                if isinstance(item, dict)
            ]
            if media and Comp is not None and hasattr(event, "chain_result"):
                chain = []
                if (
                    self.enable_exact_reply_quotes
                    and spec.get("quote")
                    and target_message_id
                ):
                    chain.append(Comp.Reply(id=target_message_id))
                if text:
                    chain.append(Comp.Plain(text))
                for item in media:
                    component = self._media_component(item)
                    if component is not None:
                        chain.append(component)
                if chain:
                    results.append(event.chain_result(chain))
                    continue
            if (
                self.enable_exact_reply_quotes
                and spec.get("quote")
                and target_message_id
                and Comp is not None
                and hasattr(event, "chain_result")
            ):
                try:
                    if self.log_http_timing:
                        print(
                            f"[FU-GM Bridge] exact reply target_message_id={target_message_id} "
                            f"envelope_id={spec.get('envelope_id') or '-'}",
                            flush=True,
                        )
                    results.append(event.chain_result([Comp.Reply(id=target_message_id), Comp.Plain(text)]))
                    continue
                except Exception:
                    pass
            results.append(event.plain_result(text))
        if not results:
            fallback = str(response.get("reply") or response.get("message") or "").strip()
            if fallback:
                results.append(event.plain_result(fallback))
        return results

    @staticmethod
    def _media_component(item: dict) -> object | None:
        if Comp is None or str(item.get("type") or "") != "image":
            return None
        path = str(item.get("path") or "").strip()
        if path:
            candidate = Path(path).expanduser()
            if candidate.is_file():
                try:
                    return Comp.Image.fromFileSystem(str(candidate.resolve()))
                except Exception:
                    pass
        url = str(item.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            try:
                return Comp.Image.fromURL(url)
            except Exception:
                return None
        return None

    async def _get(self, path: str) -> dict:
        return await asyncio.to_thread(self._request_sync, "GET", path)

    async def _post(self, path: str, payload: dict) -> dict:
        return await asyncio.to_thread(self._request_sync, "POST", path, payload)

    async def _post_stateful(
        self,
        path: str,
        payload: dict,
        *,
        serialization_key: str = "",
    ) -> dict:
        key = serialization_key or self._request_serialization_key(payload)
        return await self._request_coordinator.run(
            key,
            lambda: self._post(path, payload),
        )

    @staticmethod
    def _request_serialization_key(payload: dict) -> str:
        channel_id = str(payload.get("channel_id") or "").strip()
        if channel_id:
            return f"channel::{channel_id}"
        session_id = str(payload.get("session_id") or "").strip()
        campaign_id = str(payload.get("campaign_id") or "default").strip()
        return f"campaign::{campaign_id}::session::{session_id or 'default'}"

    def _request_sync(self, method: str, path: str, payload: dict | None = None) -> dict:
        started_at = time.monotonic()
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            url=self.server_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(http_request, timeout=self.http_timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
                self._log_request_timing(method, path, started_at, ok=bool(result.get("ok", True)))
                return result
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                result = json.loads(body)
                self._log_request_timing(method, path, started_at, ok=False, error=str(result.get("error", exc)))
                return result
            except json.JSONDecodeError:
                self._log_request_timing(method, path, started_at, ok=False, error=body or str(exc))
                return {"ok": False, "error": body or str(exc)}
        except Exception as exc:
            self._log_request_timing(method, path, started_at, ok=False, error=str(exc))
            return {"ok": False, "error": str(exc)}

    def _log_request_timing(self, method: str, path: str, started_at: float, *, ok: bool, error: str = "") -> None:
        if not self.log_http_timing:
            return
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        status = "ok" if ok else "error"
        suffix = f" error={error[:120]}" if error else ""
        print(f"[FU-GM Bridge] {method} {path} {status} {elapsed_ms}ms{suffix}", flush=True)
