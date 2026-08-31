from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_image_manager import WorldMapImageManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.map_renderer import NortantisMapRenderer
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.npc_blueprint_designer import NPCBlueprintDesigner
from fu_gm.components.npc_voice_renderer import NPCVoiceRenderer
from fu_gm.components.world_state import WorldState
from fu_gm.config import (
    DEFAULT_LLM_MODEL,
    ImageGenerationConfig,
    LLMConfig,
    parse_api_base_urls,
    resolve_model_api_key,
    uses_high_latency_model,
)
from fu_gm.expressor import Expressor, LLMExpressor
from fu_gm.image_client import ImageGenerationClient
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.llm_client_bundle import require_test_llm_bundle


def build_app(
    *,
    use_llm: bool = True,
    seed: int | None = None,
    gm_style_prompt: str = "",
    deepseek_roleplay_mode: str = "default",
    test_llm_bundle: Any | None = None,
) -> SceneOrchestrator:
    """构建一个空白 FU-GM 应用实例。

    HTTP 服务、AstrBot 桥接和测试统一使用这个工厂。它不会预置角色、场景或
    战斗；生产调用不传 seed，骰子由系统熵初始化，需要可复现结果的测试必须
    显式传入固定 seed。
    """

    test_bundle = require_test_llm_bundle(test_llm_bundle)
    characters = CharacterManager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    scene_manager = SceneManager()
    world_state = WorldState()
    world_map = WorldMapManager(world_state)
    rules = RulesEngine(seed=seed)
    travel = TravelManager(rules)
    dungeon = DungeonManager(clocks, rules)
    rest = RestManager(characters, clocks)
    session_zero = SessionZeroManager(world_state)
    image_config = (
        ImageGenerationConfig(
            api_base_url="",
            api_key="",
            model="test-only",
            enabled=False,
        )
        if test_bundle is not None
        else ImageGenerationConfig.from_env()
    )
    world_map_image_manager = WorldMapImageManager(renderer=NortantisMapRenderer())
    if test_bundle is None and os.environ.get(
        "FU_GM_WORLD_MAP_RENDERER",
        "nortantis",
    ).strip().lower() in {"image", "gpt-image", "gpt_image"}:
        world_map_image_manager = (
            WorldMapImageManager(ImageGenerationClient(image_config), image_config)
            if image_config.usable()
            else None
        )

    interceptor = ActionInterceptor(
        rules_engine=rules,
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
        scene_manager=scene_manager,
    )

    fallback_expressor = Expressor()
    npc_combat_rules = NPCCombatRules(characters, conflict, world_state)
    expressor = fallback_expressor
    gm_llm_client = None
    gm_llm_model = ""
    creative_client = None
    creative_model = ""
    npc_design_client = None
    npc_design_model = ""
    npc_voice_renderer = None

    llm_config = (
        LLMConfig.for_test_client(test_bundle.model)
        if test_bundle is not None
        else LLMConfig.from_env()
    )
    if use_llm and test_bundle is not None:
        gm_llm_client = test_bundle.pacing
        gm_llm_model = str(test_bundle.model or DEFAULT_LLM_MODEL).strip()
        creative_client = test_bundle.expressor
        creative_model = gm_llm_model
        expressor = LLMExpressor(
            client=test_bundle.expressor,
            model=gm_llm_model,
            fallback=fallback_expressor,
            allow_fallback=False,
            gm_personality_prompt=gm_style_prompt,
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            rule_result_prose_enabled=_expressor_rule_result_prose_enabled(
                gm_llm_model
            ),
        )
        npc_design_client = test_bundle.npc_design
        npc_design_model = gm_llm_model
        npc_voice_renderer = NPCVoiceRenderer(
            client=test_bundle.expressor,
            model=gm_llm_model,
            audit_client=test_bundle.core,
            audit_model=gm_llm_model,
            audit_mode=os.environ.get(
                "FU_GM_NPC_VOICE_AUDIT_MODE",
                "off",
            ),
            enabled=_env_flag("FU_GM_NPC_VOICE_ENABLED", default=True),
            max_output_tokens=int(
                os.environ.get("FU_GM_NPC_VOICE_MAX_OUTPUT_TOKENS", "900")
            ),
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            render_timeout_seconds=float(
                os.environ.get("FU_GM_NPC_VOICE_TIMEOUT_SECONDS", "10")
            ),
            audit_timeout_seconds=float(
                os.environ.get("FU_GM_NPC_VOICE_AUDIT_TIMEOUT_SECONDS", "5")
            ),
            allow_fallback=_env_flag(
                "FU_GM_NPC_VOICE_ALLOW_FALLBACK",
                default=True,
            ),
        )
    elif use_llm and llm_config.api_key:
        action_config = _component_llm_config(llm_config, "ACTION")
        expressor_config = _component_llm_config(llm_config, "EXPRESSOR")
        creative_config = _component_llm_config(expressor_config, "CREATIVE")
        npc_design_config = _component_llm_config(llm_config, "NPC_DESIGN")
        llm_client = OpenAICompatibleClient(action_config)
        gm_llm_client = llm_client
        gm_llm_model = action_config.action_model
        expressor_client = llm_client if expressor_config == action_config else OpenAICompatibleClient(expressor_config)
        creative_client = (
            expressor_client
            if creative_config == expressor_config
            else OpenAICompatibleClient(creative_config)
        )
        creative_model = creative_config.action_model
        expressor = LLMExpressor(
            client=expressor_client,
            model=expressor_config.expressor_model,
            fallback=fallback_expressor,
            allow_fallback=False,
            gm_personality_prompt=gm_style_prompt,
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            rule_result_prose_enabled=_expressor_rule_result_prose_enabled(
                expressor_config.expressor_model
            ),
        )
        npc_design_client = (
            llm_client
            if npc_design_config == action_config
            else OpenAICompatibleClient(npc_design_config)
        )
        npc_design_model = npc_design_config.action_model
        npc_voice_config = _override_llm_config(
            expressor_config,
            prefix="FU_GM_NPC_VOICE_",
            override_model=True,
        )
        npc_voice_client = (
            expressor_client
            if npc_voice_config == expressor_config
            else OpenAICompatibleClient(npc_voice_config)
        )
        npc_voice_renderer = NPCVoiceRenderer(
            client=npc_voice_client,
            model=npc_voice_config.action_model,
            audit_client=llm_client,
            audit_model=action_config.action_model,
            audit_mode=os.environ.get(
                "FU_GM_NPC_VOICE_AUDIT_MODE",
                "off",
            ),
            enabled=_env_flag("FU_GM_NPC_VOICE_ENABLED", default=True),
            max_output_tokens=int(
                os.environ.get("FU_GM_NPC_VOICE_MAX_OUTPUT_TOKENS", "900")
            ),
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            render_timeout_seconds=float(
                os.environ.get("FU_GM_NPC_VOICE_TIMEOUT_SECONDS", "10")
            ),
            audit_timeout_seconds=float(
                os.environ.get("FU_GM_NPC_VOICE_AUDIT_TIMEOUT_SECONDS", "5")
            ),
            allow_fallback=_env_flag(
                "FU_GM_NPC_VOICE_ALLOW_FALLBACK",
                default=True,
            ),
        )
    npc_blueprint_designer = NPCBlueprintDesigner(
        world_state,
        client=npc_design_client,
        model=npc_design_model,
        current_scene_id=lambda: str(
            getattr(scene_manager.current_scene, "scene_id", "")
            or getattr(scene_manager.current_scene, "name", "")
            or ""
        ),
        max_workers=int(
            os.environ.get("FU_GM_NPC_BLUEPRINT_MAX_WORKERS", "1")
        ),
        background_defer_seconds=float(
            os.environ.get(
                "FU_GM_NPC_BLUEPRINT_BACKGROUND_DEFER_SECONDS",
                "0"
                if test_bundle is not None
                or npc_design_client is None
                or not npc_design_model
                else "20",
            )
        ),
    )
    return SceneOrchestrator(
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
        interceptor=interceptor,
        expressor=expressor,
        llm_client=gm_llm_client,
        llm_model=gm_llm_model,
        creative_client=creative_client,
        creative_model=creative_model,
        deepseek_roleplay_mode=deepseek_roleplay_mode,
        semantic_review_client=gm_llm_client,
        semantic_review_model=gm_llm_model,
        npc_combat_rules=npc_combat_rules,
        npc_blueprint_designer=npc_blueprint_designer,
        npc_voice_renderer=npc_voice_renderer,
        scene_manager=scene_manager,
        session_zero_manager=session_zero,
        rest_manager=rest,
        travel_manager=travel,
        dungeon_manager=dungeon,
        world_map_manager=world_map,
        world_map_image_manager=world_map_image_manager,
        gm_beat_timeout_seconds=float(
            os.environ.get(
                "FU_GM_BEAT_TIMEOUT_SECONDS",
                "120" if uses_high_latency_model(llm_config.expressor_model) else "90",
            )
        ),
        session_prep_timeout_seconds=float(
            os.environ.get(
                "FU_GM_SESSION_PREP_TIMEOUT_SECONDS",
                "600" if test_bundle is not None else "60",
            )
        ),
    )


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "disabled", "off"}


