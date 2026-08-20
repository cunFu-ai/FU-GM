from __future__ import annotations

from copy import deepcopy
import tempfile
from types import SimpleNamespace

from fu_gm.components.campaign_state_transaction import CampaignStateTransaction
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.combat_trait_manager import CombatTraitManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.memory_store import CampaignMemoryStore
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.world_state import WorldState
from fu_gm.models import (
    Affinity,
    Character,
    EnemyRank,
    EscalationStage,
    GamePanel,
    NPCAbilityProfile,
    NPCAttackProfile,
)


BOSS = "赤炉大将"
ATTACK = "熔核横扫"
CRISIS_COOLDOWN = "scene:npc_ability:furnace-crisis-sweep:enter_crisis"


def _fixture() -> tuple[
    CharacterManager,
    ConflictManager,
    WorldState,
]:
    characters = CharacterManager()
    characters.add(
        Character(
            name=BOSS,
            attributes={"DEX": 6, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=120,
            hp=120,
            max_mp=70,
            mp=70,
            crisis_threshold=60,
            traits=["enemy", "villain", "boss"],
            npc_attacks=[
                NPCAttackProfile(
                    attack_id="furnace-cleave",
                    name=ATTACK,
                    attributes=["MIG", "MIG"],
                    damage_bonus=5,
                    damage_type="fire",
                    multi_attack=1,
                )
            ],
            npc_ability_profiles=[
                NPCAbilityProfile(
                    ability_id="furnace-crisis-sweep",
                    name="炉心连扫",
                    source_skill="危机效果",
                    trigger="enter_crisis",
                    effect_type="grant_multiattack",
                    attack_name=ATTACK,
                    multi_attack=2,
                    once_per_scene=True,
                    description="危机状态下熔核横扫获得多重攻击(2)。",
                )
            ],
        )
    )
    characters.add(
        Character(
            name="诺艾尔",
            attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 8},
            max_hp=65,
            hp=65,
            max_mp=35,
            mp=35,
            traits=["pc"],
        )
    )
    conflict = ConflictManager(characters)
    conflict.register_enemy(
        BOSS,
        EnemyRank.VILLAIN,
        ultima_points=5,
        escalation_stages=[
            EscalationStage(
                name="炉心解放",
                ultima_points=0,
                transition_kind="boss_phase",
                preparation_round=True,
                hp_restore=None,
                mp_restore=None,
                added_abilities=["灼热外壳"],
                affinity_changes={"fire": Affinity.RESIST},
                action_count=3,
            )
        ],
    )
    conflict.start_scene(
        "熔炉核心决战",
        [BOSS, "诺艾尔"],
        player_side=["诺艾尔"],
        enemy_side=[BOSS],
    )
    return characters, conflict, WorldState()


def _enter_crisis(character: Character):
    character.hp = character.crisis_threshold
    return CombatTraitManager().after_damage(
        character,
        affinity=Affinity.NORMAL,
        damage=1,
        hp_before=character.crisis_threshold + 1,
        triggering_actor="诺艾尔",
    )


def _attack_profile(
    characters: CharacterManager,
    conflict: ConflictManager,
    world: WorldState,
) -> dict[str, object]:
    panel = GamePanel(
        game_phase=conflict.format_phase(),
        active_clocks=[],
        pc_status=[],
        enemy_status=[],
        recent_chat="轮到赤炉大将行动。",
        current_actor=BOSS,
    )
    return next(
        item
        for item in NPCCombatRules(
            characters,
            conflict,
            world,
        ).build_legal_action_catalog(panel, BOSS)
        if item.get("npc_action_type") == "Attack"
        and item.get("attack_name") == ATTACK
    )


def _assert_crisis_multiattack_active(
    characters: CharacterManager,
    conflict: ConflictManager,
    world: WorldState,
) -> None:
    boss = characters.get(BOSS)
    assert _attack_profile(characters, conflict, world)["multi_attack"] == 2
    assert boss.npc_skill_effects["triggered_multiattack"][ATTACK] == 2
    assert CRISIS_COOLDOWN in boss.trigger_cooldowns


def _assert_crisis_multiattack_cleared(
    characters: CharacterManager,
    conflict: ConflictManager,
    world: WorldState,
) -> None:
    boss = characters.get(BOSS)
    assert _attack_profile(characters, conflict, world)["multi_attack"] == 1
    assert ATTACK not in boss.npc_skill_effects.get(
        "triggered_multiattack",
        {},
    )
    assert CRISIS_COOLDOWN not in boss.trigger_cooldowns


def test_crisis_multiattack_expires_on_full_phase_and_can_trigger_again() -> None:
    characters, conflict, world = _fixture()
    boss = characters.get(BOSS)

    first_events = _enter_crisis(boss)
    assert any(
        event.event_type == "npc_ability_enter_crisis"
        for event in first_events
    )
    _assert_crisis_multiattack_active(characters, conflict, world)

    boss.hp = 0
    phase = conflict.try_escalate(BOSS)

    assert phase is not None and phase.event_type == "boss_phase"
    assert boss.hp == boss.max_hp
    assert not boss.in_crisis
    _assert_crisis_multiattack_cleared(characters, conflict, world)
    # Explicit stage state is permanent and is applied after crisis cleanup.
    assert "灼热外壳" in boss.abilities
    assert boss.temporary_affinities["fire"] == Affinity.RESIST

    second_events = _enter_crisis(boss)

    assert any(
        event.event_type == "npc_ability_enter_crisis"
        for event in second_events
    )
    _assert_crisis_multiattack_active(characters, conflict, world)


