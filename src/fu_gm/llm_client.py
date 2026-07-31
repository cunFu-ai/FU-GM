from __future__ import annotations

import json
import http.client
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib import error, request

from fu_gm.config import LLMConfig, uses_high_latency_model


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


class LLMEmptyResponseError(RuntimeError):
    """Gateway returned HTTP 200 but no assistant text.

    An empty response is recoverable for decisions and player-facing prose, but
    some callers deliberately allow an empty optional prose supplement.
    """


class LLMDeadlineExceeded(TimeoutError):
    """One logical LLM operation exhausted its shared wall-clock budget."""

    def __init__(self, *, operation: str, elapsed_seconds: float) -> None:
        self.operation = str(operation or "llm_request")
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        super().__init__(
            f"LLM operation {self.operation!r} exceeded its "
            f"{self.elapsed_seconds:.2f}s wall-clock budget"
        )


class LLMProviderCircuitOpen(RuntimeError):
    """All configured endpoints for one model are temporarily unavailable."""

    def __init__(self, *, model: str, retry_after_seconds: float) -> None:
        self.model = str(model or "")
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            f"LLM provider circuit is open for model {self.model!r}; "
            f"retry after {self.retry_after_seconds:.1f}s"
        )


@dataclass
class LLMRecoveryAttempt:
    reason: str
    original_chars: int
    retry_chars: int
    attempt: int


