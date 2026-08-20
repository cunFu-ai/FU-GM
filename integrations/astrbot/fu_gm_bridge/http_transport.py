from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import time
from typing import Callable
from urllib import request
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class JsonHttpResult:
    payload: dict[str, object]
    attempts: int


def is_connection_refused_error(exc: BaseException) -> bool:
    """Return true only when the peer rejected the TCP connection.

    Retrying this case is safe because no HTTP request reached FU-GM. Timeouts
    and connection resets deliberately do not qualify: the server may already
    have committed a stateful action before the response was lost.
    """

    candidates: list[BaseException] = [exc]
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        candidates.append(exc.reason)
    for candidate in candidates:
        if isinstance(candidate, OSError) and candidate.errno in {
            errno.ECONNREFUSED,
            61,
            111,
        }:
            return True
    return "connection refused" in str(exc).lower()


def request_json_with_connection_retry(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    timeout_seconds: float = 150.0,
    connection_retries: int = 4,
    initial_backoff_seconds: float = 0.25,
    opener: Callable[..., object] = request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> JsonHttpResult:
    """Perform one JSON request and retry only definite connection refusals."""

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    retry_limit = max(0, int(connection_retries))
    backoff = max(0.0, float(initial_backoff_seconds))

    for attempt in range(1, retry_limit + 2):
        try:
            with opener(http_request, timeout=timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                return JsonHttpResult(
                    {
                        "ok": False,
                        "error": "FU-GM 返回了无法识别的数据。",
                        "error_code": "FU_GM_INVALID_RESPONSE",
                    },
                    attempt,
                )
            return JsonHttpResult(decoded, attempt)
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                decoded = {
                    "ok": False,
                    "error": body or str(exc),
                    "error_code": "FU_GM_HTTP_ERROR",
                }
            if not isinstance(decoded, dict):
                decoded = {
                    "ok": False,
                    "error": body or str(exc),
                    "error_code": "FU_GM_HTTP_ERROR",
                }
            return JsonHttpResult(decoded, attempt)
        except Exception as exc:
            refused = is_connection_refused_error(exc)
            if refused and attempt <= retry_limit:
                sleeper(min(2.0, backoff * (2 ** (attempt - 1))))
                continue
            return JsonHttpResult(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": (
                        "FU_GM_CONNECTION_REFUSED"
                        if refused
                        else "FU_GM_REQUEST_FAILED"
                    ),
                    "retryable": refused,
                    "attempts": attempt,
                },
                attempt,
            )

    raise AssertionError("unreachable")


def public_transport_failure_reply(payload: dict) -> str:
    if str(payload.get("error_code") or "") == "FU_GM_CONNECTION_REFUSED":
        return "跑团服务暂时断开了，我现在处理不了这句话。等几秒再试一次。"
    return ""
