import io
import json
import http.client
import os
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fu_gm.app_factory import _component_llm_config, _session_zero_llm_config
from fu_gm.config import LLMConfig
from fu_gm.expressor import LLMExpressor
from fu_gm.llm_client import (
    ChatMessage,
    LLMDeadlineExceeded,
    LLMEmptyResponseError,
    LLMHTTPError,
    LLMProviderCircuitOpen,
    OpenAICompatibleClient,
    UrlLibTransport,
    classify_llm_error,
)
from fu_gm.models import Action, ActionResolution, ActionType, RollOutcome
from fu_gm.prompt_cache import build_cache_friendly_messages


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        content = self.responses.pop(0)
        if isinstance(content, BaseException):
            raise content
        if isinstance(content, dict):
            return content
        return {"choices": [{"message": {"content": content}}]}


class TestLLMErrorClassification(unittest.TestCase):
    def test_external_transport_can_be_disabled_for_injected_test_backends(self) -> None:
        transport = FakeTransport(["must not be called"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
            ),
            transport=transport,
        )

        with patch.dict(
            os.environ,
            {"FU_GM_DISABLE_EXTERNAL_LLM_TRANSPORT": "1"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "禁止外部 LLM 传输"):
                client.create_chat_completion(
                    model="test-model",
                    messages=[ChatMessage(role="user", content="hello")],
                )

        self.assertEqual(transport.calls, [])

    def test_chinese_shared_deadline_is_transient_transport_failure(self) -> None:
        disposition = classify_llm_error("GM工具事务已超过共享截止时间。")

        self.assertEqual(disposition.category, "transport")
        self.assertTrue(disposition.retryable)

    def test_english_insufficient_balance_is_account_inactive(self) -> None:
        disposition = classify_llm_error(
            LLMHTTPError(
                status_code=403,
                body='{"error":{"message":"insufficient balance","type":"billing_error"}}',
            )
        )

        self.assertEqual(disposition.category, "account_inactive")
        self.assertFalse(disposition.retryable)


class HTTPConnectionPoolTests(unittest.TestCase):
    def test_direct_transport_reuses_keep_alive_connection(self) -> None:
        client_ports: list[int] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                client_ports.append(int(self.client_address[1]))
                body = b'{"choices":[{"message":{"content":"ok"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = UrlLibTransport()
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        try:
            first = transport.post_json(url, {}, {"message": "one"}, 2.0)
            second = transport.post_json(url, {}, {"message": "two"}, 2.0)
        finally:
            transport.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.assertEqual(first, second)
        self.assertEqual(transport.connection_open_count, 1)
        self.assertEqual(transport.connection_reuse_count, 1)
        self.assertEqual(len(set(client_ports)), 1)


class LLMIntegrationTests(unittest.TestCase):
    def test_creative_writer_inherits_expressor_or_accepts_explicit_deepseek_route(self) -> None:
        base = LLMConfig(
            api_base_url="https://core.test/v1",
            backup_api_base_urls=("https://core-backup.test/v1",),
            api_key="core-key",
            action_model="gpt-5.6-terra",
            expressor_model="gpt-5.6-terra",
        )
        with patch.dict(
            os.environ,
            {
                "FU_GM_EXPRESSOR_API_BASE_URL": "https://api.deepseek.com",
                "FU_GM_EXPRESSOR_API_KEY": "deepseek-key",
                "FU_GM_EXPRESSOR_MODEL": "deepseek-v4-flash",
            },
            clear=True,
        ):
            expressor = _component_llm_config(base, "EXPRESSOR")
            creative = _component_llm_config(expressor, "CREATIVE")

        self.assertEqual(creative.action_model, "deepseek-v4-flash")
        self.assertEqual(creative.api_base_url, "https://api.deepseek.com")
        self.assertEqual(creative.api_key, "deepseek-key")
        self.assertEqual(expressor.backup_api_base_urls, ())
        self.assertEqual(creative.backup_api_base_urls, ())

        with patch.dict(
            os.environ,
            {
                "FU_GM_CREATIVE_API_BASE_URL": "https://creative.deepseek.test/v1",
                "FU_GM_CREATIVE_API_KEY": "creative-key",
                "FU_GM_CREATIVE_MODEL": "deepseek-v4-flash",
            },
            clear=True,
        ):
            explicit = _component_llm_config(expressor, "CREATIVE")

        self.assertEqual(explicit.api_base_url, "https://creative.deepseek.test/v1")
        self.assertEqual(explicit.api_key, "creative-key")
        self.assertEqual(explicit.backup_api_base_urls, ())

    def test_component_provider_can_define_its_own_backup_endpoints(self) -> None:
        base = LLMConfig(
            api_base_url="https://core.test/v1",
            backup_api_base_urls=("https://core-backup.test/v1",),
            api_key="core-key",
            action_model="gpt-5.6-terra",
            expressor_model="gpt-5.6-terra",
        )
        with patch.dict(
            os.environ,
            {
                "FU_GM_EXPRESSOR_API_BASE_URL": "https://api.deepseek.com",
                "FU_GM_EXPRESSOR_API_KEY": "deepseek-key",
                "FU_GM_EXPRESSOR_MODEL": "deepseek-v4-flash",
                "FU_GM_EXPRESSOR_BACKUP_API_BASE_URLS": (
                    "https://deepseek-backup.test/v1"
                ),
            },
            clear=True,
        ):
            expressor = _component_llm_config(base, "EXPRESSOR")

        self.assertEqual(
            expressor.backup_api_base_urls,
            ("https://deepseek-backup.test/v1",),
        )

    def test_provider_error_classifier_separates_permanent_and_retryable_errors(self) -> None:
        inactive = classify_llm_error(
            LLMHTTPError(status_code=403, body='{"code":"USER_INACTIVE"}')
        )
        policy = classify_llm_error(
            LLMHTTPError(status_code=403, body='{"code":"content_policy"}')
        )
        overloaded = classify_llm_error(
            LLMHTTPError(status_code=502, body="upstream unavailable")
        )

        self.assertEqual(inactive.category, "account_inactive")
        self.assertFalse(inactive.retryable)
        self.assertFalse(inactive.stage_degradable)
        self.assertEqual(policy.category, "content_policy")
        self.assertFalse(policy.retryable)
        self.assertTrue(policy.stage_degradable)
        self.assertEqual(overloaded.category, "upstream")
        self.assertTrue(overloaded.retryable)
        self.assertTrue(overloaded.failover)

    def test_authentication_error_does_not_cycle_to_backup_endpoint(self) -> None:
        transport = FakeTransport(
            [LLMHTTPError(status_code=401, body="invalid token")]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=4,
            ),
            transport=transport,
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content="hello")],
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            client.recent_calls[-1]["error_category"],
            "authentication",
        )

    def test_upstream_error_retries_on_backup_endpoint(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=502, body="upstream unavailable"),
                "backup ok",
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=1,
            ),
            transport=transport,
        )

        result = client.create_chat_completion(
            model="test-model",
            messages=[ChatMessage(role="user", content="hello")],
        )

        self.assertEqual(result, "backup ok")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            transport.calls[1]["url"],
            "https://backup.test/v1/chat/completions",
        )
        self.assertEqual(client.recent_calls[0]["error_category"], "upstream")
        self.assertEqual(
            client.consume_call_diagnostics(),
            {
                "recovered": True,
                "recovery_codes": ["PROVIDER_RECOVERED"],
                "attempt_count": 2,
            },
        )
        self.assertEqual(client.consume_call_diagnostics(), {})

    def test_content_policy_error_does_not_repeat_same_request(self) -> None:
        transport = FakeTransport(
            [LLMHTTPError(status_code=403, body='{"code":"content_policy"}')]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=3,
            ),
            transport=transport,
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content="hello")],
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            client.recent_calls[-1]["error_category"],
            "content_policy",
        )

    def test_recovery_diagnostics_are_isolated_between_shared_client_threads(self) -> None:
        first_a_failed = threading.Event()
        b_completed = threading.Event()

        class InterleavingTransport:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.a_calls = 0

            def post_json(self, url, headers, payload, timeout):
                del url, headers, timeout
                content = str(payload["messages"][0]["content"])
                if content == "call-A":
                    with self.lock:
                        self.a_calls += 1
                        attempt = self.a_calls
                    if attempt == 1:
                        first_a_failed.set()
                        raise LLMHTTPError(
                            status_code=502,
                            body="upstream unavailable",
                        )
                    if not b_completed.wait(timeout=2):
                        raise AssertionError("call-B did not finish")
                    return {"choices": [{"message": {"content": "A ok"}}]}
                b_completed.set()
                return {"choices": [{"message": {"content": "B ok"}}]}

        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=1,
            ),
            transport=InterleavingTransport(),
        )
        results: dict[str, tuple[str, dict[str, object]]] = {}

        def invoke(label: str) -> None:
            content = client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content=label)],
            )
            results[label] = (content, client.consume_call_diagnostics())

        worker_a = threading.Thread(target=invoke, args=("call-A",))
        worker_a.start()
        self.assertTrue(first_a_failed.wait(timeout=1))
        worker_b = threading.Thread(target=invoke, args=("call-B",))
        worker_b.start()
        worker_a.join(timeout=3)
        worker_b.join(timeout=3)

        self.assertFalse(worker_a.is_alive())
        self.assertFalse(worker_b.is_alive())
        self.assertEqual(results["call-A"][0], "A ok")
        self.assertEqual(
            results["call-A"][1]["recovery_codes"],
            ["PROVIDER_RECOVERED"],
        )
        self.assertEqual(results["call-B"], ("B ok", {}))

    def test_llm_config_prefers_luna_specific_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FU_GM_DOTENV_PATH": "__missing_fu_gm_test_env__",
                "FU_GM_API_KEY": "shared-key",
                "FU_GM_LUNA_API_KEY": "luna-key",
                "FU_GM_ACTION_MODEL": "gpt-5.6-luna",
                "FU_GM_EXPRESSOR_MODEL": "gpt-5.6-luna",
            },
            clear=True,
        ):
            config = LLMConfig.from_env()

        self.assertEqual(config.api_key, "luna-key")

    def test_component_config_selects_credential_for_overridden_model(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FU_GM_API_KEY": "shared-key",
                "FU_GM_LUNA_API_KEY": "luna-key",
                "FU_GM_TERRA_API_KEY": "terra-key",
                "FU_GM_EXPRESSOR_API_KEY": "legacy-component-key",
                "FU_GM_EXPRESSOR_MODEL": "gpt-5.6-terra",
            },
            clear=True,
        ):
            config = LLMConfig(
                api_base_url="https://example.invalid/v1",
                api_key="luna-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
            )
            expressor_config = _component_llm_config(config, "EXPRESSOR")

        self.assertEqual(expressor_config.expressor_model, "gpt-5.6-terra")
        self.assertEqual(expressor_config.api_key, "terra-key")

    def test_client_bounds_retries_by_one_shared_wall_clock_deadline(self) -> None:
        class SlowFailureTransport:
            def __init__(self) -> None:
                self.calls = []

            def post_json(self, url, headers, payload, timeout):
                self.calls.append(timeout)
                time.sleep(0.03)
                raise TimeoutError("slow upstream")

        transport = SlowFailureTransport()
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                timeout_seconds=5,
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=2,
            ),
            transport=transport,
        )
        started = time.monotonic()

        with self.assertRaises(LLMDeadlineExceeded):
            client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content="hello")],
                deadline=started + 0.01,
                operation="test.shared_deadline",
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(client.recent_calls[-1]["operation"], "test.shared_deadline")
        self.assertEqual(client.recent_calls[-1]["attempt"], 1)

    def test_client_passes_remaining_operation_budget_to_transport(self) -> None:
        transport = FakeTransport(["ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                timeout_seconds=60,
            ),
            transport=transport,
        )

        client.create_chat_completion(
            model="test-model",
            messages=[ChatMessage(role="user", content="hello")],
            deadline=time.monotonic() + 0.5,
            operation="test.remaining_budget",
        )

        self.assertGreater(transport.calls[0]["timeout"], 0)
        self.assertLessEqual(transport.calls[0]["timeout"], 0.5)
        self.assertEqual(client.recent_calls[-1]["operation"], "test.remaining_budget")

    def test_client_forwards_requested_output_token_budget(self) -> None:
        transport = FakeTransport(["ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
            ),
            transport=transport,
        )

        client.create_chat_completion(
            model="test-model",
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=4096,
        )

        self.assertEqual(transport.calls[0]["payload"]["max_tokens"], 4096)
        self.assertEqual(client.recent_calls[-1]["max_tokens"], 4096)

    def test_client_reserves_shared_deadline_for_backup_endpoint(self) -> None:
        transport = FakeTransport([TimeoutError("primary timed out"), "backup ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                timeout_seconds=30,
                endpoint_attempt_timeout_seconds=14,
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=1,
            ),
            transport=transport,
        )

        content = client.create_chat_completion(
            model="test-model",
            messages=[ChatMessage(role="user", content="hello")],
            deadline=time.monotonic() + 30,
            operation="test.endpoint_slice",
        )

        self.assertEqual(content, "backup ok")
        self.assertEqual(len(transport.calls), 2)
        self.assertLessEqual(transport.calls[0]["timeout"], 14)
        self.assertLessEqual(transport.calls[1]["timeout"], 14)
        self.assertEqual(transport.calls[1]["url"], "https://backup.test/v1/chat/completions")

    def test_client_applies_attempt_timeout_to_single_endpoint(self) -> None:
        transport = FakeTransport(["ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://single.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                timeout_seconds=30,
                endpoint_attempt_timeout_seconds=1,
            ),
            transport=transport,
        )

        client.create_chat_completion(
            model="test-model",
            messages=[ChatMessage(role="user", content="hello")],
        )

        self.assertLessEqual(transport.calls[0]["timeout"], 1)

    def test_client_telemetry_keeps_model_latency_distribution(self) -> None:
        transport = FakeTransport(["ok", "ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
            ),
            transport=transport,
        )

        for _ in range(2):
            client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content="hello")],
            )

        telemetry = client.telemetry_payload()
        self.assertEqual(telemetry["total_calls"], 2)
        self.assertEqual(telemetry["latency"]["sample_count"], 2)
        self.assertIn("p50_ms", telemetry["latency"])
        self.assertIn("p95_ms", telemetry["latency"])
        self.assertEqual(telemetry["availability"]["state"], "available")
        self.assertEqual(telemetry["availability"]["label"], "模型可用")

    def test_client_exposes_provider_failure_and_logs_recovery(self) -> None:
        transport = FakeTransport([TimeoutError("primary\nreset"), "backup ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.test/v1",
                backup_api_base_urls=("https://backup.test/v1",),
                api_key="secret-test-key",
                action_model="test-model",
                expressor_model="test-model",
                timeout_seconds=30,
                endpoint_attempt_timeout_seconds=10,
                reactive_recovery_enabled=True,
                reactive_recovery_max_retries=1,
            ),
            transport=transport,
        )
        output = io.StringIO()

        with redirect_stderr(output):
            result = client.create_chat_completion(
                model="test-model",
                messages=[ChatMessage(role="user", content="hello")],
                operation="test.provider_recovery",
            )

        telemetry = client.telemetry_payload()
        logs = output.getvalue()
        self.assertEqual(result, "backup ok")
        self.assertEqual(telemetry["availability"]["state"], "available")
        self.assertEqual(telemetry["failed_calls"], 1)
        self.assertIn("[FU-GM LLM] FAILED", logs)
        self.assertIn("[FU-GM LLM] RECOVERED", logs)
        self.assertIn("error=primary reset", logs)
        self.assertNotIn("secret-test-key", logs)

    def test_client_exposes_last_provider_error_when_request_fails(self) -> None:
        transport = FakeTransport([ConnectionResetError("upstream unavailable")])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.test/v1",
                api_key="secret-test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
        )

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(ConnectionResetError):
                client.create_chat_completion(
                    model="test-model",
                    messages=[ChatMessage(role="user", content="hello")],
                    operation="test.provider_failure",
                )

        availability = client.telemetry_payload()["availability"]
        self.assertEqual(availability["state"], "unavailable")
        self.assertEqual(
            availability["label"],
            "模型不可用，GM 当前无法生成回复",
        )
        self.assertEqual(availability["last_error"], "upstream unavailable")
        self.assertEqual(availability["last_operation"], "test.provider_failure")

    def test_concurrent_empty_response_marks_its_own_telemetry_record(self) -> None:
        first_extract_started = threading.Event()
        second_finished = threading.Event()

        class InterleavedTransport:
            def post_json(self, url, headers, payload, timeout):
                operation = payload["messages"][0]["content"]
                if operation == "call-B":
                    self.assert_first_started()
                    return {
                        "marker": "B",
                        "choices": [{"message": {"content": "ok"}}],
                    }
                return {
                    "marker": "A",
                    "choices": [{"message": {"content": ""}}],
                }

            @staticmethod
            def assert_first_started() -> None:
                if not first_extract_started.wait(timeout=2):
                    raise AssertionError("A 调用尚未进入内容提取阶段。")

        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://example.test/v1",
                api_key="test-key",
                action_model="test-model",
                expressor_model="test-model",
                reactive_recovery_enabled=False,
            ),
            transport=InterleavedTransport(),
        )
        original_extract = client._extract_content

        def interleaved_extract(data):
            if data.get("marker") == "A":
                first_extract_started.set()
                if not second_finished.wait(timeout=2):
                    raise AssertionError("B 调用未在预期时间内完成。")
            return original_extract(data)

        client._extract_content = interleaved_extract
        errors: list[tuple[str, str]] = []

        def run(operation: str) -> None:
            try:
                client.create_chat_completion(
                    model="test-model",
                    messages=[ChatMessage(role="user", content=operation)],
                    operation=operation,
                )
            except Exception as exc:
                errors.append((operation, exc.__class__.__name__))
            finally:
                if operation == "call-B":
                    second_finished.set()

        with redirect_stderr(io.StringIO()):
            first = threading.Thread(target=run, args=("call-A",))
            second = threading.Thread(target=run, args=("call-B",))
            first.start()
            self.assertTrue(first_extract_started.wait(timeout=2))
            second.start()
            first.join(timeout=3)
            second.join(timeout=3)

        records = {str(item["operation"]): item for item in client.recent_calls}
        self.assertEqual(errors, [("call-A", "LLMEmptyResponseError")])
        self.assertFalse(records["call-A"]["ok"])
        self.assertTrue(records["call-A"]["response_empty"])
        self.assertTrue(records["call-B"]["ok"])
        self.assertNotIn("response_empty", records["call-B"])
        self.assertEqual(client.failed_call_count, 1)

    def test_gpt56_sends_explicit_cache_breakpoint_and_records_usage(self) -> None:
        transport = FakeTransport(
            [
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 2000,
                        "completion_tokens": 20,
                        "total_tokens": 2020,
                        "prompt_tokens_details": {
                            "cached_tokens": 1536,
                            "cache_write_tokens": 0,
                        },
                    },
                }
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache.example/v1",
                api_key="test-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
            ),
            transport=transport,
        )
        messages = build_cache_friendly_messages(
            static_system_prompt="稳定规则" * 800,
            user_content="本轮动态消息",
            cache_family="gm-initial",
        )

        client.create_chat_completion(model="gpt-5.6-luna", messages=messages)

        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["prompt_cache_options"], {"mode": "explicit", "ttl": "30m"})
        self.assertTrue(payload["prompt_cache_key"].startswith("fugm:v1:gm-initial:"))
        system_content = payload["messages"][0]["content"]
        self.assertIsInstance(system_content, list)
        self.assertEqual(
            system_content[0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        cache = client.telemetry_payload()["prompt_cache"]
        self.assertEqual(cache["usage_reported_calls"], 1)
        self.assertEqual(cache["hit_calls"], 1)
        self.assertEqual(cache["cached_tokens"], 1536)
        self.assertEqual(cache["read_ratio"], 0.768)
        self.assertEqual(cache["eligible_read_ratio"], 0.768)
        self.assertEqual(cache["reported_read_ratio"], 0.768)
        self.assertEqual(cache["known_miss_calls"], 0)
        self.assertEqual(cache["by_family"][0]["family"], "gm-initial")
        self.assertEqual(cache["by_family"][0]["hit_calls"], 1)
        self.assertEqual(
            cache["by_operation"][0]["operation"],
            "chat_completion",
        )

    def test_deepseek_cache_usage_fields_are_normalized(self) -> None:
        usage = OpenAICompatibleClient._extract_usage(
            {
                "usage": {
                    "prompt_tokens": 640,
                    "completion_tokens": 20,
                    "total_tokens": 660,
                    "prompt_cache_hit_tokens": 512,
                    "prompt_cache_miss_tokens": 128,
                }
            }
        )

        self.assertTrue(usage["cache_usage_reported"])
        self.assertEqual(usage["cached_tokens"], 512)
        self.assertEqual(usage["cache_miss_tokens"], 128)

    def test_dynamic_suffixes_share_the_same_privacy_safe_cache_key(self) -> None:
        transport = FakeTransport(["first", "second"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache-key.example/v1",
                api_key="test-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
            ),
            transport=transport,
        )

        for suffix in ("玩家甲的私密动态消息", "玩家乙的另一条动态消息"):
            client.create_chat_completion(
                model="gpt-5.6-luna",
                messages=build_cache_friendly_messages(
                    static_system_prompt="完全相同的稳定规则" * 600,
                    user_content=suffix,
                    cache_family="gm-initial",
                ),
            )

        first_key = transport.calls[0]["payload"]["prompt_cache_key"]
        second_key = transport.calls[1]["payload"]["prompt_cache_key"]
        self.assertEqual(first_key, second_key)
        self.assertNotIn("玩家甲", first_key)
        self.assertNotIn("玩家乙", second_key)

    def test_provider_serializes_system_and_stable_turn_breakpoints(self) -> None:
        transport = FakeTransport(["ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache-layers.example/v1",
                api_key="test-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
            ),
            transport=transport,
        )
        messages = build_cache_friendly_messages(
            static_system_prompt="核心规则" * 600 + "阶段规则" * 200,
            user_content="稳定轮次上下文" * 80 + "动态权威状态",
            cache_family="gm-post-scene",
            cache_breakpoint_offsets=(1200, 3000),
            user_cache_breakpoint_offsets=(320,),
        )

        client.create_chat_completion(model="gpt-5.6-luna", messages=messages)

        payload_messages = transport.calls[0]["payload"]["messages"]
        self.assertIsInstance(payload_messages[0]["content"], list)
        self.assertIsInstance(payload_messages[1]["content"], list)
        record_cache = client.recent_calls[-1]["prompt_cache"]
        self.assertEqual(record_cache["family"], "gm-post-scene")
        self.assertEqual(record_cache["breakpoint_count"], 3)

    def test_cache_protocol_downgrades_once_and_remembers_endpoint_capability(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(
                    status_code=400,
                    body='{"error":{"message":"Unsupported parameter: prompt_cache_options"}}',
                ),
                "recovered",
                "remembered",
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache-downgrade.example/v1",
                api_key="test-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
        )
        messages = build_cache_friendly_messages(
            static_system_prompt="稳定规则" * 600,
            user_content="变化内容",
        )

        self.assertEqual(
            client.create_chat_completion(model="gpt-5.6-luna", messages=messages),
            "recovered",
        )
        self.assertIn("prompt_cache_options", transport.calls[0]["payload"])
        self.assertNotIn("prompt_cache_options", transport.calls[1]["payload"])
        self.assertIsInstance(transport.calls[1]["payload"]["messages"][0]["content"], list)

        self.assertEqual(
            client.create_chat_completion(model="gpt-5.6-luna", messages=messages),
            "remembered",
        )
        self.assertEqual(len(transport.calls), 3)
        self.assertNotIn("prompt_cache_options", transport.calls[2]["payload"])
        capability = client.telemetry_payload()["prompt_cache"]["capabilities"]
        self.assertEqual(capability[0]["mode"], "breakpoint")

    def test_missing_cache_usage_is_unknown_not_a_false_zero_hit(self) -> None:
        transport = FakeTransport(["ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache-unknown.example/v1",
                api_key="test-key",
                action_model="gpt-5.6-luna",
                expressor_model="gpt-5.6-luna",
            ),
            transport=transport,
        )

        client.create_chat_completion(
            model="gpt-5.6-luna",
            messages=build_cache_friendly_messages(
                static_system_prompt="稳定规则" * 600,
                user_content="动态内容",
            ),
        )

        cache = client.telemetry_payload()["prompt_cache"]
        self.assertEqual(cache["usage_status"], "unknown")
        self.assertEqual(cache["usage_reported_calls"], 0)
        self.assertEqual(cache["hit_calls"], 0)
        self.assertEqual(cache["known_miss_calls"], 0)
        self.assertEqual(cache["unknown_calls"], 1)
        self.assertEqual(cache["by_operation"][0]["usage_status"], "unknown")
        self.assertEqual(cache["by_operation"][0]["unknown_calls"], 1)
        self.assertNotIn("usage", client.recent_calls[-1])

    def test_per_operation_cache_telemetry_keeps_hit_miss_and_unknown_separate(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                {
                    "choices": [{"message": {"content": "hit"}}],
                    "usage": {
                        "prompt_tokens": 640,
                        "completion_tokens": 10,
                        "total_tokens": 650,
                        "prompt_cache_hit_tokens": 512,
                        "prompt_cache_miss_tokens": 128,
                    },
                },
                {
                    "choices": [{"message": {"content": "miss"}}],
                    "usage": {
                        "prompt_tokens": 700,
                        "completion_tokens": 10,
                        "total_tokens": 710,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 700,
                    },
                },
                "unknown",
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://cache-breakdown.example/v1",
                api_key="test-key",
                action_model="deepseek-v4-flash",
                expressor_model="deepseek-v4-flash",
            ),
            transport=transport,
        )
        messages = [ChatMessage(role="user", content="safe aggregate only")]

        for operation in (
            "gm_tool_agent.iteration_1",
            "gm_tool_agent.iteration_2",
            "gm_tool_agent.iteration_3",
        ):
            client.create_chat_completion(
                model="deepseek-v4-flash",
                messages=messages,
                operation=operation,
            )

        cache = client.telemetry_payload()["prompt_cache"]
        by_operation = {
            row["operation"]: row for row in cache["by_operation"]
        }
        self.assertEqual(cache["usage_status"], "partial")
        self.assertEqual(cache["usage_reported_calls"], 2)
        self.assertEqual(cache["unknown_calls"], 1)
        self.assertEqual(cache["hit_calls"], 1)
        self.assertEqual(cache["known_miss_calls"], 1)
        self.assertEqual(cache["prompt_tokens"], 1340)
        self.assertEqual(cache["cached_tokens"], 512)
        self.assertEqual(cache["cache_miss_tokens"], 828)
        self.assertEqual(cache["cache_miss_tokens_reported_calls"], 2)
        self.assertEqual(
            by_operation["gm_tool_agent.iteration_1"]["cache_miss_tokens"],
            128,
        )
        self.assertEqual(
            by_operation["gm_tool_agent.iteration_2"]["known_miss_calls"],
            1,
        )
        self.assertEqual(
            by_operation["gm_tool_agent.iteration_3"]["usage_status"],
            "unknown",
        )
        self.assertEqual(
            by_operation["gm_tool_agent.iteration_3"]["unknown_calls"],
            1,
        )
        self.assertEqual(
            by_operation["gm_tool_agent.iteration_3"]["latency"][
                "sample_count"
            ],
            1,
        )

    def test_component_llm_config_can_split_expressor_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FU_GM_DOTENV_PATH": "/dev/null",
                "FU_GM_EXPRESSOR_API_BASE_URL": "https://www.moxin.online/v1",
                "FU_GM_EXPRESSOR_API_KEY": "expressor-key",
                "FU_GM_EXPRESSOR_MODEL": "claude-opus-4-6",
            },
            clear=True,
        ):
            config = LLMConfig(
                api_base_url="https://ai-pixel.online",
                api_key="action-key",
                action_model="gpt-5.4-mini",
                expressor_model="gpt-5.4-mini",
            )

            action_config = _component_llm_config(config, "ACTION")
            expressor_config = _component_llm_config(config, "EXPRESSOR")

            self.assertEqual(action_config.api_base_url, "https://ai-pixel.online")
            self.assertEqual(action_config.api_key, "action-key")
            self.assertEqual(action_config.action_model, "gpt-5.4-mini")
            self.assertEqual(expressor_config.api_base_url, "https://www.moxin.online/v1")
            self.assertEqual(expressor_config.api_key, "expressor-key")
            self.assertEqual(expressor_config.expressor_model, "claude-opus-4-6")
            self.assertEqual(expressor_config.chat_completions_url(), "https://www.moxin.online/v1/chat/completions")

    def test_session_zero_config_can_override_endpoint_without_touching_action_config(self) -> None:
        old_env = os.environ.copy()
        try:
            os.environ["FU_GM_SESSION_ZERO_API_BASE_URL"] = "https://session.example/v1"
            os.environ["FU_GM_SESSION_ZERO_API_KEY"] = "session-key"
            os.environ["FU_GM_SESSION_ZERO_TIMEOUT_SECONDS"] = "9"
            config = LLMConfig(
                api_base_url="https://ai-pixel.online",
                api_key="action-key",
                action_model="gpt-5.4-mini",
                expressor_model="gpt-5.4-mini",
                timeout_seconds=120,
            )

            session_zero_config = _session_zero_llm_config(config)

            self.assertEqual(session_zero_config.api_base_url, "https://session.example/v1")
            self.assertEqual(session_zero_config.api_key, "session-key")
            self.assertEqual(session_zero_config.timeout_seconds, 9)
            self.assertEqual(config.api_base_url, "https://ai-pixel.online")
            self.assertEqual(config.api_key, "action-key")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_session_zero_config_applies_backup_only_override(self) -> None:
        old_env = os.environ.copy()
        try:
            for key in tuple(os.environ):
                if key.startswith("FU_GM_SESSION_ZERO_"):
                    os.environ.pop(key, None)
            os.environ["FU_GM_SESSION_ZERO_BACKUP_API_BASE_URLS"] = (
                "https://backup-one.example/v1,https://backup-two.example/v1"
            )
            config = LLMConfig(
                api_base_url="https://primary.example/v1",
                api_key="action-key",
                action_model="gpt-5.4-mini",
                expressor_model="gpt-5.4-mini",
                timeout_seconds=20,
            )

            session_zero_config = _session_zero_llm_config(config)

            self.assertEqual(
                session_zero_config.backup_api_base_urls,
                ("https://backup-one.example/v1", "https://backup-two.example/v1"),
            )
            self.assertEqual(session_zero_config.timeout_seconds, 20)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_llm_config_disables_heuristic_fallback_by_default(self) -> None:
        old_dotenv = os.environ.get("FU_GM_DOTENV_PATH")
        old_fallback = os.environ.get("FU_GM_ALLOW_HEURISTIC_FALLBACK")
        try:
            os.environ["FU_GM_DOTENV_PATH"] = "__missing_fu_gm_test_env__"
            os.environ.pop("FU_GM_ALLOW_HEURISTIC_FALLBACK", None)
            self.assertFalse(LLMConfig.from_env().allow_heuristic_fallback)

            os.environ["FU_GM_ALLOW_HEURISTIC_FALLBACK"] = "0"
            self.assertFalse(LLMConfig.from_env().allow_heuristic_fallback)
        finally:
            if old_dotenv is None:
                os.environ.pop("FU_GM_DOTENV_PATH", None)
            else:
                os.environ["FU_GM_DOTENV_PATH"] = old_dotenv
            if old_fallback is None:
                os.environ.pop("FU_GM_ALLOW_HEURISTIC_FALLBACK", None)
            else:
                os.environ["FU_GM_ALLOW_HEURISTIC_FALLBACK"] = old_fallback

    def test_expressor_uses_gpt_5_4_nano_model(self) -> None:
        transport = FakeTransport(["【战斗结算】雷光炸裂，帝国机甲被命中弱点。"])
        config = LLMConfig(
            api_base_url="https://api.apiyi.com",
            api_key="test-key",
            action_model="gpt-5.4-nano",
            expressor_model="gpt-5.4-nano",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        expressor = LLMExpressor(client=client, model=config.expressor_model)

        text = expressor.render(
            ActionResolution(
                action=Action(
                    action_type=ActionType.REQUEST_ROLL,
                    parameters={"in_mind_reply": "雷鸣撕裂空气。"},
                ),
                rules_text="检定成功并造成伤害。",
                payload={
                    "roll": RollOutcome(
                        actor="瓦莉亚",
                        attributes=["DEX", "MIG"],
                        dice=[(8, 7), (10, 7)],
                        total=14,
                        modifier=0,
                        high_roll=7,
                        target_number=12,
                        success=True,
                        critical_success=False,
                        fumble=False,
                        target="帝国机甲",
                        damage=24,
                        damage_type="lightning",
                        hp_after=36,
                    )
                },
            )
        )

        self.assertIn("瓦莉亚 对 帝国机甲 的检定", text)
        self.assertNotIn("【战斗结算】", text)
        self.assertEqual(transport.calls[0]["payload"]["model"], "gpt-5.4-nano")

    def test_client_passes_deepseek_reasoning_and_thinking_options(self) -> None:
        transport = FakeTransport(["你好，英雄。"])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
            reasoning_effort="high",
            thinking_enabled=True,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[],
            temperature=0.1,
        )

        payload = transport.calls[0]["payload"]
        self.assertEqual(content, "你好，英雄。")
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(transport.calls[0]["url"], "https://api.deepseek.com/chat/completions")

    def test_client_explicitly_disables_default_thinking_for_deepseek_v4(self) -> None:
        transport = FakeTransport(["你好，英雄。"])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-flash",
            expressor_model="deepseek-v4-flash",
            thinking_enabled=False,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        client.create_chat_completion(
            model=config.action_model,
            messages=[],
            temperature=0.1,
        )

        self.assertEqual(
            transport.calls[0]["payload"]["thinking"],
            {"type": "disabled"},
        )

    def test_client_explicitly_disables_default_thinking_for_mimo_25(self) -> None:
        for model in ("mimo-v2.5", "mimo-v2.5-pro"):
            with self.subTest(model=model):
                transport = FakeTransport(["你好，英雄。"])
                config = LLMConfig(
                    api_base_url="https://api.xiaomimimo.com/v1",
                    api_key="test-key",
                    action_model=model,
                    expressor_model=model,
                    thinking_enabled=False,
                )
                client = OpenAICompatibleClient(config, transport=transport)

                client.create_chat_completion(
                    model=model,
                    messages=[],
                    temperature=0.1,
                    max_tokens=2500,
                )

                self.assertEqual(
                    transport.calls[0]["payload"]["thinking"],
                    {"type": "disabled"},
                )
                self.assertEqual(
                    transport.calls[0]["url"],
                    "https://api.xiaomimimo.com/v1/chat/completions",
                )
                self.assertEqual(
                    transport.calls[0]["payload"]["max_completion_tokens"],
                    2500,
                )
                self.assertNotIn("max_tokens", transport.calls[0]["payload"])

    def test_client_supports_per_request_thinking_override_and_telemetry(self) -> None:
        transport = FakeTransport(["沉浸表达", "规则表达"])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-flash",
            expressor_model="deepseek-v4-flash",
            thinking_enabled=False,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        client.create_chat_completion(
            model=config.action_model,
            messages=[],
            thinking_enabled=True,
            operation="immersive_expression",
        )
        client.create_chat_completion(
            model=config.action_model,
            messages=[],
            thinking_enabled=False,
            operation="rule_receipt",
        )

        self.assertEqual(
            transport.calls[0]["payload"]["thinking"],
            {"type": "enabled"},
        )
        self.assertEqual(
            transport.calls[1]["payload"]["thinking"],
            {"type": "disabled"},
        )
        self.assertTrue(client.recent_calls[-2]["thinking_enabled"])
        self.assertFalse(client.recent_calls[-1]["thinking_enabled"])

    def test_client_does_not_add_thinking_option_to_other_endpoints(self) -> None:
        transport = FakeTransport(["你好，英雄。"])
        config = LLMConfig(
            api_base_url="https://api.ai-pixel.online",
            api_key="test-key",
            action_model="gpt-5.6-luna",
            expressor_model="gpt-5.6-luna",
            thinking_enabled=False,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        client.create_chat_completion(
            model=config.action_model,
            messages=[],
            temperature=0.1,
        )

        self.assertNotIn("thinking", transport.calls[0]["payload"])

    def test_client_extracts_segmented_message_content(self) -> None:
        transport = FakeTransport(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "前半"},
                                    {"type": "text", "text": "后半"},
                                ],
                            }
                        }
                    ]
                }
            ]
        )
        config = LLMConfig(
            api_base_url="https://example.com",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(model=config.action_model, messages=[])

        self.assertEqual(content, "前半后半")

    def test_client_can_allow_missing_message_content_for_optional_prose(self) -> None:
        transport = FakeTransport([{"choices": [{"message": {"role": "assistant"}}]}])
        config = LLMConfig(
            api_base_url="https://example.com",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(model=config.action_model, messages=[], allow_empty=True)

        self.assertEqual(content, "")

    def test_client_retries_empty_success_response(self) -> None:
        transport = FakeTransport(
            [
                {"choices": [{"message": {"role": "assistant", "content": ""}}]},
                "重试后有内容。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://example.com",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
            reactive_recovery_max_retries=1,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(model=config.action_model, messages=[])

        self.assertEqual(content, "重试后有内容。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(client.last_recovery_attempts), 1)
        self.assertIn("empty assistant response", client.last_recovery_attempts[0].reason)
        self.assertTrue(client.recent_calls[0]["response_empty"])
        self.assertEqual(
            client.consume_call_diagnostics()["recovery_codes"],
            ["EMPTY_RESPONSE_RECOVERED"],
        )

    def test_deepseek_empty_json_retries_once_without_response_format(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": "deepseek-empty-response",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "只生成了推理",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 8,
                        "completion_tokens_details": {"reasoning_tokens": 8},
                    },
                },
                '{"decision":"silent","reason":"无需回应"}',
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-flash",
            expressor_model="deepseek-v4-flash",
            reasoning_effort="high",
            thinking_enabled=True,
            reactive_recovery_max_retries=5,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[ChatMessage(role="user", content="输出JSON")],
            response_format={"type": "json_object"},
            max_tokens=4096,
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )

        self.assertEqual(content, '{"decision":"silent","reason":"无需回应"}')
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            transport.calls[0]["payload"]["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("response_format", transport.calls[1]["payload"])
        self.assertEqual(
            [call["payload"]["thinking"] for call in transport.calls],
            [{"type": "disabled"}, {"type": "disabled"}],
        )
        self.assertTrue(
            all("reasoning_effort" not in call["payload"] for call in transport.calls)
        )
        self.assertEqual(
            [call["payload"]["max_tokens"] for call in transport.calls],
            [4096, 4096],
        )
        first_record = client.recent_calls[0]
        self.assertEqual(first_record["provider_response_id"], "deepseek-empty-response")
        self.assertEqual(first_record["finish_reason"], "stop")
        self.assertEqual(first_record["response_chars"], 0)
        self.assertEqual(first_record["reasoning_chars"], len("只生成了推理"))
        self.assertEqual(first_record["usage"]["reasoning_tokens"], 8)
        self.assertTrue(first_record["response_empty"])
        self.assertEqual(
            client.consume_call_diagnostics()["recovery_codes"],
            ["EMPTY_RESPONSE_RECOVERED", "RESPONSE_FORMAT_DOWNGRADED"],
        )

    def test_deepseek_repeated_empty_json_stops_after_one_fallback(self) -> None:
        empty = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }
            ]
        }
        transport = FakeTransport([empty, empty, "must-not-be-consumed"])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-flash",
            expressor_model="deepseek-v4-flash",
            reactive_recovery_max_retries=5,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        with self.assertRaises(LLMEmptyResponseError):
            client.create_chat_completion(
                model=config.action_model,
                messages=[ChatMessage(role="user", content="输出JSON")],
                response_format={"type": "json_object"},
                thinking_enabled=False,
                max_recovery_retries=1,
                retry_without_response_format_on_empty=True,
            )

        self.assertEqual(len(transport.calls), 2)
        self.assertNotIn("response_format", transport.calls[1]["payload"])

    def test_client_extracts_responses_style_output_text(self) -> None:
        transport = FakeTransport([{"output_text": "Responses 风格文本"}])
        config = LLMConfig(
            api_base_url="https://example.com",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(model=config.action_model, messages=[])

        self.assertEqual(content, "Responses 风格文本")

    def test_deepseek_base_url_uses_documented_chat_completions_path(self) -> None:
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )

        self.assertEqual(config.chat_completions_url(), "https://api.deepseek.com/chat/completions")

    def test_config_builds_distinct_primary_and_backup_completion_urls(self) -> None:
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
            backup_api_base_urls=("https://backup.example/v1", "https://primary.example"),
        )

        self.assertEqual(
            config.chat_completions_urls(),
            (
                "https://primary.example/v1/chat/completions",
                "https://backup.example/v1/chat/completions",
            ),
        )

    def test_client_reactive_compacts_and_retries_context_errors(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=413, body='{"error":{"code":"request_too_large"}}'),
                "压缩后成功。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
            reactive_recovery_target_chars=1200,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[
                ChatMessage(role="system", content="稳定系统提示"),
                ChatMessage(role="user", content="开头" + ("很长的动态上下文" * 600) + "最新玩家输入"),
            ],
        )

        self.assertEqual(content, "压缩后成功。")
        self.assertEqual(len(transport.calls), 2)
        retry_messages = transport.calls[1]["payload"]["messages"]
        self.assertEqual(retry_messages[0]["content"], "稳定系统提示")
        self.assertIn("错误恢复重试", retry_messages[1]["content"])
        self.assertIn("上下文折叠", retry_messages[1]["content"])
        self.assertIn("最新玩家输入", retry_messages[1]["content"])
        self.assertEqual(len(client.last_recovery_attempts), 1)
        self.assertEqual(
            client.consume_call_diagnostics()["recovery_codes"],
            ["CONTEXT_COMPACTED"],
        )

    def test_client_does_not_retry_non_recoverable_errors(self) -> None:
        transport = FakeTransport([RuntimeError("auth failed")])
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)

        with self.assertRaises(RuntimeError):
            client.create_chat_completion(
                model=config.action_model,
                messages=[ChatMessage(role="user", content="你好")],
            )

        self.assertEqual(len(transport.calls), 1)

    def test_client_retries_transient_upstream_error_without_compacting_prompt(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=503, body='{"error":{"message":"temporarily unavailable"}}'),
                "恢复成功。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
        )
        client = OpenAICompatibleClient(config, transport=transport)
        messages = [ChatMessage(role="user", content="保持原样的玩家输入")]

        content = client.create_chat_completion(model=config.action_model, messages=messages)

        self.assertEqual(content, "恢复成功。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["payload"]["messages"][0]["content"], "保持原样的玩家输入")
        self.assertEqual(len(client.last_recovery_attempts), 1)

    def test_client_switches_to_backup_endpoint_after_transient_error(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=502, body='{"error":{"message":"upstream error"}}'),
                "备用端点恢复成功。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
            backup_api_base_urls=("https://backup.example",),
            reactive_recovery_max_retries=1,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[ChatMessage(role="user", content="保持原样")],
        )

        self.assertEqual(content, "备用端点恢复成功。")
        self.assertEqual(transport.calls[0]["url"], "https://primary.example/v1/chat/completions")
        self.assertEqual(transport.calls[1]["url"], "https://backup.example/v1/chat/completions")
        self.assertIn("FU-GM/1.0", transport.calls[1]["headers"]["User-Agent"])
        self.assertEqual(client.recent_calls[0]["endpoint"], transport.calls[0]["url"])
        self.assertEqual(client.recent_calls[1]["endpoint"], transport.calls[1]["url"])

    def test_client_switches_endpoint_when_account_group_does_not_support_model(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(
                    status_code=404,
                    body=(
                        '{"error":{"message":"Model \\"gpt-5.6-luna\\" is not supported '
                        'by any configured account in this group"}}'
                    ),
                ),
                '{"approved":true}',
            ]
        )
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="gpt-5.6-luna",
            expressor_model="gpt-5.6-luna",
            backup_api_base_urls=("https://backup.example",),
            reactive_recovery_max_retries=1,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[ChatMessage(role="user", content="只输出JSON")],
        )

        self.assertEqual(content, '{"approved":true}')
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            transport.calls[1]["url"],
            "https://backup.example/v1/chat/completions",
        )

    def test_client_does_not_retry_an_ordinary_http_404(self) -> None:
        transport = FakeTransport(
            [LLMHTTPError(status_code=404, body='{"error":{"message":"route not found"}}')]
        )
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="gpt-5.6-luna",
            expressor_model="gpt-5.6-luna",
            backup_api_base_urls=("https://backup.example",),
            reactive_recovery_max_retries=2,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(
                model=config.action_model,
                messages=[ChatMessage(role="user", content="你好")],
            )

        self.assertEqual(len(transport.calls), 1)

    def test_luna_retries_once_without_response_format_before_switching_endpoint(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=502, body='{"error":{"message":"upstream error"}}'),
                '{"route":"game"}',
            ]
        )
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="gpt-5.6-luna",
            expressor_model="gpt-5.6-luna",
            backup_api_base_urls=("https://backup.example",),
            reactive_recovery_max_retries=1,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        content = client.create_chat_completion(
            model=config.action_model,
            messages=[ChatMessage(role="user", content="只输出 JSON")],
            response_format={"type": "json_object"},
        )

        self.assertEqual(content, '{"route":"game"}')
        self.assertIn("response_format", transport.calls[0]["payload"])
        self.assertNotIn("response_format", transport.calls[1]["payload"])
        self.assertEqual(transport.calls[1]["url"], transport.calls[0]["url"])
        self.assertIn("已移除 response_format", client.last_recovery_attempts[0].reason)
        self.assertEqual(
            client.consume_call_diagnostics()["recovery_codes"],
            ["RESPONSE_FORMAT_DOWNGRADED"],
        )

    def test_client_backs_off_only_after_all_endpoints_fail(self) -> None:
        failure = LLMHTTPError(
            status_code=502,
            body='{"error":{"message":"upstream error"}}',
        )
        transport = FakeTransport([failure, failure, failure, "恢复成功。"])
        config = LLMConfig(
            api_base_url="https://primary.example",
            api_key="test-key",
            action_model="model",
            expressor_model="model",
            backup_api_base_urls=("https://backup.example",),
            reactive_recovery_max_retries=3,
        )
        client = OpenAICompatibleClient(config, transport=transport)

        with patch("fu_gm.llm_client.time.sleep") as sleep:
            content = client.create_chat_completion(
                model=config.action_model,
                messages=[ChatMessage(role="user", content="保持原样")],
            )

        self.assertEqual(content, "恢复成功。")
        self.assertEqual(
            [item["url"] for item in transport.calls],
            [
                "https://primary.example/v1/chat/completions",
                "https://backup.example/v1/chat/completions",
                "https://primary.example/v1/chat/completions",
                "https://backup.example/v1/chat/completions",
            ],
        )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    def test_client_retries_remote_disconnect_without_compacting_prompt(self) -> None:
        transport = FakeTransport(
            [
                http.client.RemoteDisconnected("Remote end closed connection without response"),
                "连接恢复。",
            ]
        )
        config = LLMConfig(
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            action_model="deepseek-v4-pro",
            expressor_model="deepseek-v4-pro",
            reactive_recovery_max_retries=1,
        )
        client = OpenAICompatibleClient(config, transport=transport)
        messages = [ChatMessage(role="user", content="保持原样的动作请求")]

        content = client.create_chat_completion(model=config.action_model, messages=messages)

        self.assertEqual(content, "连接恢复。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["payload"]["messages"][0]["content"], "保持原样的动作请求")
        self.assertEqual(len(client.last_recovery_attempts), 1)

    def test_provider_circuit_opens_after_all_endpoints_fail_and_fast_fails(self) -> None:
        failure = LLMHTTPError(
            status_code=503,
            body='{"error":{"message":"temporarily unavailable"}}',
        )
        transport = FakeTransport([failure, failure])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                backup_api_base_urls=("https://backup.example",),
                api_key="test-key",
                action_model="model",
                expressor_model="model",
                reactive_recovery_max_retries=1,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
            circuit_failure_threshold=1,
            circuit_cooldown_seconds=30,
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(
                model="model",
                messages=[ChatMessage(role="user", content="first")],
            )
        with self.assertRaises(LLMProviderCircuitOpen):
            client.create_chat_completion(
                model="model",
                messages=[ChatMessage(role="user", content="second")],
            )

        self.assertEqual(len(transport.calls), 2)
        circuit = client.telemetry_payload()["circuit_breaker"]
        self.assertEqual(circuit["open_count"], 1)
        self.assertEqual(circuit["circuits"][0]["model"], "model")

    def test_provider_circuit_does_not_open_when_backup_recovers(self) -> None:
        transport = FakeTransport(
            [
                LLMHTTPError(status_code=503, body="temporarily unavailable"),
                "backup ok",
            ]
        )
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                backup_api_base_urls=("https://backup.example",),
                api_key="test-key",
                action_model="model",
                expressor_model="model",
                reactive_recovery_max_retries=1,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
        )

        content = client.create_chat_completion(
            model="model",
            messages=[ChatMessage(role="user", content="hello")],
        )

        self.assertEqual(content, "backup ok")
        self.assertEqual(client.circuit_breaker_payload()["open_count"], 0)

    def test_provider_circuit_half_open_probe_recovers_after_cooldown(self) -> None:
        clock = [100.0]
        failure = LLMHTTPError(status_code=503, body="temporarily unavailable")
        transport = FakeTransport([failure, "probe ok", "normal ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                api_key="test-key",
                action_model="model",
                expressor_model="model",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
            circuit_failure_threshold=1,
            circuit_cooldown_seconds=10,
            monotonic=lambda: clock[0],
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(model="model", messages=[])
        with self.assertRaises(LLMProviderCircuitOpen):
            client.create_chat_completion(model="model", messages=[])
        clock[0] = 111.0
        self.assertEqual(client.create_chat_completion(model="model", messages=[]), "probe ok")
        self.assertEqual(client.create_chat_completion(model="model", messages=[]), "normal ok")

        circuit = client.circuit_breaker_payload()
        self.assertEqual(circuit["open_count"], 0)
        self.assertEqual(circuit["circuits"][0]["state"], "closed")

    def test_provider_circuit_uses_bounded_exponential_cooldown_after_failed_probes(self) -> None:
        clock = [100.0]
        failure = LLMHTTPError(status_code=503, body="temporarily unavailable")
        transport = FakeTransport([failure, failure, failure])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                api_key="test-key",
                action_model="model",
                expressor_model="model",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
            circuit_failure_threshold=1,
            circuit_cooldown_seconds=10,
            circuit_max_cooldown_seconds=25,
            monotonic=lambda: clock[0],
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(model="model", messages=[])
        self.assertEqual(
            client.circuit_breaker_payload()["circuits"][0]["retry_after_seconds"],
            10.0,
        )
        clock[0] = 111.0
        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(model="model", messages=[])
        self.assertEqual(
            client.circuit_breaker_payload()["circuits"][0]["retry_after_seconds"],
            20.0,
        )
        clock[0] = 132.0
        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(model="model", messages=[])
        self.assertEqual(
            client.circuit_breaker_payload()["circuits"][0]["retry_after_seconds"],
            25.0,
        )

    def test_provider_circuit_keys_are_isolated_by_model(self) -> None:
        failure = LLMHTTPError(status_code=503, body="temporarily unavailable")
        transport = FakeTransport([failure, "other model ok"])
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                api_key="test-key",
                action_model="model-a",
                expressor_model="model-b",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
            circuit_failure_threshold=1,
        )

        with self.assertRaises(LLMHTTPError):
            client.create_chat_completion(model="model-a", messages=[])
        self.assertEqual(
            client.create_chat_completion(model="model-b", messages=[]),
            "other model ok",
        )

    def test_provider_circuit_allows_only_one_half_open_probe(self) -> None:
        clock = [100.0]

        class BlockingProbeTransport:
            def __init__(self) -> None:
                self.calls = 0
                self.started = threading.Event()
                self.release = threading.Event()

            def post_json(self, url, headers, payload, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("open circuit")
                self.started.set()
                self.release.wait(timeout=1)
                return {"choices": [{"message": {"content": "probe ok"}}]}

        transport = BlockingProbeTransport()
        client = OpenAICompatibleClient(
            LLMConfig(
                api_base_url="https://primary.example",
                api_key="test-key",
                action_model="model",
                expressor_model="model",
                reactive_recovery_enabled=False,
                reactive_recovery_max_retries=0,
            ),
            transport=transport,
            circuit_breaker_enabled=True,
            circuit_failure_threshold=1,
            circuit_cooldown_seconds=10,
            monotonic=lambda: clock[0],
        )
        with self.assertRaises(TimeoutError):
            client.create_chat_completion(model="model", messages=[])
        clock[0] = 111.0
        probe_result: list[str] = []

        def run_probe() -> None:
            probe_result.append(
                client.create_chat_completion(model="model", messages=[])
            )

        thread = threading.Thread(target=run_probe)
        thread.start()
        self.assertTrue(transport.started.wait(timeout=1))
        with self.assertRaises(LLMProviderCircuitOpen):
            client.create_chat_completion(model="model", messages=[])
        transport.release.set()
        thread.join(timeout=1)

        self.assertEqual(probe_result, ["probe ok"])
        self.assertEqual(transport.calls, 2)


if __name__ == "__main__":
    unittest.main()