def test_healing_above_crisis_clears_only_crisis_derived_multiattack() -> None:
    characters, conflict, world = _fixture()
    boss = characters.get(BOSS)
    _enter_crisis(boss)
    _assert_crisis_multiattack_active(characters, conflict, world)

    before, after = characters.modify_resource(BOSS, "hp", boss.max_hp)

    assert before == boss.crisis_threshold
    assert after == boss.max_hp
    _assert_crisis_multiattack_cleared(characters, conflict, world)

    _enter_crisis(boss)
    _assert_crisis_multiattack_active(characters, conflict, world)


def test_crisis_profile_does_not_downgrade_existing_same_attack_multiattack() -> None:
    characters, conflict, world = _fixture()
    boss = characters.get(BOSS)
    boss.npc_skill_effects["triggered_multiattack"] = {ATTACK: 3}

    _enter_crisis(boss)

    assert boss.npc_skill_effects["triggered_multiattack"][ATTACK] == 3
    assert _attack_profile(characters, conflict, world)["multi_attack"] == 3

    boss.hp = 0
    conflict.try_escalate(BOSS)

    assert boss.npc_skill_effects["triggered_multiattack"][ATTACK] == 3
    assert _attack_profile(characters, conflict, world)["multi_attack"] == 3
    assert CRISIS_COOLDOWN not in boss.trigger_cooldowns


def test_immediate_once_per_scene_crisis_effect_keeps_its_cooldown() -> None:
    characters, _, _ = _fixture()
    boss = characters.get(BOSS)
    boss.npc_ability_profiles = [
        NPCAbilityProfile(
            ability_id="crisis-cleanse",
            name="暴躁脾气",
            source_skill="危机效果",
            trigger="enter_crisis",
            effect_type="clear_statuses",
            once_per_scene=True,
        )
    ]
    cooldown = "scene:npc_ability:crisis-cleanse:enter_crisis"

    first_events = _enter_crisis(boss)
    characters.modify_resource(BOSS, "hp", boss.max_hp)
    second_events = _enter_crisis(boss)

    assert any(
        event.event_type == "npc_ability_enter_crisis"
        for event in first_events
    )
    assert cooldown in boss.trigger_cooldowns
    assert not any(
        event.event_type == "npc_ability_enter_crisis"
        for event in second_events
    )


def test_phase_cleanup_preserves_unrelated_and_stage_authored_state() -> None:
    characters, conflict, world = _fixture()
    boss = characters.get(BOSS)
    boss.npc_skill_effects.update(
        {
            "permanent_marker": {"active": True},
            "triggered_multiattack": {"永久连斩": 3},
            "triggered_ignore_resist": ["永久连斩"],
        }
    )
    boss.temporary_affinities["ice"] = Affinity.RESIST
    boss.npc_ability_profiles.extend(
        [
            NPCAbilityProfile(
                ability_id="crisis-bypass",
                name="灼穿装甲",
                source_skill="危机效果",
                trigger="enter_crisis",
                effect_type="ignore_resist",
                attack_name=ATTACK,
            ),
            NPCAbilityProfile(
                ability_id="crisis-affinities",
                name="炉心暴露",
                source_skill="危机效果",
                trigger="enter_crisis",
                effect_type="affinity_change",
                affinity_changes={
                    "fire": Affinity.WEAK,
                    "ice": Affinity.IMMUNE,
                },
            ),
        ]
    )

    _enter_crisis(boss)

    assert ATTACK in boss.npc_skill_effects["triggered_ignore_resist"]
    assert boss.temporary_affinities["fire"] == Affinity.WEAK
    assert boss.temporary_affinities["ice"] == Affinity.IMMUNE

    boss.hp = 0
    conflict.try_escalate(BOSS)

    assert boss.npc_skill_effects["permanent_marker"] == {"active": True}
    assert boss.npc_skill_effects["triggered_multiattack"] == {
        "永久连斩": 3
    }
    assert boss.npc_skill_effects["triggered_ignore_resist"] == [
        "永久连斩"
    ]
    assert boss.temporary_affinities["ice"] == Affinity.RESIST
    assert boss.temporary_affinities["fire"] == Affinity.RESIST
    assert "灼热外壳" in boss.abilities
    assert _attack_profile(characters, conflict, world)["multi_attack"] == 1


