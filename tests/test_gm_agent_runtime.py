from __future__ import annotations

import os
from unittest.mock import patch

from fu_gm.components.gm_agent_runtime import GMAgentRuntime
from fu_gm.gm_tool_contracts import GMToolRegistry


def _core_env() -> dict[str, str]:
    return {
        "FU_GM_DOTENV_PATH": "/dev/null",
        "FU_GM_API_BASE_URL": "https://primary.test/v1",
        "FU_GM_BACKUP_API_BASE_URL": "https://backup.test/v1",
        "FU_GM_API_KEY": "test-key",
        "FU_GM_ACTION_MODEL": "gpt-5.6-luna",
        "FU_GM_EXPRESSOR_MODEL": "gpt-5.6-luna",
        "FU_GM_TIMEOUT_SECONDS": "120",
    }


def test_default_core_runtime_tries_each_endpoint_then_one_transient_retry() -> None:
    with patch.dict(os.environ, _core_env(), clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.tool_agent is not None
    assert runtime.llm_client is not None
    assert runtime.tool_agent.timeout_seconds == 120.0
    assert runtime.llm_client.config.reactive_recovery_max_retries == 2
    assert runtime.llm_client.config.endpoint_attempt_timeout_seconds == 60.0
    assert (
        runtime.llm_client.config.endpoint_attempt_timeout_seconds
        <= runtime.tool_agent.timeout_seconds
    )
    assert runtime.llm_client.circuit_breaker_enabled is True
    assert runtime.llm_client.circuit_failure_threshold == 1
    assert runtime.llm_client.circuit_cooldown_seconds == 30.0
    assert runtime.llm_client.circuit_max_cooldown_seconds == 300.0


def test_default_core_runtime_retries_one_transient_failure_without_backup() -> None:
    env = _core_env()
    env.pop("FU_GM_BACKUP_API_BASE_URL")
    env["FU_GM_DOTENV_PATH"] = "/dev/null"
    with patch.dict(os.environ, env, clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.llm_client is not None
    assert runtime.llm_client.config.reactive_recovery_enabled is True
    assert runtime.llm_client.config.reactive_recovery_max_retries == 1


def test_default_terra_runtime_keeps_a_full_transaction_budget() -> None:
    env = _core_env()
    env["FU_GM_ACTION_MODEL"] = "gpt-5.6-terra"
    env["FU_GM_EXPRESSOR_MODEL"] = "gpt-5.6-terra"
    env["FU_GM_DOTENV_PATH"] = "/dev/null"
    with patch.dict(os.environ, env, clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.tool_agent is not None
    assert runtime.llm_client is not None
    assert runtime.tool_agent.timeout_seconds == 120.0
    assert runtime.llm_client.config.endpoint_attempt_timeout_seconds == 60.0


def test_core_runtime_allows_explicit_retry_and_timeout_overrides() -> None:
    env = {
        **_core_env(),
        "FU_GM_CORE_GM_TIMEOUT_SECONDS": "100",
        "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS": "18",
        "FU_GM_CORE_GM_RECOVERY_MAX_RETRIES": "3",
        "FU_GM_CORE_GM_CIRCUIT_BREAKER_ENABLED": "0",
        "FU_GM_CORE_GM_CIRCUIT_FAILURE_THRESHOLD": "2",
        "FU_GM_CORE_GM_CIRCUIT_COOLDOWN_SECONDS": "45",
        "FU_GM_CORE_GM_CIRCUIT_MAX_COOLDOWN_SECONDS": "180",
    }
    with patch.dict(os.environ, env, clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.tool_agent is not None
    assert runtime.llm_client is not None
    assert runtime.tool_agent.timeout_seconds == 100.0
    assert runtime.llm_client.config.endpoint_attempt_timeout_seconds == 18.0
    assert runtime.llm_client.config.reactive_recovery_max_retries == 3
    assert runtime.llm_client.circuit_breaker_enabled is False
    assert runtime.llm_client.circuit_failure_threshold == 2
    assert runtime.llm_client.circuit_cooldown_seconds == 45.0
    assert runtime.llm_client.circuit_max_cooldown_seconds == 180.0


def test_default_runtime_routes_every_core_role_to_official_deepseek() -> None:
    with patch.dict(
        os.environ,
        {
            "FU_GM_DOTENV_PATH": "/dev/null",
            "FU_GM_API_KEY": "test-key",
        },
        clear=True,
    ):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.llm_client is not None
    assert runtime.tool_agent is not None
    assert runtime.llm_client.config.api_base_url == "https://api.deepseek.com"
    assert runtime.llm_client.config.action_model == "deepseek-v4-flash"
    assert runtime.llm_client.config.thinking_enabled is False
    assert runtime.llm_client.config.response_format_enabled is True
    assert runtime.llm_client.config.backup_api_base_urls == ()
    assert runtime.tool_agent.model == "deepseek-v4-flash"
    assert runtime.tool_agent._decision_requester.repair_model == "deepseek-v4-flash"
    assert runtime.tool_agent.reply_grounding_verifier is not None
    assert runtime.tool_agent.reply_grounding_verifier.model == "deepseek-v4-flash"


def test_protocol_repair_defaults_to_tool_model_not_global_action_model() -> None:
    env = {
        "FU_GM_DOTENV_PATH": "/dev/null",
        "FU_GM_API_BASE_URL": "https://api.deepseek.com",
        "FU_GM_API_KEY": "test-key",
        "FU_GM_ACTION_MODEL": "gpt-5.6-terra",
        "FU_GM_CORE_GM_MODEL": "deepseek-v4-flash",
        "FU_GM_TOOL_AGENT_MODEL": "deepseek-v4-flash",
    }
    with patch.dict(os.environ, env, clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.tool_agent is not None
    assert runtime.tool_agent.model == "deepseek-v4-flash"
    assert runtime.tool_agent._decision_requester.repair_model == "deepseek-v4-flash"
