from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from random import Random
import tempfile
from unittest.mock import patch

from fu_gm.components.combat_trait_manager import CombatTraitManager
from fu_gm.components.rules_engine import RulesEngine
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.clock_manager import ClockManager
from fu_gm.components.conflict_manager import ConflictManager
from fu_gm.components.npc_combat_rules import NPCCombatRules
from fu_gm.components.world_state import WorldState
from fu_gm.gm_tool_agent import GMToolExecutionContext
from fu_gm.gm_tool_contracts import GMToolDefinition, GMToolReceipt
from fu_gm.http_server import FUGMHttpService
from fu_gm.interceptor import ActionInterceptor
from fu_gm.models import (
    ActionType,
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
NOEL = "诺艾尔"
MIRA = "米拉"
CRISIS_STATE_KEY = "_crisis_derived_effects_v1"
MULTI_COOLDOWN = "scene:npc_ability:furnace-crisis-sweep:enter_crisis"
AFFINITY_COOLDOWN = "scene:npc_ability:furnace-crisis-shell:enter_crisis"


class FakeRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        if not self.values:
            raise AssertionError("NPC 多目标攻击进行了预期外的额外掷骰。")
        value = self.values.pop(0)
        if not low <= value <= high:
            raise AssertionError(f"掷骰值 {value} 超出范围 {low}-{high}")
        return value

    def getstate(self):
        return tuple(self.values)

    def setstate(self, state) -> None:
        self.values = list(state)


def _boss() -> Character:
    return Character(
        name=BOSS,
        attributes={"DEX": 6, "INS": 8, "MIG": 10, "WLP": 8},
        max_hp=120,
        hp=120,
        max_mp=70,
        mp=70,
        crisis_threshold=60,
        defenses={"physical": 12, "magic": 12},
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
                target_scope="self",
                attack_name=ATTACK,
                multi_attack=2,
                once_per_scene=True,
                description="危机状态下熔核横扫获得多重攻击(2)。",
            ),
            NPCAbilityProfile(
                ability_id="furnace-crisis-shell",
                name="炉心暴露",
                source_skill="危机效果",
                trigger="enter_crisis",
                effect_type="affinity_change",
                target_scope="self",
                affinity_changes={"fire": Affinity.WEAK},
                once_per_scene=True,
                description="危机状态下火焰相性变为弱点。",
            ),
        ],
    )


def _pc(name: str) -> Character:
    return Character(
        name=name,
        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        max_hp=60,
        hp=60,
        max_mp=35,
        mp=35,
        defenses={"physical": 8, "magic": 8},
        traits=["pc"],
    )


def _start_boss_conflict(
    characters: CharacterManager,
    conflict: ConflictManager,
) -> None:
    characters.add(_boss())
    characters.add(_pc(NOEL))
    characters.add(_pc(MIRA))
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
        [BOSS, NOEL, MIRA],
        player_side=[NOEL, MIRA],
        enemy_side=[BOSS],
    )


def _enter_crisis(character: Character) -> None:
    character.hp = character.crisis_threshold
    events = CombatTraitManager().after_damage(
        character,
        affinity=Affinity.NORMAL,
        damage=1,
        hp_before=character.crisis_threshold + 1,
        triggering_actor=NOEL,
    )
    assert [event.event_type for event in events].count(
        "npc_ability_enter_crisis"
    ) == 2


def _panel(conflict: ConflictManager) -> GamePanel:
    return GamePanel(
        game_phase=conflict.format_phase(),
        active_clocks=[],
        pc_status=[f"{NOEL}: HP 60/60", f"{MIRA}: HP 60/60"],
        enemy_status=[f"{BOSS}: HP 60/120"],
        recent_chat=f"轮到{BOSS}行动。",
        current_actor=BOSS,
    )


