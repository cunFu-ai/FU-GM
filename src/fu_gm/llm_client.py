from __future__ import annotations

import json
import hashlib
import http.client
import os
import re
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib import error, request
from urllib.parse import urlsplit

from fu_gm.components.gm_live_run_monitor import emit_live_run_event
from fu_gm.config import LLMConfig, uses_high_latency_model
from fu_gm.context_compaction import StructuredContextCompactor


class Transport(Protocol):
    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        ...


class UrlLibTransport:
    """兼容系统代理的 JSON 传输，并为直连端点复用 HTTP 连接。"""

    def __init__(self, *, max_idle_connections_per_origin: int = 4) -> None:
        self.max_idle_connections_per_origin = max(
            1,
            int(max_idle_connections_per_origin),
        )
        self._pool_lock = threading.RLock()
        self._idle_connections: dict[
            tuple[str, str, int],
            list[http.client.HTTPConnection],
        ] = {}
        self.connection_open_count = 0
        self.connection_reuse_count = 0

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        body = json.dumps(payload).encode("utf-8")
        parsed = urlsplit(url)
        proxy = request.getproxies().get(parsed.scheme)
        proxy_required = bool(
            proxy
            and parsed.hostname
            and not request.proxy_bypass(parsed.hostname)
        )
        if proxy_required or parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return self._post_with_urlopen(url, headers, body, timeout)
        return self._post_with_pool(parsed, headers, body, timeout)

    @staticmethod
    def _post_with_urlopen(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> dict:
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

    def _post_with_pool(
        self,
        parsed,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> dict:
        scheme = str(parsed.scheme)
        host = str(parsed.hostname)
        port = int(parsed.port or (443 if scheme == "https" else 80))
        key = (scheme, host, port)
        connection = self._take_connection(key, timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request_headers = dict(headers)
        request_headers.setdefault("Content-Length", str(len(body)))
        try:
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            connection.request("POST", path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            reusable = not bool(response.will_close)
            if 200 <= int(response.status) < 300:
                result = json.loads(response_body.decode("utf-8"))
            else:
                raise LLMHTTPError(
                    status_code=int(response.status),
                    body=response_body.decode("utf-8", errors="replace"),
                )
        except Exception:
            connection.close()
            raise
        if reusable:
            self._return_connection(key, connection)
        else:
            connection.close()
        return result

    def _take_connection(
        self,
        key: tuple[str, str, int],
        timeout: float,
    ) -> http.client.HTTPConnection:
        with self._pool_lock:
            idle = self._idle_connections.get(key)
            if idle:
                self.connection_reuse_count += 1
                return idle.pop()
            self.connection_open_count += 1
        scheme, host, port = key
        connection_type = (
            http.client.HTTPSConnection
            if scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(host, port=port, timeout=timeout)

    def _return_connection(
        self,
        key: tuple[str, str, int],
        connection: http.client.HTTPConnection,
    ) -> None:
        with self._pool_lock:
            idle = self._idle_connections.setdefault(key, [])
            if len(idle) < self.max_idle_connections_per_origin:
                idle.append(connection)
                return
        connection.close()

    def close(self) -> None:
        with self._pool_lock:
            connections = [
                connection
                for idle in self._idle_connections.values()
                for connection in idle
            ]
            self._idle_connections.clear()
        for connection in connections:
            connection.close()


@dataclass
class ChatMessage:
    role: str
    content: str
    cache_breakpoint: bool = False
    cache_family: str = ""
    cache_breakpoint_offsets: tuple[int, ...] = ()


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


@dataclass(frozen=True)
class LLMErrorDisposition:
    """供应商错误的统一处置结论。"""

    category: str
    retryable: bool
    failover: bool
    stage_degradable: bool
    status_code: int | None = None


def classify_llm_error(exc: object) -> LLMErrorDisposition:
    """把不同中转站的错误形状归一为稳定类别。

    此函数只决定调用层能否重试或切换端点，不决定游戏规则是否回滚。
    游戏阶段如何降级由各调用者根据 ``stage_degradable`` 决定。
    """

    status_code = getattr(exc, "status_code", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    body = str(getattr(exc, "body", "") or "")
    text = f"{exc} {body}".lower()
    if status_code is None:
        match = re.search(r"(?:llm\s+http|http(?:\s+error)?)\s*[:=]?\s*(\d{3})", text)
        if match:
            status_code = int(match.group(1))

    if isinstance(exc, LLMProviderCircuitOpen) or "provider circuit is open" in text:
        return LLMErrorDisposition("circuit_open", False, False, True, status_code)
    if isinstance(exc, LLMDeadlineExceeded):
        return LLMErrorDisposition("deadline", False, False, True, status_code)
    if isinstance(exc, LLMEmptyResponseError):
        return LLMErrorDisposition("empty_response", True, True, True, status_code)
    if status_code == 401:
        return LLMErrorDisposition("authentication", False, False, False, status_code)
    if status_code == 403:
        if any(
            marker in text
            for marker in (
                "user_inactive",
                "account inactive",
                "account disabled",
                "余额不足",
                "insufficient balance",
                "insufficient_balance",
                "billing_error",
                "账户停用",
                "用户已停用",
            )
        ):
            return LLMErrorDisposition("account_inactive", False, False, False, status_code)
        if any(
            marker in text
            for marker in (
                "content_policy",
                "content filter",
                "content_filter",
                "safety policy",
                "moderation",
                "内容审核",
                "安全策略",
            )
        ):
            return LLMErrorDisposition("content_policy", False, False, True, status_code)
        return LLMErrorDisposition("forbidden", False, False, False, status_code)
    if status_code == 429:
        return LLMErrorDisposition("rate_limit", True, True, True, status_code)
    if status_code == 413 or any(
        marker in text
        for marker in (
            "prompt_too_long",
            "context_length_exceeded",
            "maximum context",
            "context length",
            "request_too_large",
            "token limit",
            "input is too long",
        )
    ):
        return LLMErrorDisposition("context_limit", True, False, True, status_code)
    if status_code in {408, 409, 425}:
        return LLMErrorDisposition("transient_request", True, True, True, status_code)
    if status_code in {500, 502, 503, 504}:
        return LLMErrorDisposition("upstream", True, True, True, status_code)
    if status_code in {400, 404}:
        model_marker = "model" in text or "模型" in text
        unavailable_marker = any(
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
        group_marker = any(
            marker in text
            for marker in (
                "configured account",
                "account group",
                "this group",
                "当前分组",
                "账号组",
            )
        )
        if model_marker and unavailable_marker and group_marker:
            return LLMErrorDisposition("endpoint_model_unavailable", True, True, True, status_code)
        return LLMErrorDisposition("invalid_request", False, False, False, status_code)
    if isinstance(
        exc,
        (
            TimeoutError,
            error.URLError,
            http.client.RemoteDisconnected,
            ConnectionError,
        ),
    ) or any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "remote end closed connection",
            "connection closed without response",
            "upstream error",
            "超时",
            "截止时间",
            "共享截止",
        )
    ):
        return LLMErrorDisposition("transport", True, True, True, status_code)
    return LLMErrorDisposition("unknown", False, False, False, status_code)


class OpenAICompatibleClient:
    def __init__(
        self,
        config: LLMConfig,
        transport: Transport | None = None,
        *,
        circuit_breaker_enabled: bool = False,
        circuit_failure_threshold: int = 1,
        circuit_cooldown_seconds: float = 30.0,
        circuit_max_cooldown_seconds: float = 300.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()
        self.last_recovery_attempts: list[LLMRecoveryAttempt] = []
        self.recent_calls: list[dict] = []
        self.call_latency_history_ms: list[int] = []
        self.failed_call_count = 0
        self.total_calls = 0
        self.prompt_token_total = 0
        self.cached_token_total = 0
        self.cache_miss_token_total = 0
        self.cache_miss_token_reported_calls = 0
        self.cache_write_token_total = 0
        self.cache_eligible_calls = 0
        self.cache_eligible_prompt_token_total = 0
        self.cache_eligible_cached_token_total = 0
        self.cache_usage_reported_calls = 0
        self.cache_usage_reported_prompt_token_total = 0
        self.cache_hit_calls = 0
        self.cache_known_miss_calls = 0
        self.cache_hit_latency_history_ms: list[int] = []
        self.cache_miss_latency_history_ms: list[int] = []
        self._prompt_cache_capabilities: dict[tuple[str, str], str] = {}
        self._prompt_cache_family_stats: dict[str, dict[str, object]] = {}
        self._prompt_cache_operation_stats: dict[str, dict[str, object]] = {}
        self._prompt_cache_lock = threading.RLock()
        self._telemetry_lock = threading.RLock()
        self._context_compactor = StructuredContextCompactor()
        self.circuit_breaker_enabled = bool(circuit_breaker_enabled)
        self.circuit_failure_threshold = max(1, int(circuit_failure_threshold))
        self.circuit_cooldown_seconds = max(0.1, float(circuit_cooldown_seconds))
        self.circuit_max_cooldown_seconds = max(
            self.circuit_cooldown_seconds,
            float(circuit_max_cooldown_seconds),
        )
        self._monotonic = monotonic or time.monotonic
        self._circuit_lock = threading.RLock()
        self._circuit_states: dict[tuple[str, str], dict[str, object]] = {}
        self._provider_log_lock = threading.RLock()
        self._provider_failure_active = False
        self._call_diagnostics: ContextVar[dict[str, object] | None] = (
            ContextVar(
                f"fu_gm_llm_call_diagnostics_{id(self)}",
                default=None,
            )
        )

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
        thinking_enabled: bool | None = None,
        max_recovery_retries: int | None = None,
        retry_without_response_format_on_empty: bool = False,
    ) -> str:
        self._call_diagnostics.set(None)
        if os.environ.get(
            "FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}:
            raise RuntimeError(
                "当前测试进程已禁止外部 LLM 传输；请使用显式注入的 test_only 客户端。"
            )
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
        max_retries = max(
            0,
            int(
                self.config.reactive_recovery_max_retries
                if max_recovery_retries is None
                else max_recovery_retries
            ),
        )
        attempted_endpoints: set[str] = set()
        last_circuit_failure = False
        attempt = 0
        cache_fallbacks = 0
        recovery_codes: list[str] = []
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
                if self.config.endpoint_attempt_timeout_seconds > 0:
                    attempt_timeout = min(
                        attempt_timeout,
                        float(self.config.endpoint_attempt_timeout_seconds),
                    )
                data, call_record = self._post_chat_completion(
                    model=model,
                    messages=current_messages,
                    temperature=temperature,
                    response_format=current_response_format,
                    max_tokens=max_tokens,
                    endpoint_url=endpoint_url,
                    timeout_seconds=attempt_timeout,
                    operation=operation,
                    attempt=attempt + cache_fallbacks + 1,
                    thinking_enabled=thinking_enabled,
                )
                content = self._extract_content(data)
                if not allow_empty and not content.strip():
                    self._mark_call_empty(call_record)
                    raise LLMEmptyResponseError("LLM gateway returned an empty assistant response")
                emit_live_run_event(
                    "provider_assistant_content",
                    phase="provider_response_received",
                    attempt=int(call_record.get("attempt") or 1),
                    summary="供应商已返回完整 assistant 正文。",
                    public_details={
                        "model": str(model or ""),
                        "operation": str(operation or "chat_completion"),
                        "response_chars": len(content),
                        "reasoning_chars": int(
                            call_record.get("reasoning_chars") or 0
                        ),
                        "finish_reason": str(
                            call_record.get("finish_reason") or ""
                        ),
                        "provider_response_id": str(
                            call_record.get("provider_response_id") or ""
                        ),
                        "elapsed_ms": int(call_record.get("elapsed_ms") or 0),
                    },
                    private_details={"assistant_output": content},
                )
                self._record_circuit_success(model=model, endpoint_urls=endpoint_urls)
                if recovery_codes:
                    self._call_diagnostics.set(
                        {
                            "recovered": True,
                            "recovery_codes": list(dict.fromkeys(recovery_codes)),
                            "attempt_count": max(
                                1,
                                int(call_record.get("attempt") or 1),
                            ),
                        }
                    )
                return content
            except Exception as exc:
                if isinstance(exc, (LLMDeadlineExceeded, LLMProviderCircuitOpen)):
                    self._release_half_open_probe(model=model, endpoint_urls=endpoint_urls)
                    raise
                disposition = classify_llm_error(exc)
                context_error = self._is_recoverable_context_error(exc)
                transient_error = disposition.retryable
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
                empty_response_format_retry = bool(
                    retry_without_response_format_on_empty
                    and disposition.category == "empty_response"
                    and current_response_format is not None
                    and not response_format_fallback_used
                )
                cache_compatibility_error = self._is_prompt_cache_compatibility_error(exc)
                if cache_compatibility_error and cache_fallbacks < 3:
                    downgraded = self._downgrade_prompt_cache_capability(
                        endpoint_url=endpoint_url,
                        model=model,
                    )
                    if downgraded:
                        cache_fallbacks += 1
                        chars = self._messages_char_count(current_messages)
                        self.last_recovery_attempts.append(
                            LLMRecoveryAttempt(
                                reason=(
                                    "提示词缓存协议不兼容，已在当前端点自动降级为 "
                                    f"{downgraded}"
                                ),
                                original_chars=chars,
                                retry_chars=chars,
                                attempt=cache_fallbacks,
                            )
                        )
                        continue
                if (
                    not self.config.reactive_recovery_enabled
                    or attempt >= max_retries
                    or not (
                        context_error
                        or transient_error
                        or response_format_error
                        or empty_response_format_retry
                    )
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
                    recovery_codes.append("CONTEXT_COMPACTED")
                    current_messages = self._compact_messages_for_retry(
                        current_messages,
                        reason=str(exc),
                        target_chars=max(4000, int(self.config.reactive_recovery_target_chars)),
                    )
                elif response_format_error or empty_response_format_retry:
                    if empty_response_format_retry:
                        recovery_codes.append("EMPTY_RESPONSE_RECOVERED")
                    recovery_codes.append("RESPONSE_FORMAT_DOWNGRADED")
                    # A gateway can reject structured output, while DeepSeek's
                    # documented JSON mode can also return a successful response
                    # with empty final content.  Callers that opt in already ask
                    # for one JSON object and validate it, so exactly one plain
                    # completion is safer than repeating the same failing shape.
                    current_response_format = None
                    response_format_fallback_used = True
                else:
                    recovery_codes.append(
                        "EMPTY_RESPONSE_RECOVERED"
                        if disposition.category == "empty_response"
                        else "PROVIDER_RECOVERED"
                    )
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
                            f"{exc}；已移除 response_format 进行一次有界重试"
                            if response_format_error or empty_response_format_retry
                            else str(exc)
                        ),
                        original_chars=original_chars,
                        retry_chars=self._messages_char_count(current_messages),
                        attempt=attempt,
                    )
                )

    def consume_call_diagnostics(self) -> dict[str, object]:
        """Return this execution context's last successful recovery summary.

        The configured client is shared across campaigns and HTTP worker
        threads.  A ``ContextVar`` keeps this handoff local to the caller that
        made the completion request, unlike process-wide telemetry such as
        ``last_recovery_attempts``.
        """

        payload = dict(self._call_diagnostics.get() or {})
        self._call_diagnostics.set(None)
        return payload

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
                self._log_circuit_blocked(
                    model=model,
                    endpoint_urls=endpoint_urls,
                    retry_after_seconds=retry_after,
                )
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
                cooldown = min(
                    self.circuit_max_cooldown_seconds,
                    self.circuit_cooldown_seconds * (2 ** max(0, failures - 1)),
                )
                state["state"] = "open"
                state["opened_at"] = now
                state["open_until"] = now + cooldown

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
            "max_cooldown_seconds": self.circuit_max_cooldown_seconds,
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

    @classmethod
    def _extract_response_metadata(
        cls,
        data: dict | None,
    ) -> dict[str, object]:
        """Return safe response-shape diagnostics without storing model text."""

        if not isinstance(data, dict):
            return {}
        choices = data.get("choices")
        choice_list = choices if isinstance(choices, list) else []
        first_choice = choice_list[0] if choice_list else {}
        if not isinstance(first_choice, dict):
            first_choice = {}
        message = first_choice.get("message")
        if not isinstance(message, dict):
            message = {}
        response_id = str(data.get("id") or "").strip()[:200]
        finish_reason = str(
            first_choice.get("finish_reason")
            or data.get("finish_reason")
            or data.get("stop_reason")
            or ""
        ).strip()[:120]
        final_content = cls._extract_content(data)
        reasoning_content = cls._content_to_text(
            message.get("reasoning_content")
        )
        return {
            "provider_response_id": response_id,
            "finish_reason": finish_reason,
            "choice_count": len(choice_list),
            "response_chars": len(final_content),
            "reasoning_chars": len(reasoning_content),
        }

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
        return classify_llm_error(exc).retryable

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

    @staticmethod
    def _requires_explicit_non_thinking(
        *,
        endpoint_url: str,
        model: str,
    ) -> bool:
        """Return whether an endpoint defaults this model to thinking mode.

        DeepSeek V4 and Xiaomi MiMo 2.5 enable thinking by default. Omitting
        the field therefore does not mean the same thing as
        ``thinking_enabled=False`` and can use the entire completion budget
        for hidden reasoning.
        """

        endpoint = str(endpoint_url or "").lower()
        normalized_model = str(model or "").strip().lower()
        deepseek_v4 = (
            "api.deepseek.com" in endpoint
            and normalized_model.startswith("deepseek-v4")
        )
        xiaomi_mimo_25 = (
            "api.xiaomimimo.com" in endpoint
            and normalized_model in {"mimo-v2.5", "mimo-v2.5-pro"}
        )
        return bool(deepseek_v4 or xiaomi_mimo_25)

    @staticmethod
    def _max_output_token_field(*, endpoint_url: str) -> str:
        """Return the provider's OpenAI-compatible output budget field."""

        endpoint = str(endpoint_url or "").lower()
        if "api.xiaomimimo.com" in endpoint:
            return "max_completion_tokens"
        return "max_tokens"

    def _mark_call_empty(self, record: dict[str, object]) -> None:
        """把空响应记到产生它的调用，避免并发时误改另一条记录。"""

        with self._telemetry_lock:
            if bool(record.get("response_empty")):
                return
            if bool(record.get("ok")):
                self.failed_call_count += 1
            record["ok"] = False
            record["response_empty"] = True
            record["error"] = "LLM gateway returned an empty assistant response"
            record["error_category"] = "empty_response"
            self._log_provider_call(record)
        emit_live_run_event(
            "provider_empty_response",
            phase="provider_recovery",
            attempt=max(1, int(record.get("attempt") or 1)),
            summary="供应商返回成功响应，但没有最终 assistant 正文。",
            public_details={
                "model": str(record.get("model") or ""),
                "operation": str(record.get("operation") or "chat_completion"),
                "finish_reason": str(record.get("finish_reason") or ""),
                "response_chars": int(record.get("response_chars") or 0),
                "reasoning_chars": int(record.get("reasoning_chars") or 0),
                "provider_response_id": str(
                    record.get("provider_response_id") or ""
                ),
                "usage": dict(record.get("usage") or {}),
            },
        )

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
        thinking_enabled: bool | None,
    ) -> tuple[dict, dict[str, object]]:
        started = time.monotonic()
        cache_metadata = self._prompt_cache_request_metadata(
            endpoint_url=endpoint_url,
            model=model,
            messages=messages,
            operation=operation,
        )
        payload = {
            "model": model,
            "messages": [
                self._serialize_message(message, cache_mode=cache_metadata["mode"])
                for message in messages
            ],
            "temperature": temperature,
        }
        effective_thinking_enabled = (
            bool(self.config.thinking_enabled)
            if thinking_enabled is None
            else bool(thinking_enabled)
        )
        if cache_metadata["key"]:
            payload["prompt_cache_key"] = cache_metadata["key"]
        if cache_metadata["mode"] == "explicit":
            payload["prompt_cache_options"] = {
                "mode": "explicit",
                "ttl": self._normalized_prompt_cache_ttl(),
            }
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload[
                self._max_output_token_field(endpoint_url=endpoint_url)
            ] = max(1, int(max_tokens))
        if self.config.reasoning_effort and effective_thinking_enabled:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if effective_thinking_enabled:
            payload["thinking"] = {"type": "enabled"}
        elif self._requires_explicit_non_thinking(
            endpoint_url=endpoint_url,
            model=model,
        ):
            payload["thinking"] = {"type": "disabled"}

        parsed_endpoint = urlsplit(endpoint_url)
        safe_endpoint = (
            f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}"
            f"{parsed_endpoint.path}"
        )
        emit_live_run_event(
            "provider_attempt_started",
            phase="provider_attempt",
            attempt=attempt,
            summary="供应商请求已发出，正在等待网络响应。",
            public_details={
                "model": str(model or ""),
                "operation": str(operation or "chat_completion"),
                "attempt": attempt,
                "endpoint": safe_endpoint,
                "timeout_seconds": max(0.1, float(timeout_seconds)),
                "input_message_count": len(messages),
                "input_chars": self._messages_char_count(messages),
                "response_format": bool(response_format),
                "max_tokens": max(0, int(max_tokens or 0)),
            },
        )
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
            record = self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=True,
                endpoint_url=endpoint_url,
                operation=operation,
                attempt=attempt,
                response_data=data,
                cache_metadata=cache_metadata,
                thinking_enabled=effective_thinking_enabled,
            )
            self._remember_prompt_cache_capability(
                endpoint_url=endpoint_url,
                model=model,
                mode=str(cache_metadata["mode"]),
            )
            emit_live_run_event(
                "provider_attempt_finished",
                phase="provider_response_received",
                attempt=attempt,
                summary="供应商请求成功返回。",
                public_details={
                    "model": str(model or ""),
                    "operation": str(operation or "chat_completion"),
                    "attempt": attempt,
                    "endpoint": safe_endpoint,
                    "ok": True,
                    "elapsed_ms": int(record.get("elapsed_ms") or 0),
                    "finish_reason": str(record.get("finish_reason") or ""),
                    "response_chars": int(record.get("response_chars") or 0),
                    "reasoning_chars": int(record.get("reasoning_chars") or 0),
                    "provider_response_id": str(
                        record.get("provider_response_id") or ""
                    ),
                    "usage": dict(record.get("usage") or {}),
                },
            )
            return data, record
        except Exception as exc:
            disposition = classify_llm_error(exc)
            call_record = self._record_call(
                model=model,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                ok=False,
                error=str(exc),
                error_category=disposition.category,
                endpoint_url=endpoint_url,
                operation=operation,
                attempt=attempt,
                cache_metadata=cache_metadata,
                thinking_enabled=effective_thinking_enabled,
            )
            emit_live_run_event(
                "provider_attempt_finished",
                phase="provider_recovery",
                attempt=attempt,
                summary="供应商请求失败，正在由有界恢复策略处理。",
                public_details={
                    "model": str(model or ""),
                    "operation": str(operation or "chat_completion"),
                    "attempt": attempt,
                    "endpoint": safe_endpoint,
                    "ok": False,
                    "elapsed_ms": int(call_record.get("elapsed_ms") or 0),
                    "error_type": type(exc).__name__,
                    "error_category": disposition.category,
                    "status_code": int(disposition.status_code or 0),
                },
            )
            raise

    @staticmethod
    def _supports_explicit_prompt_cache(model: str) -> bool:
        normalized = str(model or "").strip().lower()
        match = re.search(r"gpt-(\d+)\.(\d+)", normalized)
        if not match:
            return False
        return (int(match.group(1)), int(match.group(2))) >= (5, 6)

    def _initial_prompt_cache_mode(self, model: str) -> str:
        configured = str(self.config.prompt_cache_mode or "auto").strip().lower()
        if not self.config.prompt_cache_enabled or configured in {"off", "disabled", "none"}:
            return "off"
        if configured in {"explicit", "breakpoint", "key"}:
            return configured
        return "explicit" if self._supports_explicit_prompt_cache(model) else "key"

    def _prompt_cache_request_metadata(
        self,
        *,
        endpoint_url: str,
        model: str,
        messages: list[ChatMessage],
        operation: str,
    ) -> dict[str, object]:
        breakpoints = self._cache_breakpoint_locations(messages)
        if not breakpoints:
            return {
                "eligible": False,
                "mode": "off",
                "key": "",
                "base_fingerprint": "",
                "prefix_fingerprint": "",
                "breakpoint_count": 0,
                "family": "",
                "operation": str(operation or "chat_completion"),
            }

        capability_key = (str(endpoint_url), str(model))
        with self._prompt_cache_lock:
            mode = self._prompt_cache_capabilities.get(capability_key)
        if not mode:
            mode = self._initial_prompt_cache_mode(model)

        first_index, first_offset = breakpoints[0]
        last_index, last_offset = breakpoints[-1]
        base_fingerprint = self._message_prefix_fingerprint_at(
            messages,
            message_index=first_index,
            content_offset=first_offset,
        )
        prefix_fingerprint = self._message_prefix_fingerprint_at(
            messages,
            message_index=last_index,
            content_offset=last_offset,
        )
        family = str(messages[first_index].cache_family or "system")
        key = ""
        if mode != "off":
            namespace = re.sub(
                r"[^a-zA-Z0-9_.:-]+",
                "-",
                str(self.config.prompt_cache_key_prefix or "fugm"),
            ).strip("-:") or "fugm"
            clean_family = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", family).strip("-:") or "system"
            key = f"{namespace}:v1:{clean_family[:20]}:{base_fingerprint[:16]}"[:64]
        return {
            "eligible": mode != "off",
            "mode": mode,
            "key": key,
            "base_fingerprint": base_fingerprint,
            "prefix_fingerprint": prefix_fingerprint,
            "breakpoint_count": len(breakpoints),
            "family": family,
            "operation": str(operation or "chat_completion"),
        }

    @staticmethod
    def _message_prefix_fingerprint_at(
        messages: list[ChatMessage],
        *,
        message_index: int,
        content_offset: int,
    ) -> str:
        prefix_messages = [
            {
                "role": message.role,
                "content": (
                    message.content[:content_offset]
                    if index == message_index
                    else message.content
                ),
            }
            for index, message in enumerate(messages[: message_index + 1])
        ]
        rendered = json.dumps(
            prefix_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _cache_breakpoint_locations(
        messages: list[ChatMessage],
    ) -> list[tuple[int, int]]:
        locations: list[tuple[int, int]] = []
        for index, message in enumerate(messages):
            offsets = {
                max(0, min(len(message.content), int(offset)))
                for offset in message.cache_breakpoint_offsets
                if int(offset) > 0
            }
            if message.cache_breakpoint and not offsets:
                offsets.add(len(message.content))
            locations.extend((index, offset) for offset in sorted(offsets))
        return locations

    @staticmethod
    def _serialize_message(message: ChatMessage, *, cache_mode: object) -> dict[str, object]:
        content: object = message.content
        if str(cache_mode) in {"explicit", "breakpoint"}:
            offsets = {
                max(0, min(len(message.content), int(offset)))
                for offset in message.cache_breakpoint_offsets
                if int(offset) > 0
            }
            if message.cache_breakpoint and not offsets:
                offsets.add(len(message.content))
            if offsets:
                blocks: list[dict[str, object]] = []
                start = 0
                for offset in sorted(offsets):
                    if offset <= start:
                        continue
                    blocks.append(
                        {
                            "type": "text",
                            "text": message.content[start:offset],
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    )
                    start = offset
                if start < len(message.content):
                    blocks.append({"type": "text", "text": message.content[start:]})
                content = blocks
        return {"role": message.role, "content": content}

    def _normalized_prompt_cache_ttl(self) -> str:
        return "30m" if str(self.config.prompt_cache_ttl or "").strip() != "30m" else "30m"

    @staticmethod
    def _is_prompt_cache_compatibility_error(exc: Exception) -> bool:
        if getattr(exc, "status_code", None) not in {400, 422}:
            return False
        text = f"{exc} {getattr(exc, 'body', '')}".lower()
        return any(
            marker in text
            for marker in (
                "prompt_cache_options",
                "prompt_cache_breakpoint",
                "prompt_cache_key",
                "unsupported content type",
                "content must be a string",
                "messages.content",
            )
        )

    def _downgrade_prompt_cache_capability(self, *, endpoint_url: str, model: str) -> str:
        capability_key = (str(endpoint_url), str(model))
        with self._prompt_cache_lock:
            current = self._prompt_cache_capabilities.get(capability_key)
            if not current:
                current = self._initial_prompt_cache_mode(model)
            next_mode = {
                "explicit": "breakpoint",
                "breakpoint": "key",
                "key": "off",
            }.get(current, "")
            if next_mode:
                self._prompt_cache_capabilities[capability_key] = next_mode
            return next_mode

    def _remember_prompt_cache_capability(
        self,
        *,
        endpoint_url: str,
        model: str,
        mode: str,
    ) -> None:
        if mode == "off":
            return
        with self._prompt_cache_lock:
            self._prompt_cache_capabilities[(str(endpoint_url), str(model))] = mode

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
        structured = self._context_compactor.compact(text, max_chars=max_chars)
        if structured.strategy not in {"not-json", "json-too-large"}:
            return structured.text
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

    def _record_call(self, **record_fields: object) -> dict[str, object]:
        """串行更新共享遥测，但不串行化实际网络请求。"""

        with self._telemetry_lock:
            return self._record_call_unlocked(**record_fields)

    def _record_call_unlocked(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        response_format: dict | None,
        max_tokens: int | None,
        elapsed_ms: int,
        ok: bool,
        error: str = "",
        error_category: str = "",
        endpoint_url: str = "",
        operation: str = "",
        attempt: int = 1,
        response_data: dict | None = None,
        cache_metadata: dict[str, object] | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, object]:
        self.total_calls += 1
        self.call_latency_history_ms.append(max(0, int(elapsed_ms)))
        self.call_latency_history_ms = self.call_latency_history_ms[-5000:]
        if not ok:
            self.failed_call_count += 1
        usage = self._extract_usage(response_data)
        cache_metadata = dict(cache_metadata or {})
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "message_count": len(messages),
            "prompt_chars": self._messages_char_count(messages),
            "response_format": bool(response_format),
            "max_tokens": max(0, int(max_tokens or 0)),
            "reasoning_effort": bool(
                self.config.reasoning_effort
                and (
                    bool(self.config.thinking_enabled)
                    if thinking_enabled is None
                    else bool(thinking_enabled)
                )
            ),
            "thinking_enabled": (
                bool(self.config.thinking_enabled)
                if thinking_enabled is None
                else bool(thinking_enabled)
            ),
            "operation": str(operation or "chat_completion"),
            "attempt": max(1, int(attempt)),
            "prompt_cache": {
                "eligible": bool(cache_metadata.get("eligible")),
                "mode": str(cache_metadata.get("mode") or "off"),
                "key": str(cache_metadata.get("key") or ""),
                "family": str(cache_metadata.get("family") or ""),
                "base_fingerprint": str(cache_metadata.get("base_fingerprint") or ""),
                "prefix_fingerprint": str(cache_metadata.get("prefix_fingerprint") or ""),
                "breakpoint_count": max(
                    0,
                    int(cache_metadata.get("breakpoint_count") or 0),
                ),
            },
        }
        record.update(self._extract_response_metadata(response_data))
        if usage:
            record["usage"] = usage
            prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
            self.prompt_token_total += prompt_tokens
            if bool(cache_metadata.get("eligible")):
                self.cache_eligible_prompt_token_total += prompt_tokens
            if usage.get("cache_usage_reported"):
                cached_tokens = max(0, int(usage.get("cached_tokens") or 0))
                cache_miss_tokens = max(
                    0,
                    int(usage.get("cache_miss_tokens") or 0),
                )
                cache_write_tokens = max(0, int(usage.get("cache_write_tokens") or 0))
                self.cached_token_total += cached_tokens
                if "cache_miss_tokens" in usage:
                    self.cache_miss_token_total += cache_miss_tokens
                    self.cache_miss_token_reported_calls += 1
                self.cache_write_token_total += cache_write_tokens
                if bool(cache_metadata.get("eligible")):
                    self.cache_eligible_cached_token_total += cached_tokens
                self.cache_usage_reported_calls += 1
                self.cache_usage_reported_prompt_token_total += prompt_tokens
                if cached_tokens > 0:
                    self.cache_hit_calls += 1
                    self.cache_hit_latency_history_ms.append(max(0, int(elapsed_ms)))
                    self.cache_hit_latency_history_ms = self.cache_hit_latency_history_ms[-5000:]
                else:
                    self.cache_known_miss_calls += 1
                    self.cache_miss_latency_history_ms.append(max(0, int(elapsed_ms)))
                    self.cache_miss_latency_history_ms = self.cache_miss_latency_history_ms[-5000:]
        if bool(cache_metadata.get("eligible")):
            self.cache_eligible_calls += 1
        if endpoint_url:
            record["endpoint"] = endpoint_url
        if error:
            record["error"] = error[:500]
        if error_category:
            record["error_category"] = str(error_category)
        self._record_prompt_cache_breakdown(record)
        self.recent_calls.append(record)
        self.recent_calls = self.recent_calls[-50:]
        self._log_provider_call(record)
        return record

    @staticmethod
    def _log_field(value: object, *, limit: int = 500) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > limit:
            return f"{text[:limit]}..."
        return text

    def _log_provider_call(self, record: dict[str, object]) -> None:
        ok = bool(record.get("ok"))
        with self._provider_log_lock:
            if not ok:
                self._provider_failure_active = True
                endpoint = self._log_field(record.get("endpoint")).split("?", 1)[0]
                print(
                    "[FU-GM LLM] FAILED"
                    f" model={self._log_field(record.get('model'))}"
                    f" endpoint={endpoint}"
                    f" operation={self._log_field(record.get('operation'))}"
                    f" attempt={max(1, int(record.get('attempt') or 1))}"
                    f" elapsed_ms={max(0, int(record.get('elapsed_ms') or 0))}"
                    f" error={self._log_field(record.get('error'))}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if not self._provider_failure_active:
                return
            self._provider_failure_active = False
            endpoint = self._log_field(record.get("endpoint")).split("?", 1)[0]
            print(
                "[FU-GM LLM] RECOVERED"
                f" model={self._log_field(record.get('model'))}"
                f" endpoint={endpoint}"
                f" operation={self._log_field(record.get('operation'))}"
                f" elapsed_ms={max(0, int(record.get('elapsed_ms') or 0))}",
                file=sys.stderr,
                flush=True,
            )

    def _log_circuit_blocked(
        self,
        *,
        model: str,
        endpoint_urls: tuple[str, ...],
        retry_after_seconds: float,
    ) -> None:
        endpoints = ",".join(
            self._log_field(endpoint).split("?", 1)[0]
            for endpoint in endpoint_urls
        )
        print(
            "[FU-GM LLM] CIRCUIT_OPEN"
            f" model={self._log_field(model)}"
            f" endpoints={endpoints}"
            f" retry_after_seconds={max(0.0, float(retry_after_seconds)):.1f}",
            file=sys.stderr,
            flush=True,
        )

    def _record_prompt_cache_breakdown(self, record: dict[str, object]) -> None:
        cache = record.get("prompt_cache")
        cache = cache if isinstance(cache, dict) else {}
        usage = record.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        family = str(cache.get("family") or "unmarked")
        operation = str(record.get("operation") or "chat_completion")
        prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
        cached_tokens = max(0, int(usage.get("cached_tokens") or 0))
        cache_miss_tokens = max(0, int(usage.get("cache_miss_tokens") or 0))
        write_tokens = max(0, int(usage.get("cache_write_tokens") or 0))
        miss_tokens_reported = "cache_miss_tokens" in usage
        eligible = bool(cache.get("eligible"))
        reported = bool(usage.get("cache_usage_reported"))
        hit = reported and cached_tokens > 0
        known_miss = reported and cached_tokens <= 0
        elapsed_ms = max(0, int(record.get("elapsed_ms") or 0))

        with self._prompt_cache_lock:
            for stats, key in (
                (self._prompt_cache_family_stats, family),
                (self._prompt_cache_operation_stats, operation),
            ):
                bucket = stats.setdefault(
                    key,
                    {
                        "calls": 0,
                        "successful_calls": 0,
                        "failed_calls": 0,
                        "eligible_calls": 0,
                        "usage_reported_calls": 0,
                        "hit_calls": 0,
                        "known_miss_calls": 0,
                        "prompt_tokens": 0,
                        "eligible_prompt_tokens": 0,
                        "eligible_cached_tokens": 0,
                        "reported_prompt_tokens": 0,
                        "cached_tokens": 0,
                        "cache_miss_tokens": 0,
                        "cache_miss_tokens_reported_calls": 0,
                        "cache_write_tokens": 0,
                        "latencies_ms": [],
                        "hit_latencies_ms": [],
                        "miss_latencies_ms": [],
                        "cache_keys": set(),
                        "base_fingerprints": set(),
                        "prefix_fingerprints": set(),
                    },
                )
                bucket["calls"] = int(bucket["calls"]) + 1
                outcome_key = "successful_calls" if bool(record.get("ok")) else "failed_calls"
                bucket[outcome_key] = int(bucket[outcome_key]) + 1
                bucket["prompt_tokens"] = int(bucket["prompt_tokens"]) + prompt_tokens
                bucket["cached_tokens"] = int(bucket["cached_tokens"]) + cached_tokens
                if miss_tokens_reported:
                    bucket["cache_miss_tokens"] = (
                        int(bucket["cache_miss_tokens"]) + cache_miss_tokens
                    )
                    bucket["cache_miss_tokens_reported_calls"] = (
                        int(bucket["cache_miss_tokens_reported_calls"]) + 1
                    )
                bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + write_tokens
                if eligible:
                    bucket["eligible_calls"] = int(bucket["eligible_calls"]) + 1
                    bucket["eligible_prompt_tokens"] = (
                        int(bucket["eligible_prompt_tokens"]) + prompt_tokens
                    )
                    bucket["eligible_cached_tokens"] = (
                        int(bucket["eligible_cached_tokens"]) + cached_tokens
                    )
                if reported:
                    bucket["usage_reported_calls"] = (
                        int(bucket["usage_reported_calls"]) + 1
                    )
                    bucket["reported_prompt_tokens"] = (
                        int(bucket["reported_prompt_tokens"]) + prompt_tokens
                    )
                if hit:
                    bucket["hit_calls"] = int(bucket["hit_calls"]) + 1
                    cast_hit_latencies = bucket["hit_latencies_ms"]
                    if isinstance(cast_hit_latencies, list):
                        cast_hit_latencies.append(elapsed_ms)
                if known_miss:
                    bucket["known_miss_calls"] = int(bucket["known_miss_calls"]) + 1
                    cast_miss_latencies = bucket["miss_latencies_ms"]
                    if isinstance(cast_miss_latencies, list):
                        cast_miss_latencies.append(elapsed_ms)
                cast_latencies = bucket["latencies_ms"]
                if isinstance(cast_latencies, list):
                    cast_latencies.append(elapsed_ms)
                for field, value in (
                    ("cache_keys", cache.get("key")),
                    ("base_fingerprints", cache.get("base_fingerprint")),
                    ("prefix_fingerprints", cache.get("prefix_fingerprint")),
                ):
                    collection = bucket[field]
                    clean = str(value or "")
                    if clean and isinstance(collection, set):
                        collection.add(clean)

    @staticmethod
    def _extract_usage(response_data: dict | None) -> dict[str, object]:
        if not isinstance(response_data, dict):
            return {}
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            return {}
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = usage.get("input_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        completion_details = usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = usage.get("output_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}

        cached_present = (
            "cached_tokens" in prompt_details
            or "cached_tokens" in usage
            or "prompt_cache_hit_tokens" in usage
        )
        miss_present = "prompt_cache_miss_tokens" in usage
        write_present = "cache_write_tokens" in prompt_details or "cache_write_tokens" in usage
        result: dict[str, object] = {
            "prompt_tokens": max(
                0,
                int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            ),
            "completion_tokens": max(
                0,
                int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            ),
            "total_tokens": max(0, int(usage.get("total_tokens") or 0)),
            "cache_usage_reported": bool(cached_present or miss_present or write_present),
        }
        if cached_present:
            result["cached_tokens"] = max(
                0,
                int(
                    prompt_details.get(
                        "cached_tokens",
                        usage.get(
                            "cached_tokens",
                            usage.get("prompt_cache_hit_tokens", 0),
                        ),
                    )
                    or 0
                ),
            )
        if miss_present:
            result["cache_miss_tokens"] = max(
                0,
                int(usage.get("prompt_cache_miss_tokens") or 0),
            )
        if write_present:
            result["cache_write_tokens"] = max(
                0,
                int(
                    prompt_details.get(
                        "cache_write_tokens",
                        usage.get("cache_write_tokens", 0),
                    )
                    or 0
                ),
            )
        if "reasoning_tokens" in completion_details:
            result["reasoning_tokens"] = max(
                0,
                int(completion_details.get("reasoning_tokens") or 0),
            )
        return result

    def _prompt_cache_breakdown_payload(
        self,
        stats: dict[str, dict[str, object]],
        *,
        label: str,
    ) -> list[dict[str, object]]:
        with self._prompt_cache_lock:
            snapshots = [(name, dict(bucket)) for name, bucket in stats.items()]

        rows: list[dict[str, object]] = []
        for name, bucket in snapshots:
            prompt_tokens = max(0, int(bucket.get("prompt_tokens") or 0))
            eligible_prompt_tokens = max(
                0,
                int(bucket.get("eligible_prompt_tokens") or 0),
            )
            eligible_cached_tokens = max(
                0,
                int(bucket.get("eligible_cached_tokens") or 0),
            )
            reported_prompt_tokens = max(
                0,
                int(bucket.get("reported_prompt_tokens") or 0),
            )
            cached_tokens = max(0, int(bucket.get("cached_tokens") or 0))
            calls = max(0, int(bucket.get("calls") or 0))
            usage_reported_calls = max(
                0,
                int(bucket.get("usage_reported_calls") or 0),
            )
            unknown_calls = max(0, calls - usage_reported_calls)
            usage_status = self._prompt_cache_usage_status(
                calls=calls,
                usage_reported_calls=usage_reported_calls,
            )
            latencies = sorted(
                max(0, int(value))
                for value in list(bucket.get("latencies_ms") or [])
            )
            hit_latencies = sorted(
                max(0, int(value))
                for value in list(bucket.get("hit_latencies_ms") or [])
            )
            miss_latencies = sorted(
                max(0, int(value))
                for value in list(bucket.get("miss_latencies_ms") or [])
            )
            cache_keys = bucket.get("cache_keys")
            base_fingerprints = bucket.get("base_fingerprints")
            prefix_fingerprints = bucket.get("prefix_fingerprints")
            rows.append(
                {
                    label: name,
                    "calls": calls,
                    "successful_calls": max(
                        0,
                        int(bucket.get("successful_calls") or 0),
                    ),
                    "failed_calls": max(0, int(bucket.get("failed_calls") or 0)),
                    "eligible_calls": max(
                        0,
                        int(bucket.get("eligible_calls") or 0),
                    ),
                    "usage_reported_calls": usage_reported_calls,
                    "unknown_calls": unknown_calls,
                    "usage_status": usage_status,
                    "hit_calls": max(0, int(bucket.get("hit_calls") or 0)),
                    "known_miss_calls": max(
                        0,
                        int(bucket.get("known_miss_calls") or 0),
                    ),
                    "prompt_tokens": prompt_tokens,
                    "eligible_prompt_tokens": eligible_prompt_tokens,
                    "reported_prompt_tokens": reported_prompt_tokens,
                    "cached_tokens": cached_tokens,
                    "cache_miss_tokens": max(
                        0,
                        int(bucket.get("cache_miss_tokens") or 0),
                    ),
                    "cache_miss_tokens_reported_calls": max(
                        0,
                        int(
                            bucket.get("cache_miss_tokens_reported_calls") or 0
                        ),
                    ),
                    "cache_write_tokens": max(
                        0,
                        int(bucket.get("cache_write_tokens") or 0),
                    ),
                    "read_ratio": (
                        round(cached_tokens / prompt_tokens, 4)
                        if prompt_tokens
                        else 0.0
                    ),
                    "eligible_read_ratio": (
                        round(eligible_cached_tokens / eligible_prompt_tokens, 4)
                        if eligible_prompt_tokens
                        else 0.0
                    ),
                    "reported_read_ratio": (
                        round(cached_tokens / reported_prompt_tokens, 4)
                        if reported_prompt_tokens
                        else 0.0
                    ),
                    "cache_key_variants": (
                        len(cache_keys) if isinstance(cache_keys, set) else 0
                    ),
                    "base_prefix_variants": (
                        len(base_fingerprints)
                        if isinstance(base_fingerprints, set)
                        else 0
                    ),
                    "longest_prefix_variants": (
                        len(prefix_fingerprints)
                        if isinstance(prefix_fingerprints, set)
                        else 0
                    ),
                    "latency": {
                        "sample_count": len(latencies),
                        "p50_ms": self._percentile(latencies, 0.50),
                        "p95_ms": self._percentile(latencies, 0.95),
                        "max_ms": max(latencies, default=0),
                    },
                    "hit_latency": {
                        "sample_count": len(hit_latencies),
                        "p50_ms": self._percentile(hit_latencies, 0.50),
                        "p95_ms": self._percentile(hit_latencies, 0.95),
                        "max_ms": max(hit_latencies, default=0),
                    },
                    "miss_latency": {
                        "sample_count": len(miss_latencies),
                        "p50_ms": self._percentile(miss_latencies, 0.50),
                        "p95_ms": self._percentile(miss_latencies, 0.95),
                        "max_ms": max(miss_latencies, default=0),
                    },
                }
            )
        return sorted(
            rows,
            key=lambda row: (
                -int(row.get("prompt_tokens") or 0),
                str(row.get(label) or ""),
            ),
        )

    @staticmethod
    def _prompt_cache_usage_status(
        *,
        calls: int,
        usage_reported_calls: int,
    ) -> str:
        total = max(0, int(calls))
        reported = max(0, min(total, int(usage_reported_calls)))
        if total <= 0:
            return "no_calls"
        if reported <= 0:
            return "unknown"
        if reported < total:
            return "partial"
        return "reported"

    def _provider_availability_payload(
        self,
        *,
        last_call: dict[str, object],
        circuit_breaker: dict[str, object],
    ) -> dict[str, object]:
        circuits = [
            item
            for item in list(circuit_breaker.get("circuits") or [])
            if isinstance(item, dict)
        ]
        open_circuits = [item for item in circuits if item.get("state") == "open"]
        half_open_circuits = [
            item for item in circuits if item.get("state") == "half_open"
        ]
        if open_circuits:
            state = "unavailable"
            label = "模型不可用，GM 当前无法生成回复"
        elif half_open_circuits:
            state = "recovering"
            label = "模型正在恢复，当前回复可能延迟或失败"
        elif self.total_calls <= 0:
            state = "waiting"
            label = "等待首次模型调用"
        elif bool(last_call.get("ok")):
            state = "available"
            label = "模型可用"
        else:
            state = "unavailable"
            label = "模型不可用，GM 当前无法生成回复"

        problem_circuits = open_circuits or half_open_circuits
        circuit_error = next(
            (
                str(item.get("last_error") or "")
                for item in problem_circuits
                if str(item.get("last_error") or "")
            ),
            "",
        )
        circuit_endpoints = next(
            (
                list(item.get("endpoints") or [])
                for item in problem_circuits
                if item.get("endpoints")
            ),
            [],
        )
        retry_after_seconds = max(
            [float(item.get("retry_after_seconds") or 0.0) for item in problem_circuits]
            or [0.0]
        )
        configured_endpoints = self.config.chat_completions_urls()
        return {
            "state": state,
            "label": label,
            "model": str(
                last_call.get("model")
                or self.config.action_model
                or self.config.expressor_model
                or ""
            ),
            "endpoint": str(
                last_call.get("endpoint")
                or (circuit_endpoints[0] if circuit_endpoints else "")
                or (configured_endpoints[0] if configured_endpoints else "")
            ),
            "last_error": str(last_call.get("error") or circuit_error or "")[:500],
            "last_checked_at": str(last_call.get("at") or ""),
            "last_operation": str(last_call.get("operation") or ""),
            "last_attempt": max(0, int(last_call.get("attempt") or 0)),
            "last_elapsed_ms": max(0, int(last_call.get("elapsed_ms") or 0)),
            "retry_after_seconds": round(retry_after_seconds, 3),
        }

    def telemetry_payload(self) -> dict:
        slowest = sorted(self.recent_calls, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:5]
        last = self.recent_calls[-1] if self.recent_calls else {}
        recent = self.recent_calls[-10:]
        average = 0
        if recent:
            average = int(sum(int(item.get("elapsed_ms", 0)) for item in recent) / len(recent))
        latency_values = sorted(self.call_latency_history_ms)
        hit_latency_values = sorted(self.cache_hit_latency_history_ms)
        miss_latency_values = sorted(self.cache_miss_latency_history_ms)
        cache_read_ratio = (
            round(self.cached_token_total / self.prompt_token_total, 4)
            if self.prompt_token_total
            else 0.0
        )
        cache_write_ratio = (
            round(self.cache_write_token_total / self.prompt_token_total, 4)
            if self.prompt_token_total
            else 0.0
        )
        eligible_read_ratio = (
            round(
                self.cache_eligible_cached_token_total
                / self.cache_eligible_prompt_token_total,
                4,
            )
            if self.cache_eligible_prompt_token_total
            else 0.0
        )
        reported_read_ratio = (
            round(
                self.cached_token_total
                / self.cache_usage_reported_prompt_token_total,
                4,
            )
            if self.cache_usage_reported_prompt_token_total
            else 0.0
        )
        cache_usage_status = self._prompt_cache_usage_status(
            calls=self.total_calls,
            usage_reported_calls=self.cache_usage_reported_calls,
        )
        with self._prompt_cache_lock:
            capabilities = [
                {
                    "endpoint": endpoint,
                    "model": model,
                    "mode": mode,
                }
                for (endpoint, model), mode in sorted(self._prompt_cache_capabilities.items())
            ]
        circuit_breaker = self.circuit_breaker_payload()
        return {
            "total_calls": self.total_calls,
            "failed_calls": self.failed_call_count,
            "availability": self._provider_availability_payload(
                last_call=last,
                circuit_breaker=circuit_breaker,
            ),
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
            "prompt_cache": {
                "enabled": bool(self.config.prompt_cache_enabled),
                "configured_mode": str(self.config.prompt_cache_mode or "auto"),
                "usage_status": cache_usage_status,
                "eligible_calls": self.cache_eligible_calls,
                "usage_reported_calls": self.cache_usage_reported_calls,
                "hit_calls": self.cache_hit_calls,
                "known_miss_calls": self.cache_known_miss_calls,
                "unknown_calls": max(
                    0,
                    self.total_calls - self.cache_usage_reported_calls,
                ),
                "prompt_tokens": self.prompt_token_total,
                "eligible_prompt_tokens": self.cache_eligible_prompt_token_total,
                "reported_prompt_tokens": self.cache_usage_reported_prompt_token_total,
                "cached_tokens": self.cached_token_total,
                "cache_miss_tokens": self.cache_miss_token_total,
                "cache_miss_tokens_reported_calls": (
                    self.cache_miss_token_reported_calls
                ),
                "cache_write_tokens": self.cache_write_token_total,
                "read_ratio": cache_read_ratio,
                "eligible_read_ratio": eligible_read_ratio,
                "reported_read_ratio": reported_read_ratio,
                "write_ratio": cache_write_ratio,
                "hit_latency": {
                    "sample_count": len(hit_latency_values),
                    "p50_ms": self._percentile(hit_latency_values, 0.50),
                    "p95_ms": self._percentile(hit_latency_values, 0.95),
                },
                "miss_latency": {
                    "sample_count": len(miss_latency_values),
                    "p50_ms": self._percentile(miss_latency_values, 0.50),
                    "p95_ms": self._percentile(miss_latency_values, 0.95),
                },
                "capabilities": capabilities,
                "by_family": self._prompt_cache_breakdown_payload(
                    self._prompt_cache_family_stats,
                    label="family",
                ),
                "by_operation": self._prompt_cache_breakdown_payload(
                    self._prompt_cache_operation_stats,
                    label="operation",
                ),
            },
            "circuit_breaker": circuit_breaker,
        }

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
        return int(values[index])