def test_phase_cleanup_repairs_pre_provenance_campaign_state() -> None:
    characters, conflict, world = _fixture()
    boss = characters.get(BOSS)
    # This is the exact shape persisted by builds before the provenance
    # journal existed: the effect and deterministic cooldown are present, but
    # no internal lifecycle record accompanies them.
    boss.hp = boss.crisis_threshold
    boss.npc_skill_effects["triggered_multiattack"] = {ATTACK: 2}
    boss.trigger_cooldowns.add(CRISIS_COOLDOWN)
    _assert_crisis_multiattack_active(characters, conflict, world)

    boss.hp = 0
    conflict.try_escalate(BOSS)

    _assert_crisis_multiattack_cleared(characters, conflict, world)
    assert "灼热外壳" in boss.abilities


def test_persisted_crisis_effect_cleans_up_after_loaded_phase_transition() -> None:
    characters, conflict, world = _fixture()
    _enter_crisis(characters.get(BOSS))

    with tempfile.TemporaryDirectory() as tmpdir:
        store = CampaignMemoryStore(tmpdir)
        store.save_campaign(
            "crisis-persistence",
            world_state=world,
            character_manager=characters,
            clock_manager=ClockManager(),
            conflict_manager=conflict,
        )
        loaded_characters = CharacterManager()
        loaded_conflict = ConflictManager(loaded_characters)
        loaded_world = WorldState()
        store.load_campaign(
            "crisis-persistence",
            world_state=loaded_world,
            character_manager=loaded_characters,
            clock_manager=ClockManager(),
            conflict_manager=loaded_conflict,
        )

    _assert_crisis_multiattack_active(
        loaded_characters,
        loaded_conflict,
        loaded_world,
    )
    loaded_boss = loaded_characters.get(BOSS)
    loaded_boss.hp = 0
    loaded_conflict.try_escalate(BOSS)

    _assert_crisis_multiattack_cleared(
        loaded_characters,
        loaded_conflict,
        loaded_world,
    )
    assert "灼热外壳" in loaded_boss.abilities

    with tempfile.TemporaryDirectory() as tmpdir:
        clean_store = CampaignMemoryStore(tmpdir)
        clean_store.save_campaign(
            "post-phase-clean",
            world_state=loaded_world,
            character_manager=loaded_characters,
            clock_manager=ClockManager(),
            conflict_manager=loaded_conflict,
        )
        final_characters = CharacterManager()
        final_conflict = ConflictManager(final_characters)
        final_world = WorldState()
        clean_store.load_campaign(
            "post-phase-clean",
            world_state=final_world,
            character_manager=final_characters,
            clock_manager=ClockManager(),
            conflict_manager=final_conflict,
        )

    final_boss = final_characters.get(BOSS)
    _assert_crisis_multiattack_cleared(
        final_characters,
        final_conflict,
        final_world,
    )
    assert final_boss.hp == final_boss.max_hp
    assert final_conflict.state.current_escalation_stage[BOSS] == 0
    assert final_conflict.state.enemy_action_counts[BOSS] == 3
    assert final_boss.temporary_affinities["fire"] == Affinity.RESIST


def test_campaign_transaction_rollback_restores_crisis_lifecycle_state() -> None:
    characters, conflict, world = _fixture()
    _enter_crisis(characters.get(BOSS))
    expected_boss = deepcopy(characters.get(BOSS))
    expected_conflict_state = deepcopy(conflict.state)

    with tempfile.TemporaryDirectory() as tmpdir:
        app = SimpleNamespace(
            memory_store=CampaignMemoryStore(tmpdir),
            world_state=world,
            character_manager=characters,
            clock_manager=ClockManager(),
            conflict_manager=conflict,
            scene_manager=None,
            scene_frame_manager=None,
            ritual_manager=None,
            project_manager=None,
            story_arc_manager=None,
            hero_log_manager=None,
            ally_npc_manager=None,
            session_ledger=None,
            session_zero_manager=None,
            travel_manager=None,
            dungeon_manager=None,
            world_map_manager=None,
            progression_manager=None,
            interceptor=None,
        )
        before_phase = CampaignStateTransaction.capture(
            app,
            "crisis-rollback",
        )
        characters.get(BOSS).hp = 0
        conflict.try_escalate(BOSS)
        _assert_crisis_multiattack_cleared(characters, conflict, world)
        assert conflict.state.current_escalation_stage[BOSS] == 0
        assert conflict.state.enemy_action_counts[BOSS] == 3
        assert conflict.state.queued_turns == ["诺艾尔"]
        assert characters.get(BOSS).temporary_affinities == {
            "fire": Affinity.RESIST
        }

        CampaignStateTransaction.restore(app, before_phase)

    restored_boss = app.character_manager.get(BOSS)
    assert restored_boss == expected_boss
    assert app.conflict_manager.state == expected_conflict_state
    assert restored_boss.in_crisis
    assert app.conflict_manager.state.current_escalation_stage[BOSS] == -1
    assert "灼热外壳" not in restored_boss.abilities
    _assert_crisis_multiattack_active(
        app.character_manager,
        app.conflict_manager,
        app.world_state,
    )

    restored_boss.hp = 0
    app.conflict_manager.try_escalate(BOSS)
    _assert_crisis_multiattack_cleared(
        app.character_manager,
        app.conflict_manager,
        app.world_state,
    )
