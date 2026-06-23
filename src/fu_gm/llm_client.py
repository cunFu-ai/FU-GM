from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib import error, request

from fu_gm.config import LLMConfig


class Transport(Protocol):
    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        ...


class UrlLibTransport:
    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(url=url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                response_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                response_body = ""
            raise LLMHTTPError(status_code=exc.code, body=response_body) from exc


@dataclass
class ChatMessage:
    role: str
    content: str


class LLMHTTPError(RuntimeError):
    """LLM HTTP 调用失败。

    保留状态码和响应体，方便上层判断是否属于可恢复的上下文物理边界错误。
    """

    def __init__(self, *, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"LLM HTTP {status_code}: {body[:500]}")


@dataclass
class LLMRecoveryAttempt:
    reason: str
    original_chars: int
    retry_chars: int
    attempt: int


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()
        self.last_recovery_attempts: list[LLMRecoveryAttempt] = []
        self.recent_calls: list[dict] = []
        self.total_calls = 0

    def create_chat_completion(
        self,
        model: str,
        messages: list[ChatMessage],
        temperature: float = 0.2,
        response_format: dict | None = None,
    ) -> str:
        self.last_recovery_attempts = []
        current_messages = list(messages)
        max_retries = max(0, int(self.config.reactive_recovery_max_retries))
        attempt = 0
        while True:
            try:
                data = self._post_chat_completion(
                    model=model,
                    messages=current_messages,
                    temperature=temperature,
                    response_format=response_format,
                )
                return data["choices"][0]["message"]["content"]
            except Exception as exc:
                context_error = self._is_recoverable_context_error(exc)
                transient_error = self._is_transient_error(exc)
                if (
                    not self.config.reactive_recovery_enabled
                    or attempt >= max_retries
                    or not (context_error or transient_error)
                ):
                    raise
                original_chars = self._messages_char_count(current_messages)
                attempt += 1
                if context_error:
                    current_messages = self._compact_messages_for_retry(
                        current_messages,
                        reason=str(exc),
                        target_chars=max(4000, int(self.config.reactive_recovery_target_chars)),
                    )
                else:
                    time.sleep(min(1.0, 0.2 * (2 ** (attempt - 1))))
                self.last_recovery_attempts.append(
                    LLMRecoveryAttempt(
                        reason=str(exc),
                        original_chars=original_chars,
                        retry_chars=self._messages_char_count(current_messages),
                        attempt=attempt,
                    )
                )

    def _is_transient_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        if isinstance(exc, (TimeoutError, error.URLError)):
            return True
        text = f"{exc} {getattr(exc, 'body', '')}".lower()
        markers = (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "rate limit",
            "too many requests",
            "upstream error",
        )
        return any(marker in text for marker in markers)

    def _post_chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float,
        response_format: dict | None,
    ) -> dict:
        started = time.monotonic()
        payload = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.thinking_enabled:
            payload["thinking"] = {"type": "enabled"}

        try:
            data = self.transport.post_json(
                url=self.config.chat_completions_url(),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout=self.config.timeout_seconds,
            )
            self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=True,
            )
            return data
        except Exception as exc:
            self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=False,
                error=str(exc),
            )
            raise

    def _is_recoverable_context_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 413:
            return True
        text = f"{exc} {getattr(exc, 'body', '')}".lower()
        recoverable_markers = (
            "prompt_too_long",
            "context_length_exceeded",
            "maximum context",
            "context length",
            "request_too_large",
            "too large",
            "413",
            "token limit",
            "tokens exceeded",
            "input is too long",
            "media_size_error",
            "image too large",
            "pdf too large",
        )
        return any(marker in text for marker in recoverable_markers)

    def _compact_messages_for_retry(
        self,
        messages: list[ChatMessage],
        *,
        reason: str,
        target_chars: int,
    ) -> list[ChatMessage]:
        """最小破坏式恢复压缩。

        system prompt 保持完全不动，避免破坏静态缓存前缀；只压缩 user/assistant
        动态内容，并在最后一条 user message 追加重试标记，让模型知道本轮经历
        过上下文折叠。
        """

        if not messages:
            return messages

        system_messages = [message for message in messages if message.role == "system"]
        dynamic_messages = [message for message in messages if message.role != "system"]
        system_chars = self._messages_char_count(system_messages)
        dynamic_budget = max(1000, target_chars - system_chars)

        if not dynamic_messages:
            return list(messages)

        per_message_budget = max(800, dynamic_budget // len(dynamic_messages))
        compacted_dynamic: list[ChatMessage] = []
        for index, message in enumerate(dynamic_messages):
            budget = per_message_budget
            if index == len(dynamic_messages) - 1:
                budget = max(per_message_budget, dynamic_budget // 2)
            compacted_dynamic.append(
                ChatMessage(
                    role=message.role,
                    content=self._compact_text(message.content, max_chars=budget),
                )
            )

        compacted = [*system_messages, *compacted_dynamic]
        marker = self._recovery_marker(reason)
        for index in range(len(compacted) - 1, -1, -1):
            if compacted[index].role == "user":
                compacted[index] = ChatMessage(
                    role="user",
                    content=f"{compacted[index].content.rstrip()}\n\n{marker}",
                )
                break
        return compacted

    def _compact_text(self, text: str, *, max_chars: int) -> str:
        text = str(text)
        if len(text) <= max_chars:
            return text
        if max_chars <= 200:
            return text[:max_chars]
        head_chars = max(200, int(max_chars * 0.58))
        tail_chars = max(200, max_chars - head_chars - 240)
        omission = (
            "\n\n<system-reminder title=\"上下文折叠\">\n"
            f"此处因上一次 LLM 请求超过上下文或请求体限制，省略了约 {len(text) - head_chars - tail_chars} 个字符。"
            "请优先相信保留下来的硬规则、最新玩家输入和结算结果；不要臆造被省略的内容。\n"
            "</system-reminder>\n\n"
        )
        return f"{text[:head_chars]}{omission}{text[-tail_chars:]}"

    def _recovery_marker(self, reason: str) -> str:
        reason = " ".join(str(reason).split())
        if len(reason) > 300:
            reason = f"{reason[:300]}..."
        return (
            '<system-reminder title="错误恢复重试">\n'
            "上一次 LLM 请求触发了可恢复的上下文或请求体边界错误，系统已保留静态 system prompt，"
            "并对动态 user/assistant 内容做了最小破坏式折叠后重试。\n"
            f"错误摘要：{reason}\n"
            "如果缺少旧细节，请基于当前仍可见的信息继续，不要编造被折叠的内容。\n"
            "</system-reminder>"
        )

    def _messages_char_count(self, messages: list[ChatMessage]) -> int:
        return sum(len(str(message.content)) for message in messages)

    def _record_call(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        response_format: dict | None,
        elapsed_ms: int,
        ok: bool,
        error: str = "",
    ) -> None:
        self.total_calls += 1
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "message_count": len(messages),
            "prompt_chars": self._messages_char_count(messages),
            "response_format": bool(response_format),
            "reasoning_effort": bool(self.config.reasoning_effort),
            "thinking_enabled": bool(self.config.thinking_enabled),
        }
        if error:
            record["error"] = error[:500]
        self.recent_calls.append(record)
        self.recent_calls = self.recent_calls[-50:]

    def telemetry_payload(self) -> dict:
        slowest = sorted(self.recent_calls, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:5]
        last = self.recent_calls[-1] if self.recent_calls else {}
        recent = self.recent_calls[-10:]
        average = 0
        if recent:
            average = int(sum(int(item.get("elapsed_ms", 0)) for item in recent) / len(recent))
        return {
            "total_calls": self.total_calls,
            "last_call": last,
            "recent_calls": recent,
            "slowest_recent": slowest,
            "average_recent_elapsed_ms": average,
        }