class OpenAICompatibleClient:
    def __init__(
        self,
        config: LLMConfig,
        transport: Transport | None = None,
        *,
        circuit_breaker_enabled: bool = False,
        circuit_failure_threshold: int = 1,
        circuit_cooldown_seconds: float = 30.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()
        self.last_recovery_attempts: list[LLMRecoveryAttempt] = []
        self.recent_calls: list[dict] = []
        self.call_latency_history_ms: list[int] = []
        self.failed_call_count = 0
        self.total_calls = 0
        self.circuit_breaker_enabled = bool(circuit_breaker_enabled)
        self.circuit_failure_threshold = max(1, int(circuit_failure_threshold))
        self.circuit_cooldown_seconds = max(0.1, float(circuit_cooldown_seconds))
        self._monotonic = monotonic or time.monotonic
        self._circuit_lock = threading.RLock()
        self._circuit_states: dict[tuple[str, str], dict[str, object]] = {}

    def create_chat_completion(
        self,
        model: str,
        messages: list[ChatMessage],
        temperature: float = 0.2,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        allow_empty: bool = False,
        *,
        deadline: float | None = None,
        operation: str = "chat_completion",
    ) -> str:
        operation_started = self._monotonic()
        operation_budget = max(0.1, float(self.config.timeout_seconds))
        operation_deadline = (
            float(deadline)
            if deadline is not None
            else operation_started + operation_budget
        )
        self.last_recovery_attempts = []
        current_messages = list(messages)
        current_response_format = response_format
        response_format_fallback_used = False
        endpoint_urls = self.config.chat_completions_urls()
        self._acquire_circuit_permission(model=model, endpoint_urls=endpoint_urls)
        endpoint_index = 0
        max_retries = max(0, int(self.config.reactive_recovery_max_retries))
        attempted_endpoints: set[str] = set()
        last_circuit_failure = False
        attempt = 0
        while True:
            remaining = operation_deadline - self._monotonic()
            if remaining <= 0:
                self._complete_circuit_failure(
                    model=model,
                    endpoint_urls=endpoint_urls,
                    circuit_failure=last_circuit_failure,
                    all_endpoints_attempted=len(attempted_endpoints) >= len(endpoint_urls),
                    error="shared operation deadline exceeded",
                )
                raise LLMDeadlineExceeded(
                    operation=operation,
                    elapsed_seconds=self._monotonic() - operation_started,
                )
            endpoint_url = endpoint_urls[endpoint_index]
            attempted_endpoints.add(endpoint_url)
            try:
                attempt_timeout = min(float(self.config.timeout_seconds), remaining)
                if len(endpoint_urls) > 1 and self.config.endpoint_attempt_timeout_seconds > 0:
                    attempt_timeout = min(
                        attempt_timeout,
                        float(self.config.endpoint_attempt_timeout_seconds),
                    )
                data = self._post_chat_completion(
                    model=model,
                    messages=current_messages,
                    temperature=temperature,
                    response_format=current_response_format,
                    max_tokens=max_tokens,
                    endpoint_url=endpoint_url,
                    timeout_seconds=attempt_timeout,
                    operation=operation,
                    attempt=attempt + 1,
                )
                content = self._extract_content(data)
                if not allow_empty and not content.strip():
                    self._mark_last_call_empty()
                    raise LLMEmptyResponseError("LLM gateway returned an empty assistant response")
                self._record_circuit_success(model=model, endpoint_urls=endpoint_urls)
                return content
            except Exception as exc:
                if isinstance(exc, (LLMDeadlineExceeded, LLMProviderCircuitOpen)):
                    self._release_half_open_probe(model=model, endpoint_urls=endpoint_urls)
                    raise
                context_error = self._is_recoverable_context_error(exc)
                transient_error = self._is_transient_error(exc)
                last_circuit_failure = bool(transient_error and not context_error)
                if self._monotonic() >= operation_deadline:
                    self._complete_circuit_failure(
                        model=model,
                        endpoint_urls=endpoint_urls,
                        circuit_failure=last_circuit_failure,
                        all_endpoints_attempted=len(attempted_endpoints) >= len(endpoint_urls),
                        error=str(exc),
                    )
                    raise LLMDeadlineExceeded(
                        operation=operation,
                        elapsed_seconds=self._monotonic() - operation_started,
                    ) from exc
                response_format_error = self._is_response_format_compatibility_error(
                    exc,
                    model=model,
                    response_format=current_response_format,
                    already_retried=response_format_fallback_used,
                )
                if (
                    not self.config.reactive_recovery_enabled
                    or attempt >= max_retries
                    or not (context_error or transient_error or response_format_error)
                ):
                    self._complete_circuit_failure(
                        model=model,
                        endpoint_urls=endpoint_urls,
                        circuit_failure=last_circuit_failure,
                        all_endpoints_attempted=len(attempted_endpoints) >= len(endpoint_urls),
                        error=str(exc),
                    )
                    raise
                original_chars = self._messages_char_count(current_messages)
                attempt += 1
                if context_error:
                    current_messages = self._compact_messages_for_retry(
                        current_messages,
                        reason=str(exc),
                        target_chars=max(4000, int(self.config.reactive_recovery_target_chars)),
                    )
                elif response_format_error:
                    # Some OpenAI-compatible gateways advertise Luna but route
                    # ``response_format`` to an unavailable structured-output
                    # backend.  The prompts already require one JSON object and
                    # every caller validates/parses it, so one plain completion
                    # is safer than failing the whole game turn.
                    current_response_format = None
                    response_format_fallback_used = True
                else:
                    switched_endpoint = False
                    completed_endpoint_cycle = False
                    if len(endpoint_urls) > 1:
                        endpoint_index = (endpoint_index + 1) % len(endpoint_urls)
                        switched_endpoint = True
                        completed_endpoint_cycle = attempt >= len(endpoint_urls)
                    # Switching to a fresh backup is worth trying immediately.
                    # Once every endpoint has failed, however, cycling between
                    # aliases without a pause just exhausts retries inside the
                    # same upstream outage burst.
                    backoff = (
                        0.0
                        if switched_endpoint and not completed_endpoint_cycle
                        else min(12.0, 0.5 * (2 ** (attempt - 1)))
                    )
                    remaining = operation_deadline - self._monotonic()
                    if remaining <= 0:
                        self._complete_circuit_failure(
                            model=model,
                            endpoint_urls=endpoint_urls,
                            circuit_failure=last_circuit_failure,
                            all_endpoints_attempted=len(attempted_endpoints) >= len(endpoint_urls),
                            error=str(exc),
                        )
                        raise LLMDeadlineExceeded(
                            operation=operation,
                            elapsed_seconds=self._monotonic() - operation_started,
                        ) from exc
                    if backoff > 0:
                        time.sleep(min(backoff, remaining))
                self.last_recovery_attempts.append(
                    LLMRecoveryAttempt(
                        reason=(
                            f"{exc}；已移除 response_format 进行一次兼容重试"
                            if response_format_error
                            else str(exc)
                        ),
                        original_chars=original_chars,
                        retry_chars=self._messages_char_count(current_messages),
                        attempt=attempt,
                    )
                )

    def _circuit_key(self, *, model: str, endpoint_urls: tuple[str, ...]) -> tuple[str, str]:
        return ("|".join(endpoint_urls), str(model or "").strip())

    def _acquire_circuit_permission(
        self,
        *,
        model: str,
        endpoint_urls: tuple[str, ...],
    ) -> None:
        if not self.circuit_breaker_enabled:
            return
        key = self._circuit_key(model=model, endpoint_urls=endpoint_urls)
        now = self._monotonic()
        with self._circuit_lock:
            state = self._circuit_states.setdefault(
                key,
                {
                    "state": "closed",
                    "consecutive_failures": 0,
                    "opened_at": 0.0,
                    "open_until": 0.0,
                    "probe_in_flight": False,
                    "last_error": "",
                },
            )
            if state["state"] == "closed":
                return
            retry_after = max(0.0, float(state["open_until"]) - now)
            if state["state"] == "half_open" or retry_after > 0 or bool(state["probe_in_flight"]):
                raise LLMProviderCircuitOpen(
                    model=model,
                    retry_after_seconds=retry_after,
                )
            state["state"] = "half_open"
            state["probe_in_flight"] = True

    def _record_circuit_success(
        self,
        *,
        model: str,
        endpoint_urls: tuple[str, ...],
    ) -> None:
        if not self.circuit_breaker_enabled:
            return
        key = self._circuit_key(model=model, endpoint_urls=endpoint_urls)
        with self._circuit_lock:
            self._circuit_states[key] = {
                "state": "closed",
                "consecutive_failures": 0,
                "opened_at": 0.0,
                "open_until": 0.0,
                "probe_in_flight": False,
                "last_error": "",
            }

    def _complete_circuit_failure(
        self,
        *,
        model: str,
        endpoint_urls: tuple[str, ...],
        circuit_failure: bool,
        all_endpoints_attempted: bool,
        error: str,
    ) -> None:
        if not self.circuit_breaker_enabled:
            return
        key = self._circuit_key(model=model, endpoint_urls=endpoint_urls)
        now = self._monotonic()
        with self._circuit_lock:
            state = self._circuit_states.setdefault(
                key,
                {
                    "state": "closed",
                    "consecutive_failures": 0,
                    "opened_at": 0.0,
                    "open_until": 0.0,
                    "probe_in_flight": False,
                    "last_error": "",
                },
            )
            state["probe_in_flight"] = False
            if not circuit_failure or not all_endpoints_attempted:
                return
            failures = int(state["consecutive_failures"]) + 1
            state["consecutive_failures"] = failures
            state["last_error"] = str(error or "")[:500]
            if state["state"] == "half_open" or failures >= self.circuit_failure_threshold:
                state["state"] = "open"
                state["opened_at"] = now
                state["open_until"] = now + self.circuit_cooldown_seconds

    def _release_half_open_probe(
        self,
        *,
        model: str,
        endpoint_urls: tuple[str, ...],
    ) -> None:
        if not self.circuit_breaker_enabled:
            return
        key = self._circuit_key(model=model, endpoint_urls=endpoint_urls)
        with self._circuit_lock:
            state = self._circuit_states.get(key)
            if state is None:
                return
            state["probe_in_flight"] = False
            if state["state"] == "half_open":
                state["state"] = "closed"

    def circuit_breaker_payload(self) -> dict:
        now = self._monotonic()
        with self._circuit_lock:
            circuits = []
            for (endpoints_key, model), state in sorted(self._circuit_states.items()):
                circuits.append(
                    {
                        "model": model,
                        "endpoints": endpoints_key.split("|") if endpoints_key else [],
                        "state": state["state"],
                        "consecutive_failures": int(state["consecutive_failures"]),
                        "probe_in_flight": bool(state["probe_in_flight"]),
                        "retry_after_seconds": round(
                            max(0.0, float(state["open_until"]) - now),
                            3,
                        ),
                        "last_error": str(state["last_error"] or ""),
                    }
                )
        return {
            "enabled": self.circuit_breaker_enabled,
            "failure_threshold": self.circuit_failure_threshold,
            "cooldown_seconds": self.circuit_cooldown_seconds,
            "circuits": circuits,
            "open_count": sum(1 for item in circuits if item["state"] == "open"),
            "half_open_count": sum(1 for item in circuits if item["state"] == "half_open"),
        }

    @staticmethod
    def _extract_content(data: dict) -> str:
        """Extract assistant text from OpenAI-compatible response variants.

        Some compatible gateways occasionally return text in Responses-style
        fields or omit ``message.content`` for empty assistant outputs. Treat a
        missing text field as an empty response so narrators can fall back to the
        canonical rules panel without turning a harmless shape drift into a hard
        LLM failure.
        """

        if isinstance(data.get("output_text"), str):
            return data["output_text"]

        choices = data.get("choices") or []
        if choices:
            first_choice = choices[0] or {}
            message = first_choice.get("message") or {}
            content = message.get("content")
            extracted = OpenAICompatibleClient._content_to_text(content)
            if extracted:
                return extracted
            extracted = OpenAICompatibleClient._content_to_text(message.get("text"))
            if extracted:
                return extracted
            extracted = OpenAICompatibleClient._content_to_text(first_choice.get("text"))
            if extracted:
                return extracted
            if "content" not in message:
                return ""
            return ""

        output = data.get("output")
        extracted = OpenAICompatibleClient._content_to_text(output)
        if extracted:
            return extracted
        return ""

    @staticmethod
    def _content_to_text(content) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                text = OpenAICompatibleClient._content_to_text(item)
                if text:
                    parts.append(text)
            return "".join(parts)
        if isinstance(content, dict):
            for key in ("text", "content", "output_text"):
                value = content.get(key)
                if isinstance(value, str):
                    return value
            nested = content.get("content")
            if isinstance(nested, list):
                return OpenAICompatibleClient._content_to_text(nested)
        return ""

    def _is_transient_error(self, exc: Exception) -> bool:
        if isinstance(exc, LLMEmptyResponseError):
            return True
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        if self._is_endpoint_model_availability_error(exc):
            return True
        if isinstance(exc, (TimeoutError, error.URLError, http.client.RemoteDisconnected)):
            return True
        text = f"{exc} {getattr(exc, 'body', '')}".lower()
        markers = (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "remote end closed connection",
            "connection closed without response",
            "rate limit",
            "too many requests",
            "upstream error",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_endpoint_model_availability_error(exc: Exception) -> bool:
        """Recognize provider/account-group routing failures without retrying 404s.

        Some compatible gateways return HTTP 404 when one domain's account group
        does not expose the requested model, even though the same model is present
        on a configured backup domain. Only that explicit response shape is an
        endpoint failover condition; an ordinary missing URL remains permanent.
        """

        if getattr(exc, "status_code", None) not in {400, 404}:
            return False
        text = f"{exc} {getattr(exc, 'body', '')}".lower()
        model_marker = "model" in text or "模型" in text
        unsupported_marker = any(
            marker in text
            for marker in (
                "not supported",
                "unsupported model",
                "model_not_found",
                "model not found",
                "不支持该模型",
                "模型不可用",
            )
        )
        account_group_marker = any(
            marker in text
            for marker in (
                "configured account",
                "account group",
                "this group",
                "当前分组",
                "账号组",
            )
        )
        return model_marker and unsupported_marker and account_group_marker

    @staticmethod
    def _is_response_format_compatibility_error(
        exc: Exception,
        *,
        model: str,
        response_format: dict | None,
        already_retried: bool,
    ) -> bool:
        if response_format is None or already_retried or not uses_high_latency_model(model):
            return False
        status_code = getattr(exc, "status_code", None)
        return status_code in {400, 422, 500, 502}

    def _mark_last_call_empty(self) -> None:
        if not self.recent_calls:
            return
        self.recent_calls[-1]["ok"] = False
        self.recent_calls[-1]["response_empty"] = True
        self.recent_calls[-1]["error"] = "LLM gateway returned an empty assistant response"

    def _post_chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float,
        response_format: dict | None,
        max_tokens: int | None,
        endpoint_url: str,
        timeout_seconds: float,
        operation: str,
        attempt: int,
    ) -> dict:
        started = time.monotonic()
        payload = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.thinking_enabled:
            payload["thinking"] = {"type": "enabled"}

        try:
            data = self.transport.post_json(
                url=endpoint_url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": self.config.http_user_agent,
                },
                payload=payload,
                timeout=max(0.1, float(timeout_seconds)),
            )
            self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=True,
                endpoint_url=endpoint_url,
                operation=operation,
                attempt=attempt,
            )
            return data
        except Exception as exc:
            self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=False,
                error=str(exc),
                endpoint_url=endpoint_url,
                operation=operation,
                attempt=attempt,
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
        max_tokens: int | None,
        elapsed_ms: int,
        ok: bool,
        error: str = "",
        endpoint_url: str = "",
        operation: str = "",
        attempt: int = 1,
    ) -> None:
        self.total_calls += 1
        self.call_latency_history_ms.append(max(0, int(elapsed_ms)))
        self.call_latency_history_ms = self.call_latency_history_ms[-5000:]
        if not ok:
            self.failed_call_count += 1
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "message_count": len(messages),
            "prompt_chars": self._messages_char_count(messages),
            "response_format": bool(response_format),
            "max_tokens": max(0, int(max_tokens or 0)),
            "reasoning_effort": bool(self.config.reasoning_effort),
            "thinking_enabled": bool(self.config.thinking_enabled),
            "operation": str(operation or "chat_completion"),
            "attempt": max(1, int(attempt)),
        }
        if endpoint_url:
            record["endpoint"] = endpoint_url
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
        latency_values = sorted(self.call_latency_history_ms)
        return {
            "total_calls": self.total_calls,
            "failed_calls": self.failed_call_count,
            "last_call": last,
            "recent_calls": recent,
            "slowest_recent": slowest,
            "average_recent_elapsed_ms": average,
            "latency": {
                "sample_count": len(latency_values),
                "p50_ms": self._percentile(latency_values, 0.50),
                "p95_ms": self._percentile(latency_values, 0.95),
                "max_ms": max(latency_values, default=0),
            },
            "circuit_breaker": self.circuit_breaker_payload(),
        }

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
        return int(values[index])
