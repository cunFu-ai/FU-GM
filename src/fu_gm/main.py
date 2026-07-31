from __future__ import annotations

import os

from fu_gm.app_factory import _component_llm_config
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.dungeon_manager import DungeonManager
from fu_gm.components.rest_manager import RestManager
from fu_gm.components.scene_manager import SceneManager
from fu_gm.components.session_zero_manager import SessionZeroManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.travel_manager import TravelManager
from fu_gm.components.world_map_manager import WorldMapManager
from fu_gm.components.world_state import WorldState
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.config import LLMConfig
from fu_gm.expressor import Expressor, LLMExpressor
from fu_gm.interceptor import ActionInterceptor
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.models import (
    Action,
    ActionType,
    Affinity,
    Bond,
    Character,
    Clock,
    EnemyRank,
    EscalationStage,
    StatusEffect,
)
from fu_gm.scene_orchestrator import SceneOrchestrator


def build_demo_app(*, use_llm: bool = True) -> SceneOrchestrator:
    characters = CharacterManager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    scene_manager = SceneManager()
    world_state = WorldState()
    world_map = WorldMapManager(world_state)
    rules = RulesEngine(seed=0)
    travel = TravelManager(rules)
    dungeon = DungeonManager(clocks)
    rest = RestManager(characters, clocks)
    session_zero = SessionZeroManager(world_state)

    valia = Character(
        name="瓦莉亚",
        attributes={"DEX": 8, "MIG": 10, "INS": 6, "WLP": 8},
        max_hp=45,
        hp=15,
        max_mp=30,
        mp=20,
        crisis_threshold=15,
        fabula_points=2,
        weapon_damage=5,
        weapon_type="lightning",
        identity="帝国叛逃魔剑士",
        theme="赎罪",
        origin="雷鸣群岛",
        bonds=[Bond(target="同伴", emotions=["信赖"])],
        spells=["落雷", "元素护体", "守护咏唱"],
        traits=["pc"],
    )
    mech = Character(
        name="帝国机甲",
        attributes={"DEX": 6, "MIG": 8, "INS": 6, "WLP": 6},
        max_hp=100,
        hp=60,
        max_mp=0,
        mp=0,
        crisis_threshold=0,
        weapon_damage=8,
        weapon_type="physical",
        defenses={"physical": 12, "magic": 10},
        affinities={"lightning": Affinity.WEAK, "fire": Affinity.IMMUNE},
        traits=["enemy", "villain"],
        initiative=11,
        identity="帝国第七魔导镇压机甲",
        theme="以绝对武力碾碎反抗",
        abilities=["火箭拳", "魔导屏障"],
        spells=["雷暴放射", "魔导屏障"],
    )

    characters.add(valia)
    characters.add(mech)
    clocks.add(Clock(name="炸毁桥梁", max_segments=6, current=2))
    scene_manager.start_scene(
        "断桥上的魔导机甲战",
        location="旧王国边境断桥",
        participants=["瓦莉亚", "帝国机甲"],
        objective="阻止帝国机甲通过桥梁",
    )
    conflict.start_scene("断桥上的魔导机甲战", ["瓦莉亚", "帝国机甲"])
    conflict.register_enemy(
        "帝国机甲",
        rank=EnemyRank.VILLAIN,
        ultima_points=2,
        escalation_stages=[
            EscalationStage(
                name="魔导过载形态",
                ultima_points=3,
                hp_restore=70,
                mp_restore=30,
                added_statuses=[StatusEffect.ENRAGED],
                note="机甲外壳剥落，露出过载核心。",
            )
        ],
    )
    world_state.session_pillars = [
        "高幻想与魔导科技共存",
        "英雄的选择可以改写世界",
        "反派拥有鲜明信念而非单纯邪恶",
    ]
    world_map.add_location(
        "旧王国边境断桥",
        x=0,
        y=0,
        description="旧王国和帝国前线之间的断桥，魔导机甲正在推进。",
        terrain="警戒地区",
        threat_level="high",
    )
    world_map.add_location(
        "雷尔德村",
        x=-2,
        y=1,
        description="靠近旧王国边境的村庄，仍有人暗中支援反抗者。",
        terrain="村庄",
    )
    world_state.ensure_npc_persona(
        "帝国机甲",
        public_identity="帝国第七魔导镇压机甲",
        role_in_story="重要反派兵器",
        core_drive="用压倒性的军势证明帝国秩序不可动摇",
        manner="冷酷、压迫、缺乏多余情绪",
        speech_style="机械、简短、带有审判意味",
        combat_style="优先压制危机目标，必要时用终结点强行复位",
        first_scene="断桥上的魔导机甲战",
        goals=["歼灭阻挡在桥上的英雄", "确保帝国行动继续推进"],
        taboos=["绝不承认自身是失败品", "不会轻易后撤，除非必须重整系统"],
        secrets=["核心中封印着一位失败实验者的残余灵魂"],
        custom_prompt="把自己视为帝国意志的延伸，不把普通人当作平等个体。",
    )

    interceptor = ActionInterceptor(
        rules_engine=rules,
        character_manager=characters,
        clock_manager=clocks,
        conflict_manager=conflict,
        world_state=world_state,
        scene_manager=scene_manager,
    )
    llm_config = LLMConfig.from_env()
    fallback_expressor = Expressor()
    npc_combat_rules = NPCCombatRules(characters, conflict, world_state)
    expressor = fallback_expressor
    gm_llm_client = None
    gm_llm_model = ""
    if use_llm and llm_config.api_key:
        action_config = _component_llm_config(llm_config, "ACTION")
        expressor_config = _component_llm_config(llm_config, "EXPRESSOR")
        llm_client = OpenAICompatibleClient(action_config)
        gm_llm_client = llm_client
        gm_llm_model = action_config.action_model
        expressor_client = llm_client if expressor_config == action_config else OpenAICompatibleClient(expressor_config)
        expressor = LLMExpressor(
            client=expressor_client,
            model=expressor_config.expressor_model,
            fallback=fallback_expressor,
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
        npc_combat_rules=npc_combat_rules,
        scene_manager=scene_manager,
        session_zero_manager=session_zero,
        rest_manager=rest,
        travel_manager=travel,
        dungeon_manager=dungeon,
        world_map_manager=world_map,
    )


def main() -> None:
    app = build_demo_app()
    player_input = "玩家[瓦莉亚]: 我要用雷电魔法攻击机甲！"
    action = Action(
        ActionType.SPELL,
        {
            "actor": "瓦莉亚",
            "spell": "落雷",
            "target": "帝国机甲",
            "attributes": ["INS", "WLP"],
            "damage_type": "lightning",
        },
    )
    print("=== FU-GM 示例 ===")
    print(f"玩家输入: {player_input}")
    print()
    print(app.run_structured_turn(action, player_input))


if __name__ == "__main__":
    main()