def _component_llm_config(config: LLMConfig, component: str) -> LLMConfig:
    """允许各专用 LLM 组件单独覆盖模型、端点和速度相关配置。

    工具智能体负责语义决策，Expressor 负责已结算结果的文字表现。
    未设置组件级变量时沿用全局配置。
    """

    return _override_llm_config(config, prefix=f"FU_GM_{component}_", override_model=True)


def _expressor_rule_result_prose_enabled(model: str) -> bool:
    """Keep DeepSeek focused on scene voice unless explicitly overridden."""

    configured = os.environ.get(
        "FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED",
        "",
    ).strip().lower()
    if configured:
        return configured not in {"0", "false", "no", "disabled", "off"}
    return not str(model or "").strip().lower().startswith("deepseek-v4")


def _override_llm_config(
    config: LLMConfig,
    *,
    prefix: str,
    override_model: bool,
    default_timeout: float | None = None,
) -> LLMConfig:
    """Apply one component's environment overrides without duplicating parsing rules."""

    api_base_url = os.environ.get(f"{prefix}API_BASE_URL", "").strip()
    api_key = os.environ.get(f"{prefix}API_KEY", "").strip()
    model = os.environ.get(f"{prefix}MODEL", "").strip() if override_model else ""
    selected_model = model or config.action_model
    default_api_key = config.api_key
    if selected_model != config.action_model:
        default_api_key = (
            os.environ.get("FU_GM_API_KEY", "").strip() or config.api_key
        )
    reasoning_effort = os.environ.get(f"{prefix}REASONING_EFFORT", "").strip()
    thinking_flag = os.environ.get(f"{prefix}THINKING", "").strip().lower()
    timeout = os.environ.get(f"{prefix}TIMEOUT_SECONDS", "").strip()
    endpoint_attempt_timeout = os.environ.get(
        f"{prefix}ENDPOINT_ATTEMPT_TIMEOUT_SECONDS",
        "",
    ).strip()
    backup_key_plural = f"{prefix}BACKUP_API_BASE_URLS"
    backup_key_single = f"{prefix}BACKUP_API_BASE_URL"
    backup_override_present = backup_key_plural in os.environ or backup_key_single in os.environ
    backup_raw = os.environ.get(backup_key_plural, os.environ.get(backup_key_single, ""))

    effective_api_base_url = (
        api_base_url.rstrip("/") if api_base_url else config.api_base_url
    )
    effective_api_key = resolve_model_api_key(
        selected_model,
        api_key or default_api_key,
    )
    provider_boundary_changed = bool(
        effective_api_base_url.rstrip("/") != config.api_base_url.rstrip("/")
        or effective_api_key != config.api_key
    )

    thinking_enabled = config.thinking_enabled
    if thinking_flag in {"on", "true", "1", "yes", "enabled"}:
        thinking_enabled = True
    elif thinking_flag in {"off", "false", "0", "no", "disabled"}:
        thinking_enabled = False

    return replace(
        config,
        api_base_url=effective_api_base_url,
        api_key=effective_api_key,
        action_model=model or config.action_model,
        expressor_model=model or config.expressor_model,
        reasoning_effort=reasoning_effort or config.reasoning_effort,
        thinking_enabled=thinking_enabled,
        timeout_seconds=(
            float(timeout)
            if timeout
            else default_timeout if default_timeout is not None else config.timeout_seconds
        ),
        endpoint_attempt_timeout_seconds=(
            float(endpoint_attempt_timeout)
            if endpoint_attempt_timeout
            else config.endpoint_attempt_timeout_seconds
        ),
        backup_api_base_urls=(
            parse_api_base_urls(backup_raw)
            if backup_override_present
            else ()
            if provider_boundary_changed
            else config.backup_api_base_urls
        ),
    )


def _session_zero_llm_config(config: LLMConfig) -> LLMConfig:
    default_timeout = 75.0 if uses_high_latency_model(config.action_model) else 35.0
    return _override_llm_config(
        config,
        prefix="FU_GM_SESSION_ZERO_",
        override_model=False,
        default_timeout=min(config.timeout_seconds, default_timeout),
    )
