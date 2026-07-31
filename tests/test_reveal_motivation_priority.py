from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.world_state import WorldState
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import Action, ActionType, NPCPersona
from fu_gm.scene_orchestrator import SceneOrchestrator


def test_scene_reveal_provider_wins_over_generic_auto_created_persona_goal() -> None:
    characters = CharacterManager()
    clocks = ClockManager()
    conflict = ConflictManager(characters)
    world = WorldState()
    world.npc_personas["财团巡逻队"] = NPCPersona(
        name="财团巡逻队",
        active_goal="先保住眼下由自己承担的人与事",
        role_in_story="当前场景中的非玩家角色",
    )
    interceptor = ActionInterceptor(RulesEngine(seed=1), characters, clocks, conflict, world)
    interceptor.reveal_motivation_provider = lambda target: (
        "带走失忆旅人" if "财团巡逻队" in target else ""
    )

    motivation, inferred = interceptor._reveal_opportunity_motivation(
        Action(ActionType.TRIGGER_OPPORTUNITY, {}),
        "门外那支财团巡逻队",
    )

    assert motivation == "带走失忆旅人"
    assert not inferred


def test_scene_entity_alias_matches_descriptive_target() -> None:
    assert SceneOrchestrator._scene_entity_alias_match("门外那支财团巡逻队", "财团巡逻队")
