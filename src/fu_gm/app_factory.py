from __future__ import annotations

import os
from dataclasses import replace

from fu_gm.action_brain import HeuristicActionBrain, LLMActionBrain
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
from fu_gm.components.world_state import WorldState
from fu_gm.config import ImageGenerationConfig, LLMConfig
from fu_gm.expressor import Expressor, LLMExpressor
from fu_gm.image_client import ImageGenerationClient
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.npc_director import HeuristicNPCDirector, LLMNPCDirector
from fu_gm.scene_orchestrator import SceneOrchestrator
from fu_gm.session_zero_facilitator import HeuristicSessionZeroFacilitator, LLMSessionZeroFacilitator


def build_app(
    *,
    use_llm: bool = True,
    seed: int = 0,
    gm_style_prompt: str = "",
    deepseek_roleplay_mode: str = "default",
) -> SceneOrchestrator:
    """构建一个空白 FU-GM 应用实例。

    HTTP 服务、AstrBot 桥接和测试都应该优先使用这个工厂；demo 内容放在 main.py，
    避免真实群聊战役一启动就进入示例战斗。
    """

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
    image_config = ImageGenerationConfig.from_env()
    world_map_image_manager = WorldMapImageManager(renderer=NortantisMapRenderer())
    if os.environ.get("FU_GM_WORLD_MAP_RENDERER", "nortantis").strip().lower() in {"image", "gpt-image", "gpt_image"}:
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
    )

    fallback_action_brain = HeuristicActionBrain()
    fallback_expressor = Expressor()
    fallback_npc_director = HeuristicNPCDirector(characters, conflict, world_state)
    fallback_session_zero = HeuristicSessionZeroFacilitator()
    action_brain = fallback_action_brain
    expressor = fallback_expressor
    npc_director = fallback_npc_director
    session_zero_facilitator = fallback_session_zero

    llm_config = LLMConfig.from_env()
    allow_heuristic_fallback = llm_config.allow_heuristic_fallback
    if use_llm and llm_config.api_key:
        action_config = _component_llm_config(llm_config, "ACTION")
        expressor_config = _component_llm_config(llm_config, "EXPRESSOR")
        llm_client = OpenAICompatibleClient(action_config)
        expressor_client = llm_client if expressor_config == action_config else OpenAICompatibleClient(expressor_config)
        session_zero_model = os.environ.get("FU_GM_SESSION_ZERO_MODEL", "").strip() or llm_config.action_model
        session_zero_config = _session_zero_llm_config(llm_config)
        session_zero_client = llm_client if session_zero_config == action_config else OpenAICompatibleClient(session_zero_config)
        action_brain = LLMActionBrain(
            client=llm_client,
            model=action_config.action_model,
            fallback=fallback_action_brain,
            allow_fallback=allow_heuristic_fallback,
        )
        expressor = LLMExpressor(
            client=expressor_client,
            model=expressor_config.expressor_model,
            fallback=fallback_expressor,
        )
        npc_director = LLMNPCDirector(
            client=llm_client,
            model=action_config.action_model,
            character_manager=characters,
            conflict_manager=conflict,
            world_state=world_state,
            fallback=fallback_npc_director,
            allow_fallback=allow_heuristic_fallback,
        )
        session_zero_facilitator = LLMSessionZeroFacilitator(
            client=session_zero_client,
            model=session_zero_model,
            fallback=fallback_session_zero,
            gm_personality_prompt=gm_style_prompt,
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            allow_fallback=allow_heuristic_fallback,
        )

    return SceneOrchestrator(
        action_brain=action_brain,
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
        interceptor=interceptor,
        expressor=expressor,
        npc_director=npc_director,
        scene_manager=scene_manager,
        session_zero_manager=session_zero,
        session_zero_facilitator=session_zero_facilitator,
        rest_manager=rest,
        travel_manager=travel,
        dungeon_manager=dungeon,
        world_map_manager=world_map,
        world_map_image_manager=world_map_image_manager,
    )


def _component_llm_config(config: LLMConfig, component: str) -> LLMConfig:
    """为 Action Brain / Expressor 允许单独覆盖速度相关配置。

    Action Brain 主要负责语义路由和 JSON 决策，通常适合快模型、低推理和关闭 thinking；
    Expressor 更偏文字表现，可以单独保留更强模型。未设置组件级变量时保持旧配置兼容。
    """

    prefix = f"FU_GM_{component}_"
    action_model = os.environ.get(f"{prefix}MODEL", "").strip()
    expressor_model = os.environ.get(f"{prefix}MODEL", "").strip()
    reasoning_effort = os.environ.get(f"{prefix}REASONING_EFFORT", "").strip()
    thinking_flag = os.environ.get(f"{prefix}THINKING", "").strip().lower()
    timeout = os.environ.get(f"{prefix}TIMEOUT_SECONDS", "").strip()

    thinking_enabled = config.thinking_enabled
    if thinking_flag in {"on", "true", "1", "yes", "enabled"}:
        thinking_enabled = True
    elif thinking_flag in {"off", "false", "0", "no", "disabled"}:
        thinking_enabled = False

    return replace(
        config,
        action_model=action_model or config.action_model,
        expressor_model=expressor_model or config.expressor_model,
        reasoning_effort=reasoning_effort or config.reasoning_effort,
        thinking_enabled=thinking_enabled,
        timeout_seconds=float(timeout) if timeout else config.timeout_seconds,
    )


def _session_zero_llm_config(config: LLMConfig) -> LLMConfig:
    reasoning_effort = os.environ.get("FU_GM_SESSION_ZERO_REASONING_EFFORT", "").strip()
    thinking_flag = os.environ.get("FU_GM_SESSION_ZERO_THINKING", "").strip().lower()
    thinking_enabled = config.thinking_enabled
    if thinking_flag in {"on", "true", "1", "yes", "enabled"}:
        thinking_enabled = True
    elif thinking_flag in {"off", "false", "0", "no", "disabled"}:
        thinking_enabled = False
    if not reasoning_effort and thinking_enabled == config.thinking_enabled:
        return config
    return replace(
        config,
        reasoning_effort=reasoning_effort or config.reasoning_effort,
        thinking_enabled=thinking_enabled,
    )