def test_crisis_multiattack_catalog_validates_and_resolves_two_pc_targets() -> None:
    characters = CharacterManager()
    conflict = ConflictManager(characters)
    world = WorldState()
    _start_boss_conflict(characters, conflict)
    _enter_crisis(characters.get(BOSS))
    panel = _panel(conflict)
    npc_rules = NPCCombatRules(characters, conflict, world)

    attack_entry = next(
        item
        for item in npc_rules.build_legal_action_catalog(panel, BOSS)
        if item.get("npc_action_type") == "Attack"
        and item.get("attack_name") == ATTACK
    )

    assert attack_entry["multi_attack"] == 2
    assert attack_entry["max_targets"] == 2
    assert attack_entry["targets"] == [NOEL, MIRA]

    npc_action = npc_rules.validate_action(
        panel,
        BOSS,
        {
            "npc_action_type": "Attack",
            "attack_name": ATTACK,
            "targets": [NOEL, MIRA],
            "action_description": f"{BOSS}挥动熔核巨刃，同时横扫两名英雄。",
        },
    )

    assert npc_action.action_type == ActionType.NPCACT
    assert npc_action.parameters["target"] == NOEL
    assert npc_action.parameters["targets"] == [NOEL, MIRA]
    assert npc_action.parameters["multi_attack"] == 2

    engine = RulesEngine()
    fake_random = FakeRandom([10, 9])
    engine._rng = fake_random
    interceptor = ActionInterceptor(
        engine,
        characters,
        ClockManager(),
        conflict,
        world,
    )

    resolution = interceptor.resolve(npc_action)

    # 多重攻击按一次共享命中检定，对每个目标生成一条权威结果并分别结算。
    assert len(resolution.payload["rolls"]) == 2
    assert [roll.target for roll in resolution.payload["rolls"]] == [
        NOEL,
        MIRA,
    ]
    assert all(roll.success for roll in resolution.payload["rolls"])
    assert [roll.damage for roll in resolution.payload["rolls"]] == [15, 15]
    assert characters.get(NOEL).hp == 45
    assert characters.get(MIRA).hp == 45
    assert fake_random.values == []


