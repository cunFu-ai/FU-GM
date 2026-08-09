from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_action_adapter import NPCActionAdapter
from fu_gm.models import (
    Action,
    ActionResolution,
    ActionType,
    Character,
    NPCAbilityProfile,
)


def _adapter() -> tuple[NPCActionAdapter, ConflictManager]:
    characters = CharacterManager()
    characters.add(
        Character(
            name="监察官艾蕾娜",
            attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
            max_hp=80,
            hp=80,
            max_mp=60,
            mp=60,
            traits=["enemy"],
        )
    )
    conflict = ConflictManager(characters)
    conflict.start_scene("旧路闸门", ["监察官艾蕾娜"])
    return NPCActionAdapter(characters, conflict), conflict


def test_npc_attack_translation_preserves_complete_target_name_from_string_list() -> None:
    adapter, _conflict = _adapter()

    translated = adapter.translate(
        Action(
            ActionType.NPCACT,
            {
                "actor": "监察官艾蕾娜",
                "npc_action_type": "攻击",
                "targets": "伊莉雅、赛璃",
            },
        )
    )

    assert isinstance(translated, Action)
    assert translated.action_type == ActionType.ATTACK
    assert translated.parameters["target"] == "伊莉雅"
    assert translated.parameters["targets"] == "伊莉雅、赛璃"


def test_npc_escape_is_a_terminal_resolution_and_removes_actor_from_turns() -> None:
    adapter, conflict = _adapter()

    resolved = adapter.translate(
        Action(
            ActionType.NPCACT,
            {"actor": "监察官艾蕾娜", "npc_action_type": "escape"},
        )
    )

    assert isinstance(resolved, ActionResolution)
    assert resolved.payload["npc_escaped"] is True
    assert "监察官艾蕾娜" not in conflict.state.turn_order


def test_typed_special_action_prepares_and_attack_consumes_damage_bonus() -> None:
    adapter, _conflict = _adapter()
    actor = adapter.characters.get("监察官艾蕾娜")
    actor.weapon_damage = 5
    actor.npc_ability_profiles = [
        NPCAbilityProfile(
            ability_id="charged-strike",
            name="聚能",
            source_skill="特殊行动",
            trigger="skill_action",
            effect_type="prepare_attack_damage",
            target_scope="self",
            amount=10,
        )
    ]

    prepared = adapter.translate(
        Action(
            ActionType.NPCACT,
            {
                "actor": "监察官艾蕾娜",
                "npc_action_type": "OtherAction",
                "other_action_name": "聚能",
                "ability_id": "charged-strike",
            },
        )
    )

    assert isinstance(prepared, ActionResolution)
    assert actor.npc_skill_effects["prepared_attack_damage"]["amount"] == 10

    translated = adapter.translate(
        Action(
            ActionType.NPCACT,
            {
                "actor": "监察官艾蕾娜",
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "weapon_damage": 15,
                "consumes_combat_preparation": "prepared_attack_damage",
            },
        )
    )

    assert isinstance(translated, Action)
    assert translated.parameters["weapon_damage"] == 15
    assert "prepared_attack_damage" not in actor.npc_skill_effects


def test_npc_attack_consumes_previous_guard_marker_after_translation() -> None:
    adapter, _conflict = _adapter()
    actor = adapter.characters.get("监察官艾蕾娜")
    actor.npc_skill_effects["previous_action_guarded"] = True

    translated = adapter.translate(
        Action(
            ActionType.NPCACT,
            {
                "actor": "监察官艾蕾娜",
                "npc_action_type": "Attack",
                "target": "伊莉雅",
                "weapon_damage": 10,
            },
        )
    )

    assert isinstance(translated, Action)
    assert "previous_action_guarded" not in actor.npc_skill_effects
    assert translated.parameters["weapon_damage"] == 10
