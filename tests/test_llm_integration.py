import json
import http.client
import os
import threading
import time
import unittest
from unittest.mock import patch

from fu_gm.app_factory import _component_llm_config, _session_zero_llm_config
from fu_gm.config import LLMConfig
from fu_gm.expressor import LLMExpressor
from fu_gm.llm_client import (
    ChatMessage,
    LLMDeadlineExceeded,
    LLMHTTPError,
    LLMProviderCircuitOpen,
    OpenAICompatibleClient,
)
from fu_gm.models import Action, ActionResolution, ActionType, RollOutcome


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


class LLMIntegrationTests(unittest.TestCase):
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

    def test_component_llm_config_can_split_expressor_endpoint(self) -> None:
        old_env = os.environ.copy()
        try:
            os.environ.pop("FU_GM_ACTION_MODEL", None)
            os.environ["FU_GM_EXPRESSOR_API_BASE_URL"] = "https://www.moxin.online/v1"
            os.environ["FU_GM_EXPRESSOR_API_KEY"] = "expressor-key"
            os.environ["FU_GM_EXPRESSOR_MODEL"] = "claude-opus-4-6"
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
        finally:
            os.environ.clear()
            os.environ.update(old_env)

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
