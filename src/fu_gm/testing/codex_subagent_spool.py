from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodexSpoolConfig:
    """测试客户端向现有调用者暴露的最小配置视图。"""

    timeout_seconds: float
    response_format_enabled: bool = True


class CodexSubagentSpoolClient:
    """通过本地文件队列等待 Codex 子智能体回复的测试专用客户端。

    该客户端没有网络监听能力，也不会读取生产 API Key。调用方必须显式
    传入 ``test_only=True``，防止它被误接到真实 AstrBot 服务。
    """

    provider_name = "codex_subagent"

    def __init__(
        self,
        root: str | Path,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 0.25,
        test_only: bool = False,
    ) -> None:
        if not test_only:
            raise ValueError("Codex spool 只能在显式 test_only 模式下使用。")
        self.test_only = True
        self.root = Path(root).expanduser().resolve()
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self.cancelled_dir = self.root / "cancelled"
        self.invalid_dir = self.root / "invalid"
        for directory in (
            self.requests_dir,
            self.responses_dir,
            self.cancelled_dir,
            self.invalid_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = CodexSpoolConfig(timeout_seconds=max(1.0, float(timeout_seconds)))
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[Any],
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        allow_empty: bool = False,
        deadline: float | None = None,
        operation: str = "chat_completion",
        thinking_enabled: bool | None = None,
        max_recovery_retries: int | None = None,
        retry_without_response_format_on_empty: bool = False,
    ) -> str:
        request_id = uuid.uuid4().hex
        serialized_messages = [
            {
                "role": str(getattr(message, "role", "user") or "user"),
                "content": str(getattr(message, "content", "") or ""),
            }
            for message in messages
        ]
        request_body: dict[str, Any] = {
            "schema_version": 1,
            "request_id": request_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "test_only": True,
            "provider": self.provider_name,
            "operation": str(operation or "chat_completion"),
            "agent_role": self._agent_role(operation),
            "model": str(model or ""),
            "temperature": float(temperature),
            "max_tokens": int(max_tokens) if max_tokens is not None else None,
            "thinking_enabled": (
                bool(thinking_enabled) if thinking_enabled is not None else None
            ),
            "max_recovery_retries": (
                int(max_recovery_retries)
                if max_recovery_retries is not None
                else None
            ),
            "retry_without_response_format_on_empty": bool(
                retry_without_response_format_on_empty
            ),
            "response_format": (
                dict(response_format) if response_format is not None else None
            ),
            "output_contract": self._output_contract(
                operation,
                response_format=response_format,
            ),
            "messages": serialized_messages,
        }
        request_body["payload_sha256"] = self._sha256(request_body)
        request_path = self.requests_dir / f"{request_id}.json"
        response_path = self.responses_dir / f"{request_id}.json"
        self._atomic_write_json(request_path, request_body)

        started = time.monotonic()
        local_deadline = started + self.config.timeout_seconds
        if deadline is not None:
            local_deadline = min(local_deadline, float(deadline))
        call_record = {
            "request_id": request_id,
            "operation": request_body["operation"],
            "model": request_body["model"],
            "request_path": str(request_path),
            "response_path": str(response_path),
            "status": "waiting",
        }
        self.calls.append(call_record)

        while time.monotonic() < local_deadline:
            if not response_path.exists():
                time.sleep(self.poll_interval_seconds)
                continue
            raw_response = ""
            try:
                raw_response = response_path.read_text(encoding="utf-8")
                response = json.loads(raw_response)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self._quarantine_invalid_response(
                    response_path,
                    raw_response=raw_response,
                    reason=f"响应文件尚不可解析：{exc}",
                    call_record=call_record,
                )
                time.sleep(self.poll_interval_seconds)
                continue
            if str(response.get("request_id") or "") != request_id:
                self._quarantine_invalid_response(
                    response_path,
                    raw_response=raw_response,
                    reason="Codex spool 响应 request_id 与请求不匹配。",
                    call_record=call_record,
                )
                time.sleep(self.poll_interval_seconds)
                continue
            if str(response.get("request_payload_sha256") or "") != str(
                request_body["payload_sha256"]
            ):
                self._quarantine_invalid_response(
                    response_path,
                    raw_response=raw_response,
                    reason="Codex spool 响应没有匹配原始请求摘要。",
                    call_record=call_record,
                )
                time.sleep(self.poll_interval_seconds)
                continue
            status = str(response.get("status") or "").strip().lower()
            if status == "failed":
                call_record["status"] = "failed"
                raise RuntimeError(
                    "Codex 子智能体没有完成测试请求："
                    + str(response.get("error") or response.get("error_type") or "未知错误")
                )
            if status != "completed":
                self._quarantine_invalid_response(
                    response_path,
                    raw_response=raw_response,
                    reason="Codex spool 响应 status 必须为 completed 或 failed。",
                    call_record=call_record,
                )
                time.sleep(self.poll_interval_seconds)
                continue
            content = str(response.get("content") or "")
            if not allow_empty and not content.strip():
                self._quarantine_invalid_response(
                    response_path,
                    raw_response=raw_response,
                    reason="Codex spool 返回了空内容。",
                    call_record=call_record,
                )
                time.sleep(self.poll_interval_seconds)
                continue
            call_record.update(
                {
                    "status": "completed",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "response_provider": str(response.get("provider") or ""),
                    "worker_id": str(response.get("worker_id") or ""),
                }
            )
            return content

        call_record["status"] = "timeout"
        self._atomic_write_json(
            self.cancelled_dir / f"{request_id}.json",
            {
                "schema_version": 1,
                "request_id": request_id,
                "status": "cancelled",
                "cancelled_at": datetime.now(timezone.utc).isoformat(),
                "reason": "spool_timeout",
            },
        )
        raise TimeoutError(f"等待 Codex 子智能体响应超时：{request_id}")

    def _quarantine_invalid_response(
        self,
        response_path: Path,
        *,
        raw_response: str,
        reason: str,
        call_record: dict[str, Any],
    ) -> None:
        """隔离瞬时无效响应，并继续等待同一请求的正确版本。"""

        count = int(call_record.get("invalid_response_count") or 0) + 1
        call_record["invalid_response_count"] = count
        call_record["last_invalid_response_error"] = str(reason)
        try:
            if not response_path.exists():
                return
            current = response_path.read_text(encoding="utf-8")
            if current != raw_response:
                return
            quarantine_path = self.invalid_dir / (
                f"{response_path.stem}.{count:03d}.json"
            )
            response_path.replace(quarantine_path)
            paths = call_record.setdefault("invalid_response_paths", [])
            if isinstance(paths, list):
                paths.append(str(quarantine_path))
        except OSError:
            # 工作器可能恰好正在原子替换文件；下一轮重新读取即可。
            return

    @staticmethod
    def _agent_role(operation: str) -> str:
        clean = str(operation or "chat_completion").strip()
        player_prefix = "fu_pl.generate."
        if clean.startswith(player_prefix):
            player_name = clean[len(player_prefix) :].strip()
            if player_name:
                return f"player:{player_name}"
        if clean.startswith("fu_pl."):
            return "player:shared"
        return "gm"

    @staticmethod
    def _output_contract(
        operation: str,
        *,
        response_format: dict[str, Any] | None,
    ) -> dict[str, object]:
        """Make test-worker auditing distinguish players, GM tools and prose."""

        clean = str(operation or "chat_completion")
        if clean.startswith("fu_pl."):
            return {
                "kind": "player_simulator_decision",
                "json_required": True,
                "tool_calls_allowed": False,
                "required_fields": ["decision", "audience", "text", "reason"],
                "note": "speak表示玩家发言，不是GM工具调用。",
            }
        if clean.startswith("gm_tool_agent."):
            return {
                "kind": "gm_agent_decision",
                "json_required": True,
                "tool_calls_allowed": True,
                "required_fields": ["decision"],
            }
        return {
            "kind": "component_completion",
            "json_required": bool(
                isinstance(response_format, dict)
                and response_format.get("type") == "json_object"
            ),
            "tool_calls_allowed": False,
        }

    def telemetry_payload(self) -> dict[str, Any]:
        completed = sum(1 for call in self.calls if call.get("status") == "completed")
        failed = sum(
            1
            for call in self.calls
            if call.get("status") in {"failed", "timeout", "invalid_response"}
        )
        pending = sum(1 for call in self.calls if call.get("status") == "waiting")
        return {
            "provider": self.provider_name,
            "test_only": True,
            "total_calls": len(self.calls),
            "completed_calls": completed,
            "failed_calls": failed,
            "pending_calls": pending,
            "prompt_cache": {"reported": False, "reason": "not_available"},
            "calls": list(self.calls),
        }

    @staticmethod
    def _sha256(payload: dict[str, Any]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


__all__ = ["CodexSpoolConfig", "CodexSubagentSpoolClient"]
