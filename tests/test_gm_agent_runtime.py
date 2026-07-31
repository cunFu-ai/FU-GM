from __future__ import annotations

import os
from unittest.mock import patch

from fu_gm.components.gm_agent_runtime import GMAgentRuntime
from fu_gm.gm_tool_contracts import GMToolRegistry


def _core_env() -> dict[str, str]:
    return {
        "FU_GM_API_BASE_URL": "https://primary.test/v1",
        "FU_GM_BACKUP_API_BASE_URL": "https://backup.test/v1",
        "FU_GM_API_KEY": "test-key",
        "FU_GM_ACTION_MODEL": "gpt-5.6-luna",
        "FU_GM_EXPRESSOR_MODEL": "gpt-5.6-luna",
        "FU_GM_TIMEOUT_SECONDS": "120",
    }


def test_default_core_runtime_tries_each_endpoint_once_and_keeps_reply_budget() -> None:
    with patch.dict(os.environ, _core_env(), clear=True):
        runtime = GMAgentRuntime.build(
            registry=GMToolRegistry(),
            use_llm=True,
        )

    assert runtime.tool_agent is not None
    assert runtime.llm_client is not None
    assert runtime.tool_agent.timeout_seconds == 90.0
    assert runtime.llm_client.config.reactive_recovery_max_retries == 1
    assert runtime.llm_client.config.endpoint_attempt_timeout_seconds == 25.0
    assert runtime.llm_client.config.endpoint_attempt_timeout_seconds * 2 <= 50.0
    assert runtime.llm_client.circuit_breaker_enabled is True
    assert runtime.llm_client.circuit_failure_threshold == 1
    assert runtime.llm_client.circuit_cooldown_seconds == 30.0
    assert runtime.llm_client.circuit_max_cooldown_seconds == 300.0


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
