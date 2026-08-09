from __future__ import annotations

import errno
import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError


def _load_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "integrations"
        / "astrbot"
        / "fu_gm_bridge"
        / "http_transport.py"
    )
    spec = importlib.util.spec_from_file_location(
        "fu_gm_bridge_http_transport",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


transport = _load_module()


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_connection_refusal_retries_then_returns_success() -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(_request, *, timeout):
        nonlocal calls
        calls += 1
        assert timeout == 12
        if calls < 3:
            raise URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        return FakeResponse({"ok": True, "reply": "接上了"})

    result = transport.request_json_with_connection_retry(
        "POST",
        "http://127.0.0.1:8765/v1/message/route",
        payload={"message_id": "m1"},
        timeout_seconds=12,
        connection_retries=4,
        initial_backoff_seconds=0.1,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert result.payload == {"ok": True, "reply": "接上了"}
    assert result.attempts == 3
    assert sleeps == [0.1, 0.2]


def test_exhausted_refusal_is_structured_and_not_exposed_verbatim() -> None:
    sleeps: list[float] = []

    def opener(_request, *, timeout):
        raise URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    result = transport.request_json_with_connection_retry(
        "POST",
        "http://127.0.0.1:8765/v1/message/route",
        connection_retries=2,
        initial_backoff_seconds=0.1,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert result.attempts == 3
    assert result.payload["error_code"] == "FU_GM_CONNECTION_REFUSED"
    assert result.payload["retryable"] is True
    assert transport.public_transport_failure_reply(result.payload) == (
        "跑团服务暂时断开了，我现在处理不了这句话。等几秒再试一次。"
    )
    assert sleeps == [0.1, 0.2]


def test_connection_reset_is_not_retried() -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise URLError(ConnectionResetError(errno.ECONNRESET, "reset"))

    result = transport.request_json_with_connection_retry(
        "POST",
        "http://127.0.0.1:8765/v1/message/route",
        connection_retries=4,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert calls == 1
    assert sleeps == []
    assert result.payload["error_code"] == "FU_GM_REQUEST_FAILED"


def test_http_error_is_returned_without_transport_retry() -> None:
    calls = 0

    def opener(request, *, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(
            request.full_url,
            409,
            "conflict",
            {},
            io.BytesIO(b'{"ok": false, "error": "conflict"}'),
        )

    result = transport.request_json_with_connection_retry(
        "POST",
        "http://127.0.0.1:8765/v1/message/route",
        connection_retries=4,
        opener=opener,
    )

    assert calls == 1
    assert result.payload == {"ok": False, "error": "conflict"}
