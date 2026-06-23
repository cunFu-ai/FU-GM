from __future__ import annotations

import json
import asyncio
import time
from pathlib import Path
from sys import maxsize
from urllib import request
from urllib.error import HTTPError

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # 兼容插件单独测试或旧版 AstrBot。
    get_astrbot_data_path = None

try:
    from .message_buffer import DebouncedMessageBuffer
except ImportError:  # AstrBot 有时会把插件目录直接加入 sys.path。
    from message_buffer import DebouncedMessageBuffer


@register("fu_gm_bridge", "cunfu", "把 AstrBot 群聊消息桥接到 FU-GM HTTP 服务。", "0.1.0")
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
        self.casual_prefixes = [str(item) for item in config.get("casual_prefixes", ["时悠", "悠老师", "gm"]) if str(item)]
        self.game_prefixes = [str(item) for item in config.get("game_prefixes", ["跑团", "行动"]) if str(item)]
        self.enable_private_safety_auto = self._config_bool(config.get("enable_private_safety_auto", True))
        self.anonymous_private_safety = self._config_bool(config.get("anonymous_private_safety", True))
        self.enable_natural_routing = self._config_bool(config.get("enable_natural_routing", True))
        self.natural_route_group_messages = self._config_bool(config.get("natural_route_group_messages", True))
        self.natural_route_private_messages = self._config_bool(config.get("natural_route_private_messages", True))
        self.block_silent_table_talk = self._config_bool(config.get("block_silent_table_talk", True))
        self.http_timeout_seconds = self._config_float(config.get("http_timeout_seconds", 120), default=120.0)
        self.log_http_timing = self._config_bool(config.get("log_http_timing", True))
        self.enable_message_buffer = self._config_bool(config.get("enable_message_buffer", True))
        self.message_buffer = DebouncedMessageBuffer(
            debounce_seconds=self._config_float(config.get("buffer_debounce_seconds", 3.0), default=3.0),
            max_wait_seconds=self._config_float(config.get("buffer_max_wait_seconds", 12.0), default=12.0),
            max_messages=self._config_int(config.get("buffer_max_messages", 5), default=5),
        )
        self._route_locks: dict[str, asyncio.Lock] = {}
        self.plugin_data_dir = self._plugin_data_dir()
        self.state_path = self._state_path_from_config(config, "campaign_bindings_path", "channel_campaigns.json")
        self.user_state_path = self._state_path_from_config(
            config,
            "user_campaign_bindings_path",
            "user_campaigns.json",
        )
        self.channel_campaigns = self._load_json_map(self.state_path)
        self.user_campaigns = self._load_json_map(self.user_state_path)

    @filter.command("fugm")
    async def fugm_turn(self, event: AstrMessageEvent) -> MessageEventResult:
        """跑团回合：/fugm 我攻击宝箱王"""
        message = event.message_str.removeprefix("/fugm").strip()
        payload = self._payload(event, message=message, mode="game")
        response = await self._post("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_chat")
    async def fugm_chat(self, event: AstrMessageEvent) -> MessageEventResult:
        """水群聊天：/fugm_chat 还记得上次宝箱王那段吗"""
        message = event.message_str.removeprefix("/fugm_chat").strip()
        payload = self._payload(event, message=message, mode="casual")
        response = await self._post("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_s0")
    async def fugm_session_zero(self, event: AstrMessageEvent) -> MessageEventResult:
        """Session 0：/fugm_s0 我想要地下城宝箱和奇遇"""
        message = event.message_str.removeprefix("/fugm_s0").strip()
        payload = self._payload(event, message=message, mode="session_zero")
        response = await self._post("/v1/chat", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_safety")
    async def fugm_safety(self, event: AstrMessageEvent) -> MessageEventResult:
        """设置界限与帷幕：私聊使用时默认匿名。"""
        message = event.message_str.removeprefix("/fugm_safety").strip()
        if not message:
            yield event.plain_result("可以直接说：/fugm_safety 我不希望出现 X，或 /fugm_safety X 请淡出处理。")
            return
        payload = self._payload(event, message=message, mode="safety")
        response = await self._post("/v1/safety/declare", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_end")
    async def fugm_end_session(self, event: AstrMessageEvent) -> MessageEventResult:
        """结束并整理本场：/fugm_end 星尘迷宫第一夜"""
        title = event.message_str.removeprefix("/fugm_end").strip()
        payload = self._payload(event, message="", mode="end")
        payload["title"] = title
        response = await self._post("/v1/session/end", payload)
        summary = response.get("summary", {})
        reply = summary.get("short_memory") or summary.get("public_summary") or self._reply_text(response)
        yield event.plain_result(f"本场已经整理好啦：{reply}")

    @filter.command("fugm_campaign")
    async def fugm_campaign(self, event: AstrMessageEvent) -> MessageEventResult:
        """查看或切换当前群绑定的团：/fugm_campaign 星尘宝箱谭"""
        campaign_id = event.message_str.removeprefix("/fugm_campaign").strip()
        channel_id = self._channel_id(event)
        if not campaign_id:
            yield event.plain_result(f"当前群绑定的 FU-GM 团：{self._campaign_id(event)}")
            return
        self.channel_campaigns[channel_id] = campaign_id
        self._save_channel_campaigns()
        self._remember_user_campaign(event, campaign_id)
        yield event.plain_result(f"已将当前群绑定到《{campaign_id}》。如果本地有同名快照，FU-GM 会在使用时自动读档。")

    @filter.command("fugm_campaigns")
    async def fugm_campaigns(self, event: AstrMessageEvent) -> MessageEventResult:
        """列出 FU-GM 服务已知团。"""
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
        slot = event.message_str.removeprefix("/fugm_save").strip()
        payload = self._payload(event, message="", mode="save")
        if slot:
            payload["slot"] = slot
        response = await self._post("/v1/campaigns/save", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_load")
    async def fugm_load(self, event: AstrMessageEvent) -> MessageEventResult:
        """读档：/fugm_load，/fugm_load 团名，或 /fugm_load 团名 存档槽"""
        args = event.message_str.removeprefix("/fugm_load").strip().split()
        campaign_id = self._campaign_id(event)
        slot = ""
        if len(args) == 1:
            campaign_id = args[0]
        elif len(args) >= 2:
            campaign_id = self._campaign_id(event) if args[0] == "." else args[0]
            slot = " ".join(args[1:])
        payload = self._payload(event, message="", mode="load")
        payload["campaign_id"] = campaign_id
        if slot:
            payload["slot"] = slot
        response = await self._post("/v1/campaigns/load", payload)
        if response.get("ok"):
            self.channel_campaigns[self._channel_id(event)] = campaign_id
            self._save_channel_campaigns()
            self._remember_user_campaign(event, campaign_id)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_delete_save")
    async def fugm_delete_save(self, event: AstrMessageEvent) -> MessageEventResult:
        """删除当前团的最新快照或命名存档槽：/fugm_delete_save boss战前"""
        slot = event.message_str.removeprefix("/fugm_delete_save").strip()
        payload = self._payload(event, message="", mode="delete_save")
        if slot:
            payload["slot"] = slot
        response = await self._post("/v1/campaigns/delete", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_delete_campaign")
    async def fugm_delete_campaign(self, event: AstrMessageEvent) -> MessageEventResult:
        """删除当前群绑定的整个 FU-GM 战役目录：/fugm_delete_campaign 确认删除"""
        confirm = event.message_str.removeprefix("/fugm_delete_campaign").strip()
        payload = self._payload(event, message="", mode="delete_campaign")
        payload["delete_all"] = True
        payload["confirm"] = confirm
        response = await self._post("/v1/campaigns/delete", payload)
        if response.get("ok"):
            channel_id = self._channel_id(event)
            if channel_id in self.channel_campaigns:
                del self.channel_campaigns[channel_id]
                self._save_channel_campaigns()
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_away")
    async def fugm_away(self, event: AstrMessageEvent) -> MessageEventResult:
        """标记自己临时离席并自动保存：/fugm_away 去吃饭"""
        reason = event.message_str.removeprefix("/fugm_away").strip()
        payload = self._payload(event, message=reason, mode="away")
        payload["reason"] = reason
        response = await self._post("/v1/session/away", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_back")
    async def fugm_back(self, event: AstrMessageEvent) -> MessageEventResult:
        """标记自己回到本场。"""
        payload = self._payload(event, message="", mode="back")
        response = await self._post("/v1/session/back", payload)
        yield event.plain_result(self._reply_text(response))

    @filter.command("fugm_status")
    async def fugm_status(self, event: AstrMessageEvent) -> MessageEventResult:
        """查看当前团、场景与离席状态。"""
        payload = self._payload(event, message="", mode="status")
        response = await self._post("/v1/session/status", payload)
        yield event.plain_result(self._format_status_response(response))

    @filter.command("fugm_health")
    async def fugm_health(self, event: AstrMessageEvent) -> MessageEventResult:
        """检查 FU-GM 服务。"""
        response = await self._get("/health")
        yield event.plain_result("FU-GM 服务状态：" + json.dumps(response, ensure_ascii=False))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize - 20)
    async def passive_prefix_router(self, event: AstrMessageEvent) -> MessageEventResult:
        """自然路由。

        优先支持自然说话；前缀仍保留为强制路由和调试入口。
        """
        raw = event.message_str.strip()
        if (
            self.enable_private_safety_auto
            and self._is_private_event(event)
            and raw
            and not raw.startswith("/")
            and self._looks_like_safety_message(raw)
        ):
            payload = self._payload(event, message=raw, mode="safety")
            response = await self._post("/v1/safety/declare", payload)
            result = event.plain_result(self._reply_text(response)).stop_event()
            yield result
            return

        mode = ""
        message = ""
        for prefix in self.game_prefixes:
            if raw.startswith(prefix):
                mode = "game"
                message = raw[len(prefix) :].strip(" ：:，,")
                break
        if not mode:
            for prefix in self.casual_prefixes:
                if raw.startswith(prefix):
                    mode = "casual"
                    message = raw[len(prefix) :].strip(" ：:，,")
                    break
        if mode and message:
            if mode == "casual" and self._looks_like_status_request(message):
                payload = self._payload(event, message="", mode="status")
                response = await self._post("/v1/session/status", payload)
                result = event.plain_result(self._format_status_response(response)).stop_event()
                yield result
                return
            payload = self._payload(event, message=message, mode=mode)
            response = await self._post("/v1/chat", payload)
            if response.get("ok") and response.get("suppressed") and not response.get("reply"):
                event.stop_event()
                return
            result = event.plain_result(self._reply_text(response)).stop_event()
            yield result
            return

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
            response = await self._post("/v1/message/route", payload)
        if response.get("ok") is False:
            return
        if response.get("send_reply") and response.get("reply"):
            result = event.plain_result(self._reply_text(response)).stop_event()
            yield result
            return
        if response.get("stop_astrbot") and self.block_silent_table_talk:
            event.stop_event()
            return

    def _payload(self, event: AstrMessageEvent, *, message: str, mode: str) -> dict:
        campaign_id = self._campaign_id(event)
        if not self._is_private_event(event):
            self._ensure_channel_campaign_binding(event, campaign_id)
            self._remember_user_campaign(event, campaign_id)
        return {
            "campaign_id": campaign_id,
            "session_id": self._session_id(event),
            "speaker": event.get_sender_name() or str(event.get_sender_id()),
            "message": message,
            "channel_id": self._channel_id(event),
            "mode": mode,
            "anonymous": mode == "safety" and self.anonymous_private_safety and self._is_private_event(event),
        }

    def _session_id(self, event: AstrMessageEvent) -> str:
        return self._channel_id(event) or self.default_session_id

    def _looks_like_status_request(self, message: str) -> bool:
        compact = "".join(str(message or "").split())
        return bool(compact) and any(
            token in compact
            for token in (
                "当前跑团状态",
                "跑团状态",
                "当前状态",
                "现在状态",
                "现在是什么阶段",
                "当前是什么阶段",
                "进度到哪",
                "进度怎么样",
            )
        )

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
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.channel_campaigns, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_user_campaigns(self) -> None:
        self.user_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_state_path.write_text(json.dumps(self.user_campaigns, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remember_user_campaign(self, event: AstrMessageEvent, campaign_id: str) -> None:
        user_key = self._user_key(event)
        if not user_key or not campaign_id:
            return
        if self.user_campaigns.get(user_key) == campaign_id:
            return
        self.user_campaigns[user_key] = campaign_id
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

    def _looks_like_safety_message(self, message: str) -> bool:
        tokens = (
            "不希望",
            "不想",
            "不接受",
            "接受不了",
            "不要出现",
            "不要有",
            "禁止",
            "别出现",
            "不能出现",
            "请带过",
            "带过",
            "淡出",
            "一笔带过",
            "不要详细",
            "不舒服",
        )
        return any(token in message for token in tokens)

    def _looks_like_immediate_control_message(self, message: str) -> bool:
        tokens = (
            "开始跑团",
            "今晚开团",
            "开团",
            "开启最终物语跑团",
            "开始最终物语跑团",
            "开启最终物语",
            "开始最终物语",
            "最终物语开团",
            "最终物语跑团",
            "开最终物语",
            "开始第零章",
            "进入第零章",
            "开启最终物语第零章",
            "开始最终物语第零章",
            "继续上次冒险",
            "暂停跑团",
            "先暂停",
            "暂停一下",
            "收团",
            "结束跑团",
            "今天到这",
            "存档",
            "读档",
            "离席",
            "我回来了",
            "回到本场",
        )
        return any(token in message for token in tokens)

    def _natural_routing_enabled_for(self, event: AstrMessageEvent, message: str) -> bool:
        if not self.enable_natural_routing:
            return False
        if not message or message.startswith("/"):
            return False
        if self._is_private_event(event):
            return self.natural_route_private_messages
        return self.natural_route_group_messages

    async def _should_buffer_natural_message(self, event: AstrMessageEvent, message: str) -> bool:
        if not self.enable_message_buffer:
            return False
        if self._is_private_event(event):
            return False
        if self._looks_like_safety_message(message) or self._looks_like_immediate_control_message(message):
            return False
        payload = self._payload(event, message="", mode="status")
        response = await self._post("/v1/session/gate", payload)
        gate = response.get("gate", {}) if response.get("ok") else {}
        return gate.get("status") in {"adventure", "session_zero"}

    def _buffer_key(self, event: AstrMessageEvent) -> str:
        return f"{self._campaign_id(event)}::{self._session_id(event)}::{self._channel_id(event)}"

    async def _route_buffered_payload(self, key: str, payload: dict) -> dict:
        lock = self._route_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._post("/v1/message/route", payload)

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

    async def _get(self, path: str) -> dict:
        return await asyncio.to_thread(self._request_sync, "GET", path)

    async def _post(self, path: str, payload: dict) -> dict:
        return await asyncio.to_thread(self._request_sync, "POST", path, payload)

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