def test_gm_tool_autosave_failure_rolls_back_phase_cleanup_and_disk_snapshot() -> None:
    campaign_id = "crisis-phase-autosave-rollback"
    with tempfile.TemporaryDirectory() as data_root:
        service = FUGMHttpService(data_root=data_root, use_llm=False)
        runtime = service._runtime(campaign_id)
        app = runtime.app
        app.initialize_session_zero(participants=[NOEL, MIRA])
        _start_boss_conflict(app.character_manager, app.conflict_manager)
        boss = app.character_manager.get(BOSS)
        _enter_crisis(boss)
        app.conflict_manager.record_log(
            BOSS,
            "crisis_baseline",
            "赤炉大将的危机状态已经持久化。",
        )
        service._autosave_campaign(runtime, campaign_id)

        snapshot_path = app.memory_store._snapshot_path(campaign_id)
        events_path = app.memory_store._campaign_dir(campaign_id) / "events.jsonl"
        snapshot_before = snapshot_path.read_bytes()
        events_before = events_path.read_bytes() if events_path.exists() else None
        hp_before = boss.hp
        stage_before = app.conflict_manager.state.current_escalation_stage[BOSS]
        action_count_before = app.conflict_manager.state.enemy_action_counts[BOSS]
        queue_before = deepcopy(app.conflict_manager.state.queued_turns)
        queue_kinds_before = deepcopy(
            app.conflict_manager.state.queued_turn_kinds
        )
        affinity_before = deepcopy(boss.temporary_affinities)
        journal_before = deepcopy(boss.npc_skill_effects[CRISIS_STATE_KEY])
        cooldowns_before = deepcopy(boss.trigger_cooldowns)
        skill_effects_before = deepcopy(boss.npc_skill_effects)
        log_before = deepcopy(app.conflict_manager.state.combat_log)
        rng_before = app.interceptor.rules_engine._rng.getstate()
        expected_rng = Random()
        expected_rng.setstate(rng_before)
        expected_next_roll = expected_rng.randint(1, 20)
        mutation_evidence: dict[str, object] = {}

        def fail_after_writing_mutated_snapshot(
            target_runtime,
            target_campaign_id: str,
        ) -> str:
            path = Path(
                target_runtime.app.save_campaign_memory(target_campaign_id)
            )
            mutation_evidence["snapshot_during_failure"] = path.read_bytes()
            raise RuntimeError(
                "INJECTED_AUTOSAVE_FAILURE_AFTER_PHASE_SNAPSHOT"
            )

        def phase_then_autosave(
            _context: GMToolExecutionContext,
            _arguments: dict[str, object],
        ) -> GMToolReceipt:
            target = app.character_manager.get(BOSS)
            target.hp = 0
            event = app.conflict_manager.try_escalate(BOSS)
            assert event is not None and event.event_type == "boss_phase"
            app.interceptor.rules_engine.roll_die(20)
            mutation_evidence.update(
                {
                    "hp": target.hp,
                    "stage": app.conflict_manager.state.current_escalation_stage[
                        BOSS
                    ],
                    "action_count": app.conflict_manager.state.enemy_action_counts[
                        BOSS
                    ],
                    "queue": deepcopy(
                        app.conflict_manager.state.queued_turns
                    ),
                    "affinity": deepcopy(target.temporary_affinities),
                    "journal_present": CRISIS_STATE_KEY
                    in target.npc_skill_effects,
                    "cooldowns": deepcopy(target.trigger_cooldowns),
                    "skill_effects": deepcopy(target.npc_skill_effects),
                    "log": deepcopy(app.conflict_manager.state.combat_log),
                    "rng": app.interceptor.rules_engine._rng.getstate(),
                }
            )
            service._autosave_campaign(runtime, campaign_id)
            raise AssertionError("注入的自动保存失败没有生效。")

        service.gm_tool_registry.register(
            GMToolDefinition(
                name="test_phase_then_autosave",
                description="仅用于验证首领阶段事务回滚。",
                handler=phase_then_autosave,
                side_effect="write",
            )
        )
        context = GMToolExecutionContext(
            campaign_id=campaign_id,
            session_id="boss-rollback",
            channel_id="group-1",
            speaker=NOEL,
            gate_status="adventure",
            directly_addressed=True,
            metadata={"current_message": "进入下一阶段。"},
        )

        with patch.object(
            service,
            "_autosave_campaign",
            side_effect=fail_after_writing_mutated_snapshot,
        ):
            receipt = service.gm_tool_registry.execute(
                "test_phase_then_autosave",
                {},
                context,
            )

        assert not receipt.ok
        assert receipt.error_code == "TOOL_EXECUTION_FAILED"
        assert (
            "INJECTED_AUTOSAVE_FAILURE_AFTER_PHASE_SNAPSHOT"
            in receipt.message
        )
        assert mutation_evidence["hp"] == 120
        assert mutation_evidence["stage"] == 0
        assert mutation_evidence["action_count"] == 3
        assert mutation_evidence["queue"] == [NOEL, MIRA]
        assert mutation_evidence["affinity"] == {"fire": Affinity.RESIST}
        assert mutation_evidence["journal_present"] is False
        assert MULTI_COOLDOWN not in mutation_evidence["cooldowns"]
        assert AFFINITY_COOLDOWN not in mutation_evidence["cooldowns"]
        assert "triggered_multiattack" not in mutation_evidence["skill_effects"]
        assert len(mutation_evidence["log"]) == len(log_before) + 1
        assert mutation_evidence["rng"] != rng_before
        assert mutation_evidence["snapshot_during_failure"] != snapshot_before

        restored_boss = app.character_manager.get(BOSS)
        assert restored_boss.hp == hp_before
        assert (
            app.conflict_manager.state.current_escalation_stage[BOSS]
            == stage_before
        )
        assert (
            app.conflict_manager.state.enemy_action_counts[BOSS]
            == action_count_before
        )
        assert app.conflict_manager.state.queued_turns == queue_before
        assert app.conflict_manager.state.queued_turn_kinds == queue_kinds_before
        assert restored_boss.temporary_affinities == affinity_before
        assert restored_boss.npc_skill_effects[CRISIS_STATE_KEY] == journal_before
        assert restored_boss.trigger_cooldowns == cooldowns_before
        assert restored_boss.npc_skill_effects == skill_effects_before
        assert app.conflict_manager.state.combat_log == log_before
        assert app.interceptor.rules_engine._rng.getstate() == rng_before
        assert snapshot_path.read_bytes() == snapshot_before
        assert (
            events_path.read_bytes() if events_path.exists() else None
        ) == events_before

        restarted = FUGMHttpService(data_root=data_root, use_llm=False)
        reloaded = restarted._runtime(campaign_id)
        reloaded_app = reloaded.app
        reloaded_boss = reloaded_app.character_manager.get(BOSS)

        assert reloaded.loaded_from_disk is True
        assert reloaded_boss.hp == hp_before
        assert (
            reloaded_app.conflict_manager.state.current_escalation_stage[BOSS]
            == stage_before
        )
        assert (
            reloaded_app.conflict_manager.state.enemy_action_counts[BOSS]
            == action_count_before
        )
        assert reloaded_app.conflict_manager.state.queued_turns == queue_before
        assert (
            reloaded_app.conflict_manager.state.queued_turn_kinds
            == queue_kinds_before
        )
        assert reloaded_boss.temporary_affinities == affinity_before
        assert (
            reloaded_boss.npc_skill_effects[CRISIS_STATE_KEY]
            == journal_before
        )
        assert reloaded_boss.trigger_cooldowns == cooldowns_before
        assert reloaded_boss.npc_skill_effects == skill_effects_before
        assert reloaded_app.conflict_manager.state.combat_log == log_before
        assert (
            reloaded_app.interceptor.rules_engine.roll_die(20)
            == expected_next_roll
        )
        assert snapshot_path.read_bytes() == snapshot_before
