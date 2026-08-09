from __future__ import annotations

import os
from dataclasses import dataclass

from fu_gm.config import LLMConfig, resolve_model_api_key, uses_high_latency_model
from fu_gm.gm_tool_agent import LLMGMToolAgent
from fu_gm.gm_tool_contracts import GMToolRegistry
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.components.gm_reply_grounding_verifier import (
    GMReplyGroundingVerifier,
)


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
        # A normal request may require capability discovery, one state tool,
        # and a final natural-language response.  A 30-second *transaction*
        # budget can expire after a healthy first call, then hide an explicit
        # GM request when the second call is slow.  Keep the bounded 90-second
        # budget for every core model; per-endpoint limits still avoid hangs.
        default_agent_timeout = min(config.timeout_seconds, 90.0)
        agent_timeout = float(
            os.environ.get(
                "FU_GM_CORE_GM_TIMEOUT_SECONDS",
                str(default_agent_timeout),
            )
        )
        default_endpoint_attempt = min(
            25.0 if high_latency_model else 20.0,
            max(10.0, agent_timeout * 0.28),
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
        else:
            # Give every configured endpoint one chance, then allow one final
            # bounded retry for a transient 5xx/empty response.  Correctness is
            # preferable to dropping a table turn, while the shared operation
            # deadline still prevents an outage from retrying indefinitely.
            agent_retries = len(config.backup_api_base_urls) + 1

        agent_config = LLMConfig(
            api_base_url=config.api_base_url,
            api_key=resolve_model_api_key(
                core_model,
                (
                    config.api_key
                    if core_model == config.action_model
                    else os.environ.get("FU_GM_API_KEY", "").strip()
                    or config.api_key
                ),
            ),
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
            response_format_enabled=os.environ.get(
                "FU_GM_CORE_GM_RESPONSE_FORMAT_ENABLED",
                "1" if config.response_format_enabled else "0",
            ).lower()
            not in {"0", "false", "no", "disabled", "off"},
            prompt_cache_enabled=config.prompt_cache_enabled,
            prompt_cache_mode=config.prompt_cache_mode,
            prompt_cache_key_prefix=config.prompt_cache_key_prefix,
            prompt_cache_ttl=config.prompt_cache_ttl,
            reactive_recovery_enabled=agent_retries > 0,
            reactive_recovery_max_retries=agent_retries,
            reactive_recovery_target_chars=12000,
            allow_heuristic_fallback=False,
        )
        circuit_enabled = os.environ.get(
            "FU_GM_CORE_GM_CIRCUIT_BREAKER_ENABLED",
            "1",
        ).lower() not in {"0", "false", "no", "disabled"}
        circuit_failure_threshold = max(
            1,
            int(os.environ.get("FU_GM_CORE_GM_CIRCUIT_FAILURE_THRESHOLD", "1")),
        )
        circuit_cooldown_seconds = max(
            1.0,
            float(os.environ.get("FU_GM_CORE_GM_CIRCUIT_COOLDOWN_SECONDS", "30")),
        )
        circuit_max_cooldown_seconds = max(
            circuit_cooldown_seconds,
            float(os.environ.get("FU_GM_CORE_GM_CIRCUIT_MAX_COOLDOWN_SECONDS", "300")),
        )
        client = OpenAICompatibleClient(
            agent_config,
            circuit_breaker_enabled=circuit_enabled,
            circuit_failure_threshold=circuit_failure_threshold,
            circuit_cooldown_seconds=circuit_cooldown_seconds,
            circuit_max_cooldown_seconds=circuit_max_cooldown_seconds,
        )
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
            reply_grounding_verifier=(
                GMReplyGroundingVerifier(
                    client,
                    model=(
                        os.environ.get(
                            "FU_GM_REPLY_GROUNDING_MODEL",
                            "",
                        ).strip()
                        or tool_model
                    ),
                    max_output_tokens=max(
                        256,
                        int(
                            os.environ.get(
                                "FU_GM_REPLY_GROUNDING_MAX_TOKENS",
                                "900",
                            )
                        ),
                    ),
                )
                if os.environ.get(
                    "FU_GM_REPLY_GROUNDING_ENABLED",
                    "1",
                ).lower()
                not in {"0", "false", "no", "disabled", "off"}
                else None
            ),
        )
        return cls(
            llm_client=client,
            llm_model=core_model,
            tool_agent=tool_agent,
        )
