from __future__ import annotations

import os
from dataclasses import dataclass

from fu_gm.config import LLMConfig, uses_high_latency_model
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolRegistry
from fu_gm.llm_client import OpenAICompatibleClient


@dataclass(frozen=True)
class GMAgentRuntime:
    """Transport-independent composition root for the single core GM agent."""

    llm_client: OpenAICompatibleClient | None = None
    llm_model: str = ""
    tool_agent: LLMGMToolAgent | None = None

    @classmethod
    def build(
        cls,
        *,
        registry: GMToolRegistry,
        use_llm: bool,
        gm_personality_prompt: str = "",
    ) -> "GMAgentRuntime":
        config = LLMConfig.from_env()
        agent_enabled = os.environ.get(
            "FU_GM_CORE_AGENT_ENABLED",
            "1",
        ).lower() not in {"0", "false", "no", "disabled"}
        if not (use_llm and config.api_key and agent_enabled):
            return cls()

        core_model = (
            os.environ.get("FU_GM_CORE_GM_MODEL", "").strip()
            or config.action_model
        )
        high_latency_model = uses_high_latency_model(core_model)
        default_agent_timeout = min(
            config.timeout_seconds,
            90.0 if high_latency_model else 30.0,
        )
        agent_timeout = float(
            os.environ.get(
                "FU_GM_CORE_GM_TIMEOUT_SECONDS",
                str(default_agent_timeout),
            )
        )
        default_endpoint_attempt = min(
            45.0 if high_latency_model else 14.0,
            max(5.0, agent_timeout * 0.48),
        )
        endpoint_attempt_timeout = float(
            os.environ.get(
                "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS",
                str(default_endpoint_attempt),
            )
        )
        retry_override = os.environ.get(
            "FU_GM_CORE_GM_RECOVERY_MAX_RETRIES",
            "",
        ).strip()
        if retry_override:
            agent_retries = max(0, int(retry_override))
        elif config.backup_api_base_urls:
            agent_retries = max(3, int(config.reactive_recovery_max_retries))
        else:
            agent_retries = 0

        agent_config = LLMConfig(
            api_base_url=config.api_base_url,
            api_key=config.api_key,
            action_model=core_model,
            expressor_model=core_model,
            backup_api_base_urls=config.backup_api_base_urls,
            timeout_seconds=max(5.0, agent_timeout),
            endpoint_attempt_timeout_seconds=max(1.0, endpoint_attempt_timeout),
            reasoning_effort=os.environ.get(
                "FU_GM_CORE_GM_REASONING_EFFORT",
                "",
            ).strip(),
            thinking_enabled=os.environ.get(
                "FU_GM_CORE_GM_THINKING",
                "",
            ).lower() in {"1", "true", "yes", "enabled"},
            reactive_recovery_enabled=bool(config.backup_api_base_urls),
            reactive_recovery_max_retries=agent_retries,
            reactive_recovery_target_chars=12000,
            allow_heuristic_fallback=False,
        )
        client = OpenAICompatibleClient(agent_config)
        tools_enabled = os.environ.get(
            "FU_GM_AGENT_TOOLS_ENABLED",
            "1",
        ).lower() not in {"0", "false", "no", "disabled"}
        if not tools_enabled:
            return cls(
                llm_client=client,
                llm_model=core_model,
            )

        tool_model = (
            os.environ.get("FU_GM_TOOL_AGENT_MODEL", "").strip()
            or core_model
        )
        tool_agent = LLMGMToolAgent(
            client,
            model=tool_model,
            registry=registry,
            protocol_repair_model=(
                os.environ.get(
                    "FU_GM_TOOL_PROTOCOL_REPAIR_MODEL",
                    "",
                ).strip()
                or config.action_model
                or tool_model
            ),
            max_iterations=max(
                2,
                int(os.environ.get("FU_GM_TOOL_AGENT_MAX_ITERATIONS", "8")),
            ),
            parse_retries=max(
                0,
                int(os.environ.get("FU_GM_TOOL_AGENT_PARSE_RETRIES", "3")),
            ),
            max_output_tokens=max(
                512,
                int(os.environ.get("FU_GM_TOOL_AGENT_MAX_TOKENS", "4096")),
            ),
            timeout_seconds=float(
                os.environ.get(
                    "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS",
                    str(client.config.timeout_seconds),
                )
            ),
            gm_personality_prompt=gm_personality_prompt,
        )
        return cls(
            llm_client=client,
            llm_model=core_model,
            tool_agent=tool_agent,
        )
