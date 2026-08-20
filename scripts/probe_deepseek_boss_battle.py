#!/usr/bin/env python3
"""Isolated two-player Boss-battle probe for the official DeepSeek service.

Safe by default: without ``--live`` this command only constructs and validates
the authoritative fixture.  A live run uses a system-random rules seed, a
temporary campaign directory and an ephemeral loopback port.  It never routes
gameplay to the deployed service on port 8765; that service is read only and is
sampled solely as a before/after sentinel.

The probe intentionally does not offer a seed argument.  Each live run gets one
seed from ``secrets`` and records it for forensic replay after the run.  It also
asserts that no outcome is preloaded at any HTTP boundary.  The engine's own
already-rolled provisional-check replay remains available because that is how a
real accepted check is committed without rolling twice.  The probe never calls
or preloads that hook, and no model response, roll or branch is retried merely
to obtain a more attractive result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import secrets
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import fu_gm
from benchmark_deepseek_nonopening import (
    _close_client,
    _secret_scan,
    _shutdown_background_workers,
    _write_json_secure,
    _write_jsonl_secure,
)
from benchmark_deepseek_opening_ab import _production_snapshot, _usage_summary
from probe_deepseek_full_opening import (
    request_json,
    role_snapshot,
    sanitized_client_calls,
)
from probe_deepseek_session_prep_json import provider_config, read_dotenv

from fu_gm.components.encounter_manager import EncounterManager
from fu_gm.http_server import FUGMHttpService, make_server
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.llm_client_bundle import TestLLMClientBundle
from fu_gm.models import (
    Affinity,
    Character,
    EnemyRank,
    EscalationStage,
    HeroCreationProfile,
    NPCAbilityProfile,
    NPCAttackProfile,
    PartyMemberEntry,
    PartySheet,
    SceneType,
    StatusEffect,
)


CAMPAIGN_ID = "probe-deepseek-boss-battle"
SESSION_ID = "boss-battle-session"
CHANNEL_ID = "boss-battle-isolated"
FRONT_PLAYER = "阿凛"
FRONT_HERO = "诺艾尔"
SUPPORT_PLAYER = "小澜"
SUPPORT_HERO = "星澜"
BOSS_NAME = "赤炉大将"
MINION_NAME = "熔炉侍从"
PRODUCTION_PORT = 8765

ERROR_CATEGORIES = {
    "PROVIDER_TIMEOUT",
    "PROVIDER_EMPTY_RESPONSE",
    "PROVIDER_HTTP",
    "PROVIDER_SCHEMA",
    "MODEL_TOOL_MISSING",
    "MODEL_TOOL_REJECTED",
    "MODEL_RULE_RETRY_STUCK",
    "REVIEWER_FALSE_REJECTION",
    "RULE_RECEIPT_ERROR",
    "FIXTURE_INVALID",
    "SKILL_EVIDENCE_MISSING",
    "CAPABILITY_EVIDENCE_MISSING",
    "BOSS_RULE_INVARIANT_FAILED",
    "PERSISTENCE_MISMATCH",
    "PRODUCTION_SENTINEL_DRIFT",
    "RANDOM_COVERAGE_INCONCLUSIVE",
    "TURN_LIMIT_EXCEEDED",
    "UNEXPECTED_DECISION_WINDOW",
    "INTERNAL_ERROR",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated two-player DeepSeek Boss-battle probe."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow real official-DeepSeek calls. Omit for offline validation.",
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        default=Path.home() / ".fu-gm" / "fu_gm.env",
    )
    parser.add_argument("--output-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--max-exchanges", type=int, default=96)
    parser.add_argument("--request-timeout", type=float, default=240.0)
    parser.add_argument(
        "--expected-source-root",
        type=Path,
        default=Path.home() / ".fu-gm" / "src",
        help="Live runs fail unless the imported fu_gm package is below this root.",
    )
    parser.add_argument(
        "--production-data-root",
        type=Path,
        default=Path.home() / ".fu-gm" / "data" / "campaigns",
    )
    parser.add_argument(
        "--capability",
        action="append",
        choices=("protector_reaction", "dual_wield"),
        default=[],
        help="Enable an optional future-capability audit when runtime support exists.",
    )
    return parser.parse_args(argv)


def selected_skill_matrix() -> dict[str, dict[str, object]]:
    """Return the current, authoritative skill-coverage contract."""

    rows = {
        "防御精通": {
            "actor": FRONT_HERO,
            "kind": "passive_hard",
            "planned_probe": "dual_shield_damage_and_defense",
            "required": True,
        },
        "双盾战士": {
            "actor": FRONT_HERO,
            "kind": "passive_hard",
            "planned_probe": "two_shields_equipped",
            "required": True,
        },
        "利刃风暴": {
            "actor": FRONT_HERO,
            "kind": "hard_rule",
            "planned_probe": f"attack_distinct_targets:{BOSS_NAME},{MINION_NAME}",
            "required": True,
        },
        "挺身守护": {
            "actor": FRONT_HERO,
            "kind": "hard_rule_reaction",
            "planned_probe": f"arm_for:{SUPPORT_HERO};redirect_next_danger",
            "required": True,
            "required_evidence_kinds": [
                "successful_skill_receipt",
                "armed_out_of_turn",
                "redirected",
                "not_reused",
            ],
        },
        "集中心智": {
            "actor": SUPPORT_HERO,
            "kind": "passive_hard",
            "planned_probe": "fixture_max_mp",
            "required": True,
        },
        "知识就是力量": {
            "actor": SUPPORT_HERO,
            "kind": "hard_rule",
            "planned_probe": "open_insight_check",
            "required": True,
        },
        "快速评估": {
            "actor": SUPPORT_HERO,
            "kind": "hard_rule",
            "planned_probe": f"conflict_start_assessment:{BOSS_NAME}",
            "required": True,
        },
        "元素魔法": {
            "actor": SUPPORT_HERO,
            "kind": "hard_rule",
            "planned_probe": "cast_elemental_barrier_and_fire_spell",
            "required": True,
            "required_evidence_kinds": [
                "elemental_barrier_cast",
                "fire_spell_cast",
            ],
        },
    }
    return {
        name: {
            **row,
            "status": "planned",
            "evidence": [],
        }
        for name, row in rows.items()
    }


def optional_capability_matrix(
    enabled: Iterable[str] = (),
) -> dict[str, dict[str, object]]:
    """Runtime-capability checks outside the class-skill budget.

    Dual wield, the p.66 minor action and p.76 conflict teamwork are mandatory
    in the current live probe.  Protector reaction remains mandatory through
    the selected skill matrix.
    """

    selected = set(enabled)
    return {
        "protector_reaction": {
            "enabled": True,
            "requested_by_cli": "protector_reaction" in selected,
            "required": False,
            "status": "covered_by_skill_matrix",
            "contract": [
                "is explicitly armed before an imminent attack, spell, or hazard resolves",
                "does not consume the protector's main action",
                "redirects exactly one eligible danger and persists the used cooldown",
            ],
            "note": "Mandatory arm-and-redirect evidence is tracked by 挺身守护 in skill_matrix.",
        },
        "dual_wield": {
            "enabled": True,
            "requested_by_cli": "dual_wield" in selected,
            "required": True,
            "status": "planned",
            "evidence": [],
            "contract": [
                "two weapons share a legal weapon category",
                "each hit has high_roll zero",
                "both hits lose and cannot gain multi-attack",
                "same or different targets are accepted",
                "incidental attacks cannot dual-wield",
            ],
            "note": "The live probe must settle one real two-weapon Attack receipt.",
        },
        "minor_action": {
            "enabled": True,
            "required": True,
            "status": "planned",
            "required_evidence_kinds": [
                "settled_without_check",
                "main_action_preserved",
            ],
            "evidence": [],
            "contract": [
                "uses MinorAction/interact on the registered 炉心安全栓",
                "changes its authoritative state to 断开辅助燃料",
                "does not roll dice or enter a check",
                "does not change current_actor",
                "allows at most the ordinary first owner-turn lifecycle increment",
                "the same actor's following main action succeeds and advances once",
            ],
            "note": "Required before 诺艾尔's first main action.",
        },
        "team_assist": {
            "enabled": True,
            "required": True,
            "status": "planned",
            "required_evidence_kinds": [
                "registered_and_turn_consumed",
                "consumed_by_check",
                "not_reused",
            ],
            "evidence": [],
            "contract": [
                "星澜 explicitly assists current actor 诺艾尔 out of turn",
                "registration consumes 星澜's round action but not 诺艾尔's action",
                "the pending assist is consumed by 诺艾尔's real 利刃风暴 check",
                "the same assist is absent from a later 诺艾尔 check",
            ],
            "note": "No intent text or minor action can count as check consumption.",
        },
    }


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted((_jsonable(item) for item in value), key=str)
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return value


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _front_profile() -> HeroCreationProfile:
    return HeroCreationProfile(
        player_name=FRONT_PLAYER,
        hero_name=FRONT_HERO,
        identity="以双盾守住同伴的灰港卫士",
        theme="责任",
        origin="灰港",
        classes={"守护者": 4, "武器大师": 1},
        attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
        skills={"防御精通": 2, "双盾战士": 1, "挺身守护": 1, "利刃风暴": 1},
        equipment=["旅行装束", "青铜盾", "符文盾"],
    )


def _support_profile() -> HeroCreationProfile:
    return HeroCreationProfile(
        player_name=SUPPORT_PLAYER,
        hero_name=SUPPORT_HERO,
        identity="记录古代灾变的星塔学者",
        theme="求知",
        origin="星落尖塔",
        classes={"博学家": 3, "元素使": 2},
        attributes={"DEX": 6, "INS": 10, "MIG": 8, "WLP": 8},
        skills={"集中心智": 1, "知识就是力量": 1, "快速评估": 1, "元素魔法": 2},
        spells=["元素幕障", "炎弹"],
        equipment=["晨星匕首（钢匕首模板）", "暮影匕首（钢匕首模板）"],
    )


def _base_enemy(
    name: str,
    *,
    initiative: int,
    attack: NPCAttackProfile,
    traits: list[str] | None = None,
) -> Character:
    return Character(
        name=name,
        level=5,
        attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 6},
        max_hp=40,
        hp=40,
        max_mp=35,
        mp=35,
        crisis_threshold=20,
        # FU Core p303: with DEX d10 / INS d8 / MIG d6 / WLP d6 the base
        # defenses are 10 physical and 8 magic, while initiative is 9.
        defenses={"physical": 10, "magic": 8},
        affinities={
            # Construct species (p304).
            "poison": Affinity.IMMUNE,
            "earth": Affinity.RESIST,
            # One selected Damage Resistance skill.
            "lightning": Affinity.RESIST,
            "wind": Affinity.RESIST,
        },
        initiative=initiative,
        weapon_accuracy_attributes=list(attack.attributes),
        weapon_accuracy_modifier=attack.accuracy_modifier,
        weapon_damage=attack.damage_bonus,
        weapon_type=attack.damage_type,
        weapon_range=attack.range,
        npc_attacks=[attack],
        npc_trait_rules=[
            "构装体：免疫毒系伤害，对土系伤害抵抗，并免疫中毒。",
            "伤害抵抗：对雷系和风系伤害获得抵抗。",
            "特殊攻击：命中时施加迟缓。",
        ],
        traits=["enemy", "construct", *(traits or [])],
        skills={"伤害抵抗": 1, "特殊攻击": 1},
        permanent_status_immunities={StatusEffect.POISONED},
    )


def _boss_attack() -> NPCAttackProfile:
    return NPCAttackProfile(
        attack_id="furnace-cleave",
        name="熔核横扫",
        attributes=["MIG", "MIG"],
        damage_bonus=5,
        damage_type="fire",
        accuracy_modifier=0,
        range="melee",
        multi_attack=1,
        status_effect_on_hit=StatusEffect.SLOW,
        notes=["危机后由危机效果将此攻击变为多重攻击(2)。"],
    )


def _minion_attack() -> NPCAttackProfile:
    return NPCAttackProfile(
        attack_id="ember-spear",
        name="余烬长枪",
        attributes=["DEX", "MIG"],
        damage_bonus=5,
        damage_type="physical",
        accuracy_modifier=0,
        range="melee",
        status_effect_on_hit=StatusEffect.SLOW,
    )


def install_randomness_guards(service: FUGMHttpService, runtime: Any) -> dict[str, object]:
    """Fail closed on fake RNGs or probe-preloaded outcomes.

    The engine's outcome-replay method is intentionally left intact: normal
    provisional-check acceptance and post-check choices use it to replay the
    already recorded *real* roll without rolling a second time.  The probe
    never invokes or preloads that method itself.
    """

    engines = {
        "combat": runtime.app.interceptor.rules_engine,
        "character_creation": runtime.app.character_creation_manager.rules_engine,
    }
    details: dict[str, object] = {}
    for label, engine in engines.items():
        rng = getattr(engine, "_rng", None)
        if type(rng) is not random.Random:
            raise RuntimeError(
                f"FIXTURE_INVALID: {label} RNG must be random.Random, got "
                f"{type(rng).__module__}.{type(rng).__name__}"
            )
        queued = list(getattr(engine, "_forced_check_outcomes", []) or [])
        if queued:
            raise RuntimeError(
                f"FIXTURE_INVALID: {label} has preloaded check outcomes"
            )
        details[label] = {
            "rng_type": f"{type(rng).__module__}.{type(rng).__name__}",
            "preloaded_outcome_count": 0,
            "probe_outcome_preload_used": False,
            "internal_real_roll_replay_preserved": True,
        }
    return details


def assert_no_pending_outcome_replay(runtime: Any) -> None:
    """The normal replay queue must be empty at every HTTP transaction edge."""

    engines = {
        "combat": runtime.app.interceptor.rules_engine,
        "character_creation": runtime.app.character_creation_manager.rules_engine,
    }
    for label, engine in engines.items():
        pending = list(getattr(engine, "_forced_check_outcomes", []) or [])
        if pending:
            raise RuntimeError(
                f"FIXTURE_INVALID: {label} leaked {len(pending)} replay outcomes "
                "across an HTTP transaction boundary"
            )


def install_resolution_capture(runtime: Any) -> list[dict[str, object]]:
    """Capture real authoritative ActionResolution payloads inside the probe.

    HTTP tool receipts intentionally expose only stable public/audit fields.
    This probe-local wrapper preserves the underlying rule payload needed to
    verify rolls, reaction redirection, minor-action timing and teamwork.  It
    never changes an action, result or RNG state.
    """

    interceptor = runtime.app.interceptor
    original = interceptor.resolve
    records: list[dict[str, object]] = []

    def capture(action: Any) -> Any:
        try:
            resolution = original(action)
        except BaseException as exc:
            records.append(
                {
                    "ok": False,
                    "action": _jsonable(action),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            raise
        records.append(
            {
                "ok": True,
                "action": _jsonable(action),
                "rules_text": str(getattr(resolution, "rules_text", "") or ""),
                "payload": _jsonable(getattr(resolution, "payload", {}) or {}),
            }
        )
        return resolution

    interceptor.resolve = capture
    return records


def build_boss_fixture(
    service: FUGMHttpService,
    *,
    campaign_id: str = CAMPAIGN_ID,
    session_id: str = SESSION_ID,
    channel_id: str = CHANNEL_ID,
) -> tuple[Any, dict[str, object]]:
    """Construct the complete battle using only authoritative Python APIs."""

    runtime = service._runtime(campaign_id, auto_load=False)
    app = runtime.app
    # Character creation fate dice and combat dice share the same true seeded
    # engine.  This prevents an unrecorded second entropy source in the fixture.
    app.character_creation_manager.rules_engine = app.interceptor.rules_engine
    front = app.character_creation_manager.create_player_character(_front_profile()).character
    support = app.character_creation_manager.create_player_character(_support_profile()).character
    app.interceptor.economy_manager.equip_items(FRONT_HERO, ["符文盾"])
    app.interceptor.economy_manager.configure_loadout(
        SUPPORT_HERO,
        {"main_hand": "晨星匕首", "off_hand": "暮影匕首"},
    )

    boss = _base_enemy(
        BOSS_NAME,
        initiative=9,
        attack=_boss_attack(),
        traits=["boss"],
    )
    minion = _base_enemy(
        MINION_NAME,
        initiative=9,
        attack=_minion_attack(),
    )
    # A Champion(3) gains three NPC skills in addition to the construct's two
    # starting skills.  All three are represented by executable sheet state,
    # not merely prose labels.
    boss.skills.update({"危机效果": 1, "特殊行动": 1, "伤害免疫": 1})
    boss.affinities["ice"] = Affinity.IMMUNE
    boss.npc_trait_rules.extend(
        [
            "伤害免疫：免疫冰系伤害。",
            "危机效果：熔核横扫在危机状态下获得多重攻击(2)。",
            "特殊行动【炉心蓄力】：下一次攻击造成10点额外伤害。",
        ]
    )
    boss.npc_ability_profiles.extend(
        [
            NPCAbilityProfile(
                ability_id="furnace-crisis-sweep",
                name="炉心连扫",
                source_skill="危机效果",
                trigger="enter_crisis",
                effect_type="grant_multiattack",
                target_scope="self",
                attack_name="熔核横扫",
                multi_attack=2,
                once_per_scene=True,
                description="进入危机时，熔核横扫获得多重攻击(2)。",
            ),
            NPCAbilityProfile(
                ability_id="furnace-charge",
                name="炉心蓄力",
                source_skill="特殊行动",
                trigger="skill_action",
                effect_type="prepare_attack_damage",
                target_scope="self",
                amount=10,
                description="准备下一次攻击，使其造成10点额外伤害。",
            ),
        ]
    )
    app.character_manager.add(boss)
    app.character_manager.add(minion)
    encounter = EncounterManager(app.character_manager, app.conflict_manager)
    encounter.apply_rank_template(
        BOSS_NAME,
        EnemyRank.CHAMPION,
        champion_value=3,
        # FU Core p101/p300: a Boss is at least a minor villain, whose fixed
        # initial Ultima Point budget is five (major 10, supreme 15).
        ultima_points=5,
        is_villain=True,
    )
    app.conflict_manager.register_enemy(MINION_NAME, EnemyRank.SOLDIER)
    app.conflict_manager.state.escalation_stages[BOSS_NAME] = [
        EscalationStage(
            name="炉心解放",
            ultima_points=0,
            transition_kind="boss_phase",
            hp_restore=None,
            mp_restore=None,
            action_count=3,
            added_abilities=["灼热外壳"],
            affinity_changes={"fire": Affinity.RESIST},
            public_cue=(
                "赤炉大将的外壳崩裂，完整炉心形态从熔光中站起；"
                "火焰在新外壳上滑散，公开显示它已获得火系抵抗。"
            ),
            note="全新阶段恢复完整 HP/MP；危机状态不减少每轮三个行动。",
        )
    ]
    app.conflict_manager.state.current_escalation_stage[BOSS_NAME] = -1

    app.world_state.apply_party_sheet(
        PartySheet(
            group_concept="追入熔炉核心的双人调查队",
            shared_goal="击败赤炉大将并关闭失控炉心",
            starting_region="灰港地底熔炉",
            members=[
                PartyMemberEntry(
                    player_name=FRONT_PLAYER,
                    hero_name=FRONT_HERO,
                    identity=front.identity,
                    theme=front.theme,
                    origin=front.origin,
                    classes=dict(front.classes),
                    skills=dict(front.skills),
                    equipment=list(front.equipment),
                ),
                PartyMemberEntry(
                    player_name=SUPPORT_PLAYER,
                    hero_name=SUPPORT_HERO,
                    identity=support.identity,
                    theme=support.theme,
                    origin=support.origin,
                    classes=dict(support.classes),
                    skills=dict(support.skills),
                    equipment=list(support.equipment),
                ),
            ],
        )
    )
    app.world_state.commit_story_item_action(
        operation="place",
        item_name="炉心安全栓",
        actor="GM",
        scene_location="灰港地底熔炉",
        public_fact="【炉心安全栓】位于炉心控制台上，所有参战者都能看见。",
        source="isolated_boss_probe_fixture",
        to_location="灰港地底熔炉",
        state_note="辅助燃料仍连接",
    )
    app.scene_manager.start_scene(
        "熔炉核心决战",
        SceneType.CONFLICT,
        location="灰港地底熔炉",
        participants=[FRONT_HERO, SUPPORT_HERO, BOSS_NAME, MINION_NAME],
        objective="击败赤炉大将并关闭炉心",
        summary="炉心即将失控，赤炉大将以三个行动压制两名英雄。",
    )
    # Boss initiative 12 after Champion(3), so the enemy side starts.  Rank
    # bonus turns are queued by ConflictManager and must alternate with PCs.
    turn_order = app.conflict_manager.start_scene_from_initiative(
        "熔炉核心决战",
        [FRONT_HERO, SUPPORT_HERO],
        [BOSS_NAME, MINION_NAME],
        players_first=False,
    )
    skill_windows: list[dict[str, object]] = []
    for hero in (front, support):
        outcome = app.interceptor.skill_lifecycle.trigger(
            "conflict_start",
            hero,
            visible_targets=[BOSS_NAME, MINION_NAME],
        )
        app.interceptor._capture_skill_lifecycle(outcome)
        skill_windows.extend(list(outcome.windows))

    service.session_gates.activate(
        campaign_id,
        channel_id,
        session_id,
        status="adventure",
        reason="isolated official-DeepSeek Boss probe",
    )
    save_path = app.save_campaign_memory(campaign_id)
    runtime.last_saved_path = str(save_path)
    fixture = validate_fixture(runtime)
    fixture.update(
        {
            "turn_order": turn_order,
            "skill_windows": _jsonable(skill_windows),
            "save_path_relative": str(Path(save_path).relative_to(service.data_root)),
        }
    )
    return runtime, fixture


def validate_fixture(runtime: Any) -> dict[str, object]:
    app = runtime.app
    front = app.character_manager.get(FRONT_HERO)
    support = app.character_manager.get(SUPPORT_HERO)
    boss = app.character_manager.get(BOSS_NAME)
    minion = app.character_manager.get(MINION_NAME)
    conflict = app.conflict_manager.state
    checks = {
        "two_players": conflict.player_side == [FRONT_HERO, SUPPORT_HERO],
        "front_skill_budget": sum(front.skills.values()) == 5,
        "support_skill_budget": sum(support.skills.values()) == 5,
        "front_two_shields": bool(front.equipped_shield)
        and bool(front.equipped_main_hand)
        and "盾" in front.equipped_shield
        and "盾" in front.equipped_main_hand,
        "front_dual_shield_attributes": front.weapon_accuracy_attributes == ["MIG", "MIG"],
        "front_defense_mastery_damage": front.weapon_damage == 7,
        "front_hp_without_unselected_iron_wall": front.max_hp == 65,
        "focused_mind_mp": support.max_mp == 58,
        "support_spells": set(support.spells) == {"元素幕障", "炎弹"},
        "support_dual_wield_loadout": (
            support.equipped_main_hand == "晨星匕首"
            and support.equipped_off_hand == "暮影匕首"
            and support.equipment_templates.get("晨星匕首") == "钢匕首"
            and support.equipment_templates.get("暮影匕首") == "钢匕首"
        ),
        "boss_champion_three_hp": boss.max_hp == minion.max_hp * 3,
        "boss_champion_mp": boss.max_mp == minion.max_mp * 2,
        "above_standard_difficult_budget_disclosed": (
            conflict.enemy_side == [BOSS_NAME, MINION_NAME]
            and conflict.enemy_ranks.get(BOSS_NAME) == EnemyRank.CHAMPION
            and conflict.enemy_action_counts.get(BOSS_NAME) == 3
            and conflict.enemy_ranks.get(MINION_NAME) == EnemyRank.SOLDIER
        ),
        "base_construct_initiative": minion.initiative == 9,
        "base_construct_defenses": minion.defenses == {
            "physical": 10,
            "magic": 8,
        },
        "base_attack_raw_budget": all(
            attack.accuracy_modifier == 0 and attack.damage_bonus == 5
            for attack in (boss.npc_attacks[0], minion.npc_attacks[0])
        ),
        "construct_species_affinities": all(
            actor.affinities.get("poison") == Affinity.IMMUNE
            and actor.affinities.get("earth") == Affinity.RESIST
            for actor in (boss, minion)
        ),
        "construct_poison_status_immunity": all(
            StatusEffect.POISONED in actor.permanent_status_immunities
            for actor in (boss, minion)
        ),
        "construct_two_starting_skills": len(minion.skills) == 2,
        "boss_five_skills": len(boss.skills) == 5,
        "boss_three_actions": conflict.enemy_action_counts.get(BOSS_NAME) == 3,
        "boss_initiative_bonus": boss.initiative == 12,
        "boss_is_villain": BOSS_NAME in conflict.villains,
        "boss_has_phase": len(conflict.escalation_stages.get(BOSS_NAME, [])) == 1,
        "boss_phase_three_actions": (
            conflict.escalation_stages[BOSS_NAME][0].action_count == 3
        ),
        "boss_minor_villain_five_ultima": (
            conflict.ultima_points.get(BOSS_NAME) == 5
        ),
        "conflict_active": conflict.active,
        "story_item_registered": (
            app.world_state.find_story_item(name="炉心安全栓") is not None
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError("FIXTURE_INVALID: " + ", ".join(failed))
    return {
        "ok": True,
        "checks": checks,
        "characters": {
            actor.name: {
                "level": actor.level,
                "hp": actor.hp,
                "max_hp": actor.max_hp,
                "mp": actor.mp,
                "max_mp": actor.max_mp,
                "initiative": actor.initiative,
                "defenses": _jsonable(actor.defenses),
                "affinities": _jsonable(actor.affinities),
                "status_immunities": sorted(
                    status.value for status in actor.permanent_status_immunities
                ),
                "classes": dict(actor.classes),
                "skills": dict(actor.skills),
                "spells": list(actor.spells),
                "equipment": list(actor.equipment),
                "equipped_main_hand": actor.equipped_main_hand,
                "equipped_off_hand": actor.equipped_off_hand,
                "equipped_shield": actor.equipped_shield,
                "weapon_accuracy_attributes": list(actor.weapon_accuracy_attributes),
                "weapon_damage": actor.weapon_damage,
            }
            for actor in (front, support, boss, minion)
        },
        "boss_rank": "champion",
        "boss_champion_value": 3,
        "boss_phase_count": 2,
        "villain": {
            "type": "minor",
            "initial_ultima_points": 5,
            "zero_hp_escape_cost": 1,
            "expected_remaining_after_unspent_escape": 4,
        },
        "encounter_budget": {
            "encounter_value": 4,
            "two_pc_difficult_baseline": 3,
            "over_budget_by": 1,
            "difficulty_label": "above_standard_difficult_boss",
            "rules_legality": (
                "legal encounter above the recommended two-PC difficult baseline"
            ),
            "reason": (
                "the extra Soldier is retained to exercise real multi(2) and "
                "dual-wield attacks against distinct targets"
            ),
        },
    }


def classify_error(
    error: BaseException | str | None = None,
    *,
    http_status: int | None = None,
    response: Mapping[str, Any] | None = None,
) -> str:
    """Map transport/model/rules failures to one explicit stable category."""

    body = dict(response or {})
    message = " ".join(
        str(part or "")
        for part in (
            error,
            body.get("agent_error"),
            body.get("error"),
            body.get("message"),
            body.get("error_code"),
            body.get("provider_error_category"),
            dict(body.get("agent_loop") or {}).get("terminal_reason"),
        )
    ).lower()
    if "fixture_invalid" in message:
        return "FIXTURE_INVALID"
    if any(
        token in message
        for token in ("timeout", "timed out", "deadline", "超时", "截止")
    ):
        return "PROVIDER_TIMEOUT"
    if "empty" in message or "空回复" in message or "空响应" in message:
        return "PROVIDER_EMPTY_RESPONSE"
    provider_category = str(body.get("provider_error_category") or "").strip()
    if provider_category and any(
        token in provider_category.lower() for token in ("schema", "json", "parse")
    ):
        return "PROVIDER_SCHEMA"
    if provider_category:
        # A non-empty provider category is never allowed to disappear behind
        # an outer HTTP 200/ok=true envelope.  Specific timeout/empty/schema
        # cases were classified above; every remaining upstream category is
        # a provider HTTP/transport failure.
        return "PROVIDER_HTTP"
    if any(
        token in message
        for token in (
            "rate_limit",
            "authentication",
            "account_inactive",
            "forbidden",
            "transport",
            "circuit_open",
            "provider_failure",
        )
    ):
        return "PROVIDER_HTTP"
    if any(
        token in message
        for token in (
            "iteration_exhausted",
            "unresolved",
            "not_applicable",
        )
    ):
        return "MODEL_TOOL_MISSING"
    if http_status is not None and http_status >= 500:
        return "PROVIDER_HTTP"
    if any(token in message for token in ("schema", "json", "parse", "解析")):
        return "PROVIDER_SCHEMA"
    if any(token in message for token in ("tool missing", "未调用工具", "没有调用工具")):
        return "MODEL_TOOL_MISSING"
    if any(token in message for token in ("tool rejected", "工具被拒绝", "工具调用被拒绝")):
        return "MODEL_TOOL_REJECTED"
    if any(token in message for token in ("receipt", "error_code", "规则回执")):
        return "RULE_RECEIPT_ERROR"
    if "window" in message or "待决" in message:
        return "UNEXPECTED_DECISION_WINDOW"
    if http_status is not None and http_status >= 400:
        return "PROVIDER_HTTP"
    return "INTERNAL_ERROR"


def response_error_category(
    error: BaseException | None,
    *,
    http_status: int | None,
    response: Mapping[str, Any],
) -> str:
    """Catch failures even when the HTTP envelope itself says ``ok: true``."""

    body = dict(response or {})
    provider_error = str(body.get("provider_error_category") or "").strip()
    agent_error = str(body.get("agent_error") or "").strip()
    terminal_reason = str(
        dict(body.get("agent_loop") or {}).get("terminal_reason") or ""
    ).strip()
    terminal_error = terminal_reason in {
        "provider_failure",
        "deadline",
        "iteration_exhausted",
        "unresolved",
        "not_applicable",
        "exception",
    }
    if (
        error is not None
        or http_status != 200
        or not bool(body.get("ok", True))
        or provider_error
        or agent_error
        or terminal_error
    ):
        return classify_error(error, http_status=http_status, response=body)
    return ""


def reviewer_false_rejection(
    response: Mapping[str, Any],
    *,
    state_before: Mapping[str, Any],
    requested_message: str,
) -> dict[str, object] | None:
    """Identify a semantic reviewer rejecting an authoritatively legal proposal.

    A completed model turn with no receipt is not automatically a missing-tool
    failure when the tool proposal was repeatedly blocked by the grounding
    reviewer.  This function only calls it a *false* rejection when the
    pre-turn state proves either the requested skill, an exactly mapped known
    spell cast, or the complete dual-wield loadout and every
    player-message-to-argument mapping.  A later successful authoritative
    receipt in the same HTTP turn proves that the earlier reviewer rejection
    was recovered, so it must not be promoted to the turn's terminal error.
    """

    trace = [
        dict(item)
        for item in list(
            response.get("tool_trace") or response.get("agent_trace") or []
        )
        if isinstance(item, Mapping)
    ]
    successful_top_level_tools = {
        str(item.get("tool_name") or "")
        for item in list(response.get("tool_receipts") or [])
        if isinstance(item, Mapping)
        and bool(item.get("ok"))
        and not bool(item.get("rolled_back"))
    }
    rejected: list[dict[str, Any]] = []
    for index, item in enumerate(trace):
        if (
            str(item.get("protocol_error") or "")
            != "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
        ):
            continue
        tool_name = str(item.get("tool_name") or "")
        recovered = False
        for later in trace[index + 1 :]:
            if str(later.get("tool_name") or "") != tool_name:
                continue
            if str(later.get("protocol_error") or ""):
                continue
            receipt = later.get("receipt")
            if isinstance(receipt, Mapping) and bool(receipt.get("ok")):
                recovered = True
                break
            if tool_name and tool_name in successful_top_level_tools:
                recovered = True
                break
        if not recovered:
            rejected.append(item)
    if not rejected:
        return None
    actor = str(state_before.get("actor") or "")
    actor_state = dict(
        dict(state_before.get("combatants") or {}).get(actor) or {}
    )
    owned_skills = {
        str(name)
        for name, rank in dict(actor_state.get("skills") or {}).items()
        if int(rank or 0) > 0
    }
    requested_owned = sorted(
        skill for skill in owned_skills if skill and skill in requested_message
    )
    if requested_owned:
        return {
            "actor": actor,
            "requested_owned_skills": requested_owned,
            "reviewer_protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            "reviewer_rejection_count": len(rejected),
            "reply": str(response.get("reply") or ""),
        }

    # Spell names live in a separate authoritative character-card field from
    # skills.  The Boss probe deliberately casts a known spell before its
    # later weapon/check coverage; if the compact reviewer projection omits
    # that field, the core model can still propose the exact action while the
    # semantic reviewer falsely rejects it.  Require the complete typed Spell
    # proposal to remain literal in the player's message so this detector does
    # not bless an invented spell, element, target, or free-form effect.
    owned_spells = {
        str(name or "").strip()
        for name in list(actor_state.get("spells") or [])
        if str(name or "").strip()
    }
    clean_message = str(requested_message or "").strip()
    grounded_spell_proposals: list[dict[str, object]] = []
    allowed_spell_arguments = {
        "action_type",
        "actor",
        "target",
        "timing",
        "details",
        "source_event_id",
    }
    allowed_spell_details = {"spell_name", "spell", "element", "targets"}
    for item in rejected:
        if str(item.get("tool_name") or "") != "perform_character_action":
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        if set(arguments) - allowed_spell_arguments:
            continue
        details = arguments.get("details")
        if (
            not isinstance(details, Mapping)
            or set(details) - allowed_spell_details
        ):
            continue
        spell_name = str(
            details.get("spell_name") or details.get("spell") or ""
        ).strip()
        element = str(details.get("element") or "").strip()
        raw_targets = details.get("targets")
        if not isinstance(raw_targets, (list, tuple)):
            continue
        target_names = [str(target or "").strip() for target in raw_targets]
        top_target = str(arguments.get("target") or "").strip()
        combatant_names = {
            str(name or "").strip()
            for name in dict(state_before.get("combatants") or {})
            if str(name or "").strip()
        }
        mappings = {
            "action_type_spell": (
                str(arguments.get("action_type") or "").strip().lower()
                == "spell"
            ),
            "actor": str(arguments.get("actor") or "").strip() == actor,
            "timing_immediate": (
                str(arguments.get("timing") or "").strip().lower()
                == "immediate"
            ),
            "spell_is_known": bool(spell_name) and spell_name in owned_spells,
            "spell_named_by_player": bool(spell_name)
            and spell_name in clean_message,
            "targets_present": bool(target_names) and all(target_names),
            "top_target_matches_first_target": bool(target_names)
            and top_target == target_names[0],
            "all_targets_named_by_player": bool(target_names)
            and all(target in clean_message for target in target_names),
            "all_targets_authoritative_combatants": bool(target_names)
            and all(target in combatant_names for target in target_names),
            "element_named_by_player": not element or element in clean_message,
        }
        if not all(mappings.values()):
            continue
        grounded_spell_proposals.append(
            {
                "proposed_spell": spell_name,
                "proposed_element": element,
                "proposed_targets": target_names,
                "message_argument_mapping": mappings,
            }
        )
    if grounded_spell_proposals:
        first_grounded_spell = grounded_spell_proposals[0]
        return {
            "actor": actor,
            "grounded_capability": "known_spell_cast",
            "requested_owned_spells": [
                str(first_grounded_spell["proposed_spell"])
            ],
            **first_grounded_spell,
            "reviewer_protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
            "reviewer_rejection_count": len(rejected),
            "grounded_rejection_count": len(grounded_spell_proposals),
            "reply": str(response.get("reply") or ""),
        }

    main_hand = str(actor_state.get("equipped_main_hand") or "").strip()
    off_hand = str(actor_state.get("equipped_off_hand") or "").strip()
    equipment = {
        str(item or "").strip()
        for item in list(actor_state.get("equipment") or [])
        if str(item or "").strip()
    }
    if (
        not actor
        or not main_hand
        or not off_hand
        or main_hand == "徒手攻击"
        or off_hand == "徒手攻击"
        or main_hand not in equipment
        or off_hand not in equipment
        or actor not in clean_message
        or "双武器" not in clean_message
        or main_hand not in clean_message
        or off_hand not in clean_message
    ):
        return None

    grounded_dual_proposals: list[dict[str, object]] = []
    for item in rejected:
        if str(item.get("tool_name") or "") != "perform_character_action":
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        details = arguments.get("details")
        if not isinstance(details, Mapping):
            continue
        targets = details.get("targets")
        if not isinstance(targets, (list, tuple)):
            continue
        target_names = [str(target or "").strip() for target in targets]
        top_target = str(arguments.get("target") or "").strip()
        active_hostiles = {
            str(target or "").strip()
            for target in list(
                dict(state_before.get("resolution_status") or {}).get(
                    "active_hostiles"
                )
                or []
            )
            if str(target or "").strip()
        }
        mappings = {
            "action_type_attack": (
                str(arguments.get("action_type") or "").strip().lower()
                == "attack"
            ),
            "actor": str(arguments.get("actor") or "").strip() == actor,
            "dual_wield": details.get("dual_wield") is True,
            "two_targets": len(target_names) == 2 and all(target_names),
            "top_target_matches_main_hand": bool(target_names)
            and top_target == target_names[0],
            "all_targets_named_by_player": bool(target_names)
            and all(target in clean_message for target in target_names),
            "all_targets_authoritatively_active": bool(target_names)
            and all(target in active_hostiles for target in target_names),
        }
        same_target = len(target_names) == 2 and target_names[0] == target_names[1]
        mappings["same_target_explicitly_applies_to_both_weapons"] = (
            not same_target
            or "都攻击" in clean_message
            or "同一目标" in clean_message
        )
        if not all(mappings.values()):
            continue
        grounded_dual_proposals.append(
            {
                "proposed_targets": target_names,
                "same_target": same_target,
                "message_argument_mapping": mappings,
            }
        )
    if not grounded_dual_proposals:
        return None
    first_grounded = grounded_dual_proposals[0]
    return {
        "actor": actor,
        "grounded_capability": "dual_wield",
        "authoritative_loadout": {
            "main_hand": main_hand,
            "off_hand": off_hand,
        },
        **first_grounded,
        "reviewer_protocol_error": "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED",
        "reviewer_rejection_count": len(rejected),
        "grounded_rejection_count": len(grounded_dual_proposals),
        "reply": str(response.get("reply") or ""),
    }


def repeated_rule_action_rejection(
    response: Mapping[str, Any],
    *,
    receipt_failures: Sequence[Mapping[str, Any]],
) -> dict[str, object] | None:
    """Detect an agent retrying one rejected rules action without correction.

    A single ``RULE_ACTION_REJECTED`` is an ordinary authoritative rules
    receipt.  Three unrecovered identical rejections plus at least three
    consecutive submissions of the same tool arguments means the model loop
    did not apply the retryable correction; reporting that as only a generic
    rules error would blame the rejecting rules engine rather than the stuck
    retry loop.
    """

    rejected = [
        dict(item)
        for item in receipt_failures
        if isinstance(item, Mapping)
        and str(item.get("error_code") or "") == "RULE_ACTION_REJECTED"
    ]
    if len(rejected) < 3:
        return None
    failure_signatures = {
        (
            str(item.get("tool_name") or ""),
            str(item.get("error_code") or ""),
            str(item.get("message") or ""),
            str(item.get("correction_hint") or ""),
        )
        for item in rejected
    }
    if len(failure_signatures) != 1:
        return None
    tool_name, error_code, rule_message, correction_hint = next(
        iter(failure_signatures)
    )
    trace = [
        dict(item)
        for item in list(
            response.get("tool_trace") or response.get("agent_trace") or []
        )
        if isinstance(item, Mapping)
        and str(item.get("decision") or "") == "call_tool"
        and str(item.get("tool_name") or "") == tool_name
    ]
    longest_run = 0
    current_run = 0
    previous_fingerprint = ""
    repeated_fingerprint = ""
    for item in trace:
        fingerprint = _canonical_hash(dict(item.get("arguments") or {}))
        if fingerprint == previous_fingerprint:
            current_run += 1
        else:
            current_run = 1
            previous_fingerprint = fingerprint
        if current_run > longest_run:
            longest_run = current_run
            repeated_fingerprint = fingerprint
    if longest_run < 3:
        return None
    return {
        "tool_name": tool_name,
        "rule_error_code": error_code,
        "rule_message": rule_message,
        "correction_hint": correction_hint,
        "identical_attempt_count": longest_run,
        "unchanged_retry_count": longest_run - 1,
        "arguments_sha256": repeated_fingerprint,
        "reason": (
            "The model resubmitted identical rejected tool arguments without "
            "applying the retryable rules correction."
        ),
    }


def turn_response_error(
    error: BaseException | None,
    *,
    http_status: int | None,
    response: Mapping[str, Any],
    state_before: Mapping[str, Any],
    requested_message: str,
    receipt_failures: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, dict[str, object] | None]:
    """Classify one HTTP turn while keeping reviewer failures distinct."""

    category = response_error_category(
        error,
        http_status=http_status,
        response=response,
    )
    repeated_rejection = repeated_rule_action_rejection(
        response,
        receipt_failures=receipt_failures,
    )
    if repeated_rejection is not None:
        category = "MODEL_RULE_RETRY_STUCK"
    elif receipt_failures:
        category = "RULE_RECEIPT_ERROR"
    reviewer_rejection = reviewer_false_rejection(
        response,
        state_before=state_before,
        requested_message=requested_message,
    )
    if (
        reviewer_rejection is not None
        and repeated_rejection is None
        and not receipt_failures
    ):
        body = dict(response or {})
        terminal_reason = str(
            dict(body.get("agent_loop") or {}).get("terminal_reason") or ""
        ).strip()
        provider_category = str(
            body.get("provider_error_category") or ""
        ).strip().lower()
        trace = [
            item
            for item in list(
                body.get("tool_trace") or body.get("agent_trace") or []
            )
            if isinstance(item, Mapping)
        ]
        semantic_reviewer_terminal = (
            error is None
            and http_status == 200
            and bool(body.get("ok", True))
            and provider_category in {"", "unknown"}
            and terminal_reason in {"completed", "iteration_exhausted"}
            and any(
                str(item.get("protocol_error") or "")
                == "SEMANTIC_TOOL_PROPOSAL_NOT_GROUNDED"
                for item in trace
            )
        )
        if semantic_reviewer_terminal or not category:
            # The HTTP coordinator uses ``unknown`` for isolated non-provider
            # failures too.  A completed/iteration-exhausted HTTP 200 whose
            # concrete terminal evidence is the semantic reviewer must not be
            # reported as an upstream HTTP failure.
            category = "REVIEWER_FALSE_REJECTION"
    return category, reviewer_rejection


def production_authority_digest(root: Path) -> dict[str, object]:
    """Hash a production data tree without copying or exposing its contents."""

    resolved = root.expanduser().resolve()
    if not resolved.exists():
        return {"root": str(resolved), "exists": False, "file_count": 0, "bytes": 0, "sha256": ""}
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in sorted((item for item in resolved.rglob("*") if item.is_file()), key=str):
        relative = path.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        try:
            payload = path.read_bytes()
        except (OSError, PermissionError) as exc:
            digest.update(f"<unreadable:{type(exc).__name__}>".encode("utf-8"))
            continue
        digest.update(payload)
        file_count += 1
        byte_count += len(payload)
    return {
        "root": str(resolved),
        "exists": True,
        "file_count": file_count,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


def production_sentinel(root: Path) -> dict[str, object]:
    return {
        "service": _production_snapshot(),
        "authority": production_authority_digest(root),
        "observed_at": utc_now(),
    }


def compare_production_sentinels(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, object]:
    before_service = dict(before.get("service") or {})
    after_service = dict(after.get("service") or {})
    before_authority = dict(before.get("authority") or {})
    after_authority = dict(after.get("authority") or {})
    pid_same = str(before_service.get("pid") or "") == str(after_service.get("pid") or "")
    start_same = str(before_service.get("started_at") or "") == str(
        after_service.get("started_at") or ""
    )
    hash_same = str(before_authority.get("sha256") or "") == str(
        after_authority.get("sha256") or ""
    )
    return {
        "pid_same": pid_same,
        "started_at_same": start_same,
        "authority_hash_same": hash_same,
        "unchanged": pid_same and start_same and hash_same,
        "interpretation": (
            "Production identity and data digest were unchanged."
            if pid_same and start_same and hash_same
            else "Production changed during observation; this probe never writes to port 8765 or that data root, so concurrent production activity must be distinguished from probe impact."
        ),
    }


def assert_live_source(expected_root: Path) -> dict[str, object]:
    imported = Path(str(fu_gm.__file__)).resolve()
    expected = expected_root.expanduser().resolve()
    try:
        imported.relative_to(expected)
    except ValueError as exc:
        raise RuntimeError(
            f"FIXTURE_INVALID: live probe imported {imported}, expected below {expected}"
        ) from exc
    return {"fu_gm_module": str(imported), "expected_root": str(expected), "matched": True}


def state_projection(runtime: Any) -> dict[str, object]:
    """Stable authoritative subset for save/reload equivalence checks."""

    app = runtime.app
    conflict = app.conflict_manager.state
    characters = {}
    for actor in app.character_manager.all():
        characters[actor.name] = {
            "hp": actor.hp,
            "max_hp": actor.max_hp,
            "mp": actor.mp,
            "max_mp": actor.max_mp,
            "crisis_threshold": actor.crisis_threshold,
            "in_crisis": bool(actor.in_crisis),
            "statuses": _jsonable(actor.statuses),
            "trigger_cooldowns": sorted(actor.trigger_cooldowns),
            "guarding": actor.guarding,
            "guarded_target": actor.guarded_target,
            "temporary_affinities": _jsonable(actor.temporary_affinities),
            "defense_bonuses": dict(actor.defense_bonuses),
            "skills": dict(actor.skills),
            "equipment": list(actor.equipment),
            "equipped_main_hand": actor.equipped_main_hand,
            "equipped_off_hand": actor.equipped_off_hand,
            "equipped_shield": actor.equipped_shield,
            "weapon_damage_type_override": actor.weapon_damage_type_override,
            # These are authoritative NPC combat state, not presentation
            # metadata.  In particular, crisis-triggered attack overrides
            # live in npc_skill_effects and must survive save/reload without
            # leaking into a restored full-HP phase.
            "npc_skill_effects": _jsonable(actor.npc_skill_effects),
            "npc_ability_profiles": _jsonable(actor.npc_ability_profiles),
            "npc_attacks": _jsonable(actor.npc_attacks),
        }
    pending = [
        _jsonable(window)
        for window in app.interceptor.decision_window_manager.pending()
        # Successful-check invocation rights rely on an in-memory rollback
        # journal and are intentionally expired by load_campaign_memory().
        # They are real within this process, but are not durable state and
        # therefore must not create a false save/reload mismatch.
        if not bool(window.payload.get("ephemeral_same_runtime"))
    ]
    scene = app.scene_manager.current_scene
    return {
        "characters": characters,
        "conflict": {
            "active": conflict.active,
            "scene_name": conflict.scene_name,
            "round_number": conflict.round_number,
            "turn_order": list(conflict.turn_order),
            "current_turn_index": conflict.current_turn_index,
            "current_bonus_actor": conflict.current_bonus_actor,
            "current_bonus_kind": conflict.current_bonus_kind,
            "queued_turns": list(conflict.queued_turns),
            "queued_turn_kinds": list(conflict.queued_turn_kinds),
            "turn_serial": conflict.turn_serial,
            "acted_this_round": list(conflict.acted_this_round),
            "pending_assists": {
                leader: list(supporters)
                for leader, supporters in conflict.pending_assists.items()
            },
            "action_penalties": dict(conflict.action_penalties),
            "ultima_points": dict(conflict.ultima_points),
            "enemy_action_counts": dict(conflict.enemy_action_counts),
            "escalation_stages": _jsonable(conflict.escalation_stages),
            "current_escalation_stage": dict(conflict.current_escalation_stage),
            "escaped": sorted(conflict.escaped_combatants),
            "surrendered": sorted(conflict.surrendered_combatants),
            "defeated": sorted(conflict.defeated_combatants),
            "active_effects": _jsonable(conflict.active_effects),
            "combat_log": _jsonable(conflict.combat_log),
        },
        "session": {
            "active": bool(app.session_ledger.active),
            "session_id": str(app.session_ledger.session_id or ""),
            "ultima_spent": _jsonable(getattr(app.session_ledger, "ultima_spent", {})),
        },
        "pending_windows": pending,
        "scene": _jsonable(scene) if scene is not None else None,
        "story_items": _jsonable(app.world_state.story_items),
    }


def verify_persistence(data_root: Path, campaign_id: str, runtime: Any) -> dict[str, object]:
    runtime.app.save_campaign_memory(campaign_id)
    before = state_projection(runtime)
    reloaded_service = FUGMHttpService(data_root=data_root, use_llm=False)
    reloaded = reloaded_service._runtime(campaign_id)
    after = state_projection(reloaded)
    return {
        "matched": before == after,
        "before_sha256": _canonical_hash(before),
        "after_sha256": _canonical_hash(after),
    }


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk(item, (*path, str(index)))


def extract_rolls(payload: Any, *, turn_id: str = "") -> list[dict[str, object]]:
    """Extract only roll objects actually returned by the authoritative chain."""

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    walked = list(_walk(payload))
    dual_wield_roll_aliases: set[str] = set()
    for _path, item in walked:
        if not isinstance(item, Mapping):
            continue
        for attack in list(item.get("dual_wield_attacks") or []):
            if not isinstance(attack, Mapping):
                continue
            roll = attack.get("roll")
            if isinstance(roll, Mapping):
                dual_wield_roll_aliases.add(_canonical_hash(_jsonable(roll)))
    indexed_rolls_present = any(
        "rolls" in path or "dual_wield_attacks" in path
        for path, _item in walked
    )
    for path, item in walked:
        if not isinstance(item, Mapping):
            continue
        if "dice" not in item or "total" not in item:
            continue
        dice = item.get("dice")
        if not isinstance(dice, (list, tuple)):
            continue
        if (
            "dual_wield_attacks" not in path
            and _canonical_hash(_jsonable(item)) in dual_wield_roll_aliases
        ):
            # A settled dual-wield payload repeats the two authoritative rolls
            # under roll/rolls/check_roll_sequence convenience aliases.  Only
            # dual_wield_attacks is the canonical per-strike collection.
            continue
        occurrence_index: int | None = None
        for collection_name in ("rolls", "dual_wield_attacks"):
            if collection_name not in path:
                continue
            collection_at = path.index(collection_name)
            if collection_at + 1 < len(path):
                try:
                    occurrence_index = int(path[collection_at + 1])
                except (TypeError, ValueError):
                    pass
            break
        if (
            occurrence_index is None
            and indexed_rolls_present
            and path
            and path[-1] == "roll"
        ):
            # The conventional singular ``roll`` aliases the first item in a
            # sibling ``rolls`` list.  Including logical occurrence (not raw
            # path) keeps that alias deduplicated while preserving two truly
            # identical dual-wield strikes at indices 0 and 1.
            occurrence_index = 0
        row = {
            "turn_id": turn_id,
            "path": ".".join(path),
            "occurrence_index": occurrence_index,
            "actor": str(item.get("actor") or ""),
            "target": str(item.get("target") or ""),
            "attributes": _jsonable(item.get("attributes") or []),
            "dice": _jsonable(dice),
            "total": item.get("total"),
            "modifier": item.get("modifier"),
            "high_roll": item.get("high_roll"),
            "target_number": item.get("target_number"),
            "success": item.get("success"),
            "critical_success": item.get("critical_success"),
            "fumble": item.get("fumble"),
            "damage": item.get("damage"),
            "damage_type": str(item.get("damage_type") or ""),
            "applied_affinity": _jsonable(item.get("applied_affinity")),
            "hp_after": item.get("hp_after"),
        }
        # A serialized receipt may expose the same authoritative roll under
        # both ``roll`` and a convenience/result alias.  The path is useful for
        # audit, but must not defeat duplicate suppression.
        fingerprint = _canonical_hash(
            {key: value for key, value in row.items() if key != "path"}
        )
        if fingerprint not in seen:
            seen.add(fingerprint)
            rows.append(row)
    return rows


def append_unique_rolls(
    destination: list[dict[str, object]],
    candidates: Iterable[dict[str, object]],
    *,
    seen: set[str],
) -> None:
    """Keep one row when a real provisional roll is replayed on acceptance."""

    for row in candidates:
        fingerprint = _canonical_hash(
            {
                key: value
                for key, value in row.items()
                if key not in {"turn_id", "path"}
            }
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        destination.append(row)


def _audit_status(checks: Iterable[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("status") or "unknown") for item in checks}
    if "failed" in statuses:
        return "failed"
    if "unknown" in statuses:
        return "unknown"
    return "passed"


def _conflict_events(value: Any, event_type: str) -> list[dict[str, object]]:
    """Return unique serialized conflict events from a resolution payload."""

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for _path, item in _walk(value):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("event_type") or "") != event_type:
            continue
        row = _jsonable(item)
        fingerprint = _canonical_hash(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        rows.append(row)
    return rows


def _is_settled_authoritative_resolution(record: Mapping[str, Any]) -> bool:
    """Return true only for a committed authoritative resolution."""

    if not bool(record.get("ok")):
        return False
    payload = dict(record.get("payload") or {})
    return not (
        payload.get("check_result_provisional") is True
        or payload.get("action_uncommitted") is True
        or bool(payload.get("held_action"))
        or bool(payload.get("turn_held_for_decision"))
    )


def _boss_zero_hp_crossing(
    record: Mapping[str, Any],
    *,
    hp_before: int | None,
) -> dict[str, object] | None:
    """Find explicit settled evidence that Boss HP crossed from positive to zero.

    A Boss phase immediately restores HP, so ``state_after.hp`` and even the
    final roll alias may already show the new form's full HP.  The committed
    payload's ``actual_hp_loss`` is therefore the primary crossing evidence;
    an explicit Boss roll with ``hp_after == 0`` is also sufficient.
    """

    if hp_before is None or hp_before <= 0:
        return None
    action = dict(record.get("action") or {})
    parameters = dict(action.get("parameters") or {})
    payload = dict(record.get("payload") or {})
    roll_targets = {
        str(roll.get("target") or "")
        for roll in extract_rolls(record)
        if isinstance(roll, Mapping)
    }
    event_targets = {
        str(event.get("target") or "")
        for event_type in ("boss_phase", "villain_escape")
        for event in _conflict_events(record, event_type)
    }
    targets_boss = (
        str(parameters.get("target") or "") == BOSS_NAME
        or BOSS_NAME in roll_targets
        or BOSS_NAME in event_targets
    )
    actual_loss: int | None = None
    direct_loss = payload.get("actual_hp_loss")
    if (
        targets_boss
        and isinstance(direct_loss, (int, float))
        and not isinstance(direct_loss, bool)
    ):
        actual_loss = max(0, int(direct_loss))
    if actual_loss is None:
        nested_losses = [
            int(item.get("actual_hp_loss") or 0)
            for _path, item in _walk(payload)
            if isinstance(item, Mapping)
            and str(item.get("target") or "") == BOSS_NAME
            and isinstance(item.get("actual_hp_loss"), (int, float))
            and not isinstance(item.get("actual_hp_loss"), bool)
        ]
        if nested_losses:
            actual_loss = max(nested_losses)
    explicit_zero = any(
        str(roll.get("target") or "") == BOSS_NAME
        and isinstance(roll.get("hp_after"), (int, float))
        and int(roll.get("hp_after")) == 0
        for roll in extract_rolls(record)
    )
    if not explicit_zero and (actual_loss is None or actual_loss < hp_before):
        return None
    return {
        "hp_before": hp_before,
        "actual_hp_loss": actual_loss,
        "explicit_zero_hp_report": explicit_zero,
        "derived_hp_after_damage": (
            max(0, hp_before - actual_loss)
            if actual_loss is not None
            else 0
        ),
    }


def _remaining_hostiles_after_boss_escape(
    state: Mapping[str, Any],
) -> tuple[list[str], bool]:
    """Return remaining hostiles and whether the state exposed a complete list."""

    resolution_status = dict(state.get("resolution_status") or {})
    if "active_hostiles" in resolution_status:
        return (
            [
                str(name)
                for name in list(resolution_status.get("active_hostiles") or [])
                if str(name) and str(name) != BOSS_NAME
            ],
            True,
        )
    combatants = dict(state.get("combatants") or {})
    if MINION_NAME in combatants:
        minion = dict(combatants.get(MINION_NAME) or {})
        inactive = set(state.get("defeated") or []) | set(
            state.get("escaped") or []
        ) | set(state.get("surrendered") or [])
        alive = int(minion.get("hp") or 0) > 0 and MINION_NAME not in inactive
        return ([MINION_NAME] if alive else [], True)
    return [], False


def audit_authoritative_rules(
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Reconcile real rolls and Boss transitions without inventing evidence.

    The authoritative payload remains the source of truth.  This audit checks
    what can be reconstructed exactly from that payload plus the before/after
    state projection.  A field that lacks enough inputs is labelled
    ``unknown`` instead of being treated as a pass.
    """

    roll_checks: list[dict[str, object]] = []
    phase_checks: list[dict[str, object]] = []
    terminal_trigger_checks: list[dict[str, object]] = []
    audited_roll_fingerprints: set[str] = set()

    for turn in turns:
        turn_id = str(turn.get("turn_id") or "")
        before = dict(turn.get("state_before") or {})
        after = dict(turn.get("state_after") or {})
        before_combatants = {
            str(name): dict(details or {})
            for name, details in dict(before.get("combatants") or {}).items()
        }
        hp_cursor = {
            str(name): int(value)
            for name, value in dict(before.get("hp") or {}).items()
            if isinstance(value, (int, float))
        }
        stage_cursor = int(before.get("current_stage", -1))
        authoritative = [
            item
            for item in list(turn.get("authoritative_resolutions") or [])
            if isinstance(item, Mapping)
            and _is_settled_authoritative_resolution(item)
        ]

        for resolution_index, record in enumerate(authoritative):
            action = dict(record.get("action") or {})
            parameters = dict(action.get("parameters") or {})
            action_type = str(
                action.get("action_type") or action.get("type") or ""
            )
            record_phase_events = _conflict_events(record, "boss_phase")
            record_escape_events = _conflict_events(record, "villain_escape")
            boss_hp_before_record = hp_cursor.get(BOSS_NAME)
            stage_before_record = stage_cursor
            zero_hp_crossing = _boss_zero_hp_crossing(
                record,
                hp_before=boss_hp_before_record,
            )
            bonus_markers = {
                "next_damage_bonuses",
                "incoming_damage_bonuses",
                "single_target_skill_effects",
                "prepared_attack_damage",
                "_spell_skill_damage_bonus",
                "_gadget_damage_bonus",
            }
            has_unreconstructed_bonus = any(
                isinstance(item, Mapping)
                and bool(bonus_markers.intersection(str(key) for key in item))
                for _path, item in _walk(record)
            )
            for roll_index, roll in enumerate(
                extract_rolls(record, turn_id=turn_id)
            ):
                roll_fingerprint = _canonical_hash(
                    {
                        key: value
                        for key, value in roll.items()
                        if key not in {"turn_id", "path"}
                    }
                    | {
                        "turn_serial": int(before.get("turn_serial") or 0),
                    }
                )
                if roll_fingerprint in audited_roll_fingerprints:
                    continue
                audited_roll_fingerprints.add(roll_fingerprint)
                checks: list[dict[str, object]] = []
                total = roll.get("total")
                target_number = roll.get("target_number")
                observed_success = roll.get("success")
                critical = roll.get("critical_success")
                fumble = roll.get("fumble")
                if (
                    isinstance(total, (int, float))
                    and isinstance(target_number, (int, float))
                    and isinstance(observed_success, bool)
                    and isinstance(critical, bool)
                    and isinstance(fumble, bool)
                ):
                    expected_success = bool(
                        critical or (total >= target_number and not fumble)
                    )
                    checks.append(
                        {
                            "name": "success_from_total_critical_fumble",
                            "status": (
                                "passed"
                                if observed_success == expected_success
                                else "failed"
                            ),
                            "observed": observed_success,
                            "expected": expected_success,
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": "success_from_total_critical_fumble",
                            "status": "unknown",
                            "reason": "authoritative roll omitted one or more required fields",
                        }
                    )

                target = str(roll.get("target") or "")
                actor = str(roll.get("actor") or parameters.get("actor") or "")
                damage = roll.get("damage")
                high_roll = roll.get("high_roll")
                affinity = str(roll.get("applied_affinity") or "normal")
                actor_before = before_combatants.get(actor, {})
                target_before = before_combatants.get(target, {})
                weapon_damage = parameters.get("weapon_damage")
                if not isinstance(weapon_damage, (int, float)):
                    weapon_damage = actor_before.get("weapon_damage")
                formula_check: dict[str, object]
                if damage is None:
                    formula_check = {
                        "name": "damage_formula",
                        "status": "unknown",
                        "reason": "non-damage check or payload omitted damage",
                    }
                elif observed_success is False:
                    formula_check = {
                        "name": "damage_formula",
                        "status": "passed" if damage == 0 else "failed",
                        "observed": damage,
                        "expected": 0,
                    }
                elif (
                    action_type not in {"Attack", "NPCAct", "RequestRoll"}
                    or not isinstance(high_roll, (int, float))
                    or not isinstance(weapon_damage, (int, float))
                    or not target_before
                    or has_unreconstructed_bonus
                ):
                    formula_check = {
                        "name": "damage_formula",
                        "status": "unknown",
                        "reason": (
                            "skill/spell or additional damage inputs cannot be fully "
                            "reconstructed from this outer authoritative action"
                        ),
                    }
                else:
                    equipment_bonus = int(
                        actor_before.get("equipment_attack_damage_bonus") or 0
                    )
                    expected_damage = max(
                        0, int(high_roll) + int(weapon_damage) + equipment_bonus
                    )
                    mastery_rank = int(
                        dict(target_before.get("skills") or {}).get("防御精通", 0)
                        or 0
                    )
                    mastery_active = bool(
                        str(target_before.get("equipped_shield") or "").strip()
                    )
                    if mastery_rank and mastery_active:
                        expected_damage = max(0, expected_damage - mastery_rank)
                    if affinity == "weak":
                        expected_damage *= 2
                    elif affinity == "resist":
                        expected_damage //= 2
                    elif affinity == "immune":
                        expected_damage = 0
                    elif affinity == "absorb":
                        # RollOutcome stores the absolute amount for absorption.
                        expected_damage = abs(expected_damage)
                    if (
                        bool(target_before.get("guarding"))
                        and expected_damage > 0
                        and affinity != "resist"
                    ):
                        expected_damage //= 2
                    formula_check = {
                        "name": "damage_formula",
                        "status": (
                            "passed"
                            if int(damage) == expected_damage
                            else "failed"
                        ),
                        "observed": damage,
                        "expected": expected_damage,
                        "inputs": {
                            "high_roll": high_roll,
                            "weapon_damage": weapon_damage,
                            "equipment_attack_damage_bonus": equipment_bonus,
                            "defensive_mastery_rank": mastery_rank,
                            "defensive_mastery_active": mastery_active,
                            "guarding": bool(target_before.get("guarding")),
                            "applied_affinity": affinity,
                        },
                    }
                checks.append(formula_check)

                hp_before = hp_cursor.get(target)
                hp_after = roll.get("hp_after")
                if damage is None or hp_before is None or hp_after is None:
                    hp_check: dict[str, object] = {
                        "name": "damage_hp_delta",
                        "status": "unknown",
                        "reason": "damage, target HP before, or roll.hp_after is absent",
                    }
                elif record_phase_events and target == BOSS_NAME:
                    hp_check = {
                        "name": "damage_hp_delta",
                        "status": "unknown",
                        "reason": "the hit crossed a Boss phase boundary and restored a new form",
                        "hp_before": hp_before,
                        "reported_hp_after": hp_after,
                    }
                else:
                    target_max_hp = int(target_before.get("max_hp") or hp_before)
                    expected_hp_after = (
                        min(target_max_hp, hp_before + int(damage))
                        if affinity == "absorb" and observed_success is True
                        else max(
                            0,
                            hp_before
                            - (int(damage) if observed_success is True else 0),
                        )
                    )
                    hp_check = {
                        "name": "damage_hp_delta",
                        "status": (
                            "passed"
                            if int(hp_after) == expected_hp_after
                            else "failed"
                        ),
                        "hp_before": hp_before,
                        "damage": damage,
                        "reported_hp_after": hp_after,
                        "expected_hp_after": expected_hp_after,
                    }
                checks.append(hp_check)
                if target and isinstance(hp_after, (int, float)):
                    hp_cursor[target] = int(hp_after)
                roll_checks.append(
                    {
                        "turn_id": turn_id,
                        "resolution_index": resolution_index,
                        "roll_index": roll_index,
                        "actor": actor,
                        "target": target,
                        "dice": roll.get("dice"),
                        "status": _audit_status(checks),
                        "checks": checks,
                    }
                )

            if zero_hp_crossing is not None and stage_before_record == -1:
                event = next(
                    (
                        item
                        for item in record_phase_events
                        if str(item.get("target") or "") == BOSS_NAME
                    ),
                    {},
                )
                after_boss = dict(
                    dict(after.get("combatants") or {}).get(BOSS_NAME) or {}
                )
                event_text = " ".join(
                    [
                        str(event.get("summary") or ""),
                        str(record.get("rules_text") or ""),
                    ]
                )
                stage_after = int(after.get("current_stage", -1))
                phase_rule_checks = [
                    {
                        "name": "positive_hp_crossed_zero",
                        "status": "passed",
                        **zero_hp_crossing,
                    },
                    {
                        "name": "stage_minus_one_to_zero_same_transaction",
                        "status": "passed" if stage_after == 0 else "failed",
                        "observed": [stage_before_record, stage_after],
                        "expected": [-1, 0],
                    },
                    {
                        "name": "boss_phase_event_same_resolution",
                        "status": "passed" if event else "failed",
                    },
                    {
                        "name": "public_phase_cue",
                        "status": (
                            "passed"
                            if "公开显示它已获得火系抵抗" in event_text
                            else "failed"
                        ),
                    },
                    {
                        "name": "full_hp_restore",
                        "status": (
                            "passed"
                            if int(after_boss.get("hp") or -1)
                            == int(after_boss.get("max_hp") or -2)
                            and int(event.get("hp_after") or -1)
                            == int(after_boss.get("max_hp") or -2)
                            else "failed"
                        ),
                    },
                    {
                        "name": "full_mp_restore",
                        "status": (
                            "passed"
                            if int(after_boss.get("mp") or -1)
                            == int(after_boss.get("max_mp") or -2)
                            and int(event.get("mp_after") or -1)
                            == int(after_boss.get("max_mp") or -2)
                            else "failed"
                        ),
                    },
                    {
                        "name": "not_prematurely_defeated",
                        "status": (
                            "passed"
                            if BOSS_NAME not in set(after.get("defeated") or [])
                            else "failed"
                        ),
                    },
                    {
                        "name": "fire_affinity_state",
                        "status": (
                            "passed"
                            if str(
                                dict(
                                    after_boss.get("temporary_affinities") or {}
                                ).get("fire")
                                or ""
                            )
                            == "resist"
                            else "failed"
                        ),
                    },
                ]
                phase_checks.append(
                    {
                        "turn_id": turn_id,
                        "resolution_index": resolution_index,
                        "stage_before": stage_before_record,
                        "stage_after": stage_after,
                        "trigger": zero_hp_crossing,
                        "status": _audit_status(phase_rule_checks),
                        "checks": phase_rule_checks,
                    }
                )

            if zero_hp_crossing is not None and stage_before_record == 0:
                escape_event = next(
                    (
                        item
                        for item in record_escape_events
                        if str(item.get("target") or "") == BOSS_NAME
                    ),
                    {},
                )
                ultima_before = before.get("ultima")
                ultima_after = after.get("ultima")
                exact_ultima_delta = (
                    isinstance(ultima_before, (int, float))
                    and not isinstance(ultima_before, bool)
                    and isinstance(ultima_after, (int, float))
                    and not isinstance(ultima_after, bool)
                    and int(ultima_before) == 5
                    and int(ultima_after) == 4
                    and int(ultima_before) - int(ultima_after) == 1
                )
                remaining_hostiles, hostiles_known = (
                    _remaining_hostiles_after_boss_escape(after)
                )
                conflict_active = after.get("active")
                if not hostiles_known or not isinstance(conflict_active, bool):
                    activity_check: dict[str, object] = {
                        "name": "conflict_activity_after_escape",
                        "status": "unknown",
                        "reason": (
                            "active flag or complete active_hostiles state is absent"
                        ),
                    }
                elif remaining_hostiles:
                    activity_check = {
                        "name": "conflict_activity_after_escape",
                        "status": "passed" if conflict_active is True else "failed",
                        "remaining_hostiles": remaining_hostiles,
                        "observed_active": conflict_active,
                        "expected_active": True,
                    }
                else:
                    activity_check = {
                        "name": "conflict_activity_after_escape",
                        "status": "passed" if conflict_active is False else "failed",
                        "remaining_hostiles": [],
                        "observed_active": conflict_active,
                        "expected_active": False,
                    }
                terminal_checks = [
                    {
                        "name": "positive_hp_crossed_zero",
                        "status": "passed",
                        **zero_hp_crossing,
                    },
                    {
                        "name": "villain_escape_event_same_resolution",
                        "status": "passed" if escape_event else "failed",
                    },
                    {
                        "name": "one_ultima_spent_on_zero_hp_escape",
                        "status": (
                            "passed"
                            if int(escape_event.get("ultima_spent") or 0) == 1
                            else "failed"
                        ),
                    },
                    {
                        "name": "minor_villain_ultima_exactly_five_to_four",
                        "status": "passed" if exact_ultima_delta else "failed",
                        "before": ultima_before,
                        "after": ultima_after,
                        "expected": [5, 4],
                    },
                    {
                        "name": "boss_entered_escaped_state",
                        "status": (
                            "passed"
                            if BOSS_NAME in set(after.get("escaped") or [])
                            else "failed"
                        ),
                    },
                    activity_check,
                ]
                terminal_trigger_checks.append(
                    {
                        "turn_id": turn_id,
                        "resolution_index": resolution_index,
                        "stage_before": stage_before_record,
                        "trigger": zero_hp_crossing,
                        "boss_escaped": BOSS_NAME
                        in set(after.get("escaped") or []),
                        "status": _audit_status(terminal_checks),
                        "checks": terminal_checks,
                    }
                )

            if record_phase_events:
                stage_cursor = stage_before_record + 1
                boss_event = next(
                    (
                        item
                        for item in record_phase_events
                        if str(item.get("target") or "") == BOSS_NAME
                    ),
                    {},
                )
                if isinstance(boss_event.get("hp_after"), (int, float)):
                    hp_cursor[BOSS_NAME] = int(boss_event["hp_after"])
            elif record_escape_events:
                hp_cursor[BOSS_NAME] = 0
            elif zero_hp_crossing is not None:
                hp_cursor[BOSS_NAME] = 0

    if terminal_trigger_checks:
        if len(terminal_trigger_checks) == 1:
            terminal_audit = terminal_trigger_checks[0]
        else:
            terminal_audit = {
                "status": _audit_status(terminal_trigger_checks),
                "boss_escaped": any(
                    bool(item.get("boss_escaped"))
                    for item in terminal_trigger_checks
                ),
                "reason": "multiple settled stage-0 zero-HP triggers were observed",
                "triggers": terminal_trigger_checks,
                "checks": [],
            }
    else:
        terminal_audit = {
            "status": "unknown",
            "boss_escaped": False,
            "reason": (
                "no settled authoritative stage-0 Boss HP transition from "
                "positive to zero was observed"
            ),
            "checks": [],
        }

    audited_items = [
        *roll_checks,
        *phase_checks,
        terminal_audit,
    ]
    status_counts = {
        status: sum(1 for item in audited_items if item.get("status") == status)
        for status in ("passed", "failed", "unknown")
    }
    return {
        "status": (
            "failed"
            if status_counts["failed"]
            else ("unknown" if status_counts["unknown"] else "passed")
        ),
        "summary": status_counts,
        "roll_checks": roll_checks,
        "phase_checks": phase_checks,
        "terminal_ultima_check": terminal_audit,
        "unknown_policy": (
            "Missing inputs are reported as unknown and never counted as a pass."
        ),
    }


def _receipt_source_event_id(receipt: Mapping[str, Any]) -> str:
    return str(
        dict(dict(receipt.get("result") or {}).get("source_event") or {}).get(
            "event_id"
        )
        or ""
    )


def receipt_failure_report(
    response: Mapping[str, Any],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Separate terminal receipt failures from in-transaction recoveries.

    The agent deliberately keeps failed attempts in its audit ledger.  A
    retryable failure is recovered only when a later successful receipt for
    the same tool and source event exists.  The original error remains in the
    artifact but must not abort an otherwise committed transaction.
    """

    receipts = [
        receipt
        for receipt in list(response.get("tool_receipts") or [])
        if isinstance(receipt, Mapping)
    ]
    unresolved: list[dict[str, object]] = []
    recovered: list[dict[str, object]] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping) or bool(receipt.get("ok")):
            continue
        row = {
            "tool_name": str(receipt.get("tool_name") or ""),
            "error_code": str(receipt.get("error_code") or ""),
            "message": str(receipt.get("message") or ""),
            "correction_hint": str(receipt.get("correction_hint") or ""),
        }
        tool_name = row["tool_name"]
        source_event_id = _receipt_source_event_id(receipt)
        repaired = bool(receipt.get("retryable")) and any(
            bool(later.get("ok"))
            and str(later.get("tool_name") or "") == tool_name
            and (
                not source_event_id
                or _receipt_source_event_id(later) == source_event_id
            )
            for later in receipts[index + 1 :]
        )
        if repaired:
            recovered.append({**row, "recovered_by_later_success": True})
        else:
            unresolved.append(row)
    return unresolved, recovered


def update_skill_evidence(
    matrix: dict[str, dict[str, object]],
    *,
    turn: Mapping[str, Any] | None = None,
    fixture: Mapping[str, Any] | None = None,
) -> None:
    """Attach auditable fixture/receipt evidence; never infer hidden execution."""

    if fixture is not None:
        checks = dict(fixture.get("checks") or {})
        fixture_map = {
            "防御精通": "front_defense_mastery_damage",
            "双盾战士": "front_two_shields",
            "集中心智": "focused_mind_mp",
        }
        for skill, check in fixture_map.items():
            if checks.get(check):
                evidence = list(matrix[skill].get("evidence") or [])
                evidence.append({"source": "fixture", "check": check})
                matrix[skill]["evidence"] = evidence
    if turn is not None:
        # Search only the current action's explicit fields.  Full payloads
        # contain historical combat_log/turn_board text and would make a past
        # skill name look like fresh execution.  Provisional, held and rolled
        # back actions are not evidence until their committed replay appears.
        active_skills = {
            "利刃风暴",
            "知识就是力量",
            "快速评估",
            "元素魔法",
        }
        for index, record in enumerate(
            list(turn.get("authoritative_resolutions") or [])
        ):
            if not isinstance(record, Mapping) or not bool(record.get("ok")):
                continue
            action = dict(record.get("action") or {})
            parameters = dict(action.get("parameters") or {})
            payload = dict(record.get("payload") or {})
            if (
                payload.get("check_result_provisional") is True
                or payload.get("action_uncommitted") is True
                or payload.get("held_action")
                or payload.get("team_assist_rejected") is True
            ):
                continue
            explicit_names = {
                str(parameters.get("skill_name") or "").strip(),
                str(payload.get("skill_name") or "").strip(),
            }
            trigger_sources = {
                str(item.get("source") or item.get("skill_name") or "").strip()
                for item in list(payload.get("skill_trigger_effects") or [])
                if isinstance(item, Mapping)
            }
            spell_name = str(
                parameters.get("spell_name") or payload.get("spell_name") or ""
            ).strip()
            action_type = str(
                action.get("action_type") or action.get("type") or ""
            )
            matched_skills = {
                skill for skill in active_skills if skill in trigger_sources
            }
            if action_type == "Skill":
                matched_skills.update(
                    skill for skill in active_skills if skill in explicit_names
                )
            if action_type == "Spell" and spell_name in {"元素幕障", "炎弹"}:
                matched_skills.add("元素魔法")
            for skill in matched_skills:
                row = matrix[skill]
                evidence = list(row.get("evidence") or [])
                evidence_kind = ""
                if skill == "元素魔法":
                    evidence_kind = (
                        "elemental_barrier_cast"
                        if spell_name == "元素幕障"
                        else "fire_spell_cast"
                    )
                fingerprint = {
                    "source": "authoritative_resolution",
                    "turn_id": str(turn.get("turn_id") or ""),
                    "path": f"authoritative_resolutions.{index}",
                    "action_type": str(
                        action.get("action_type") or action.get("type") or ""
                    ),
                    "skill_name": skill,
                    "spell_name": spell_name,
                }
                if evidence_kind:
                    fingerprint["kind"] = evidence_kind
                if fingerprint not in evidence:
                    evidence.append(fingerprint)
                    row["evidence"] = evidence
    for row in matrix.values():
        required_kinds = set(row.get("required_evidence_kinds") or [])
        observed_kinds = {
            str(item.get("kind") or "")
            for item in list(row.get("evidence") or [])
            if isinstance(item, Mapping)
        }
        if required_kinds:
            row["status"] = (
                "observed"
                if required_kinds.issubset(observed_kinds)
                else ("partial" if observed_kinds else "missing")
            )
        else:
            row["status"] = "observed" if row.get("evidence") else "missing"


def update_protect_evidence(
    matrix: dict[str, dict[str, object]],
    *,
    turn: Mapping[str, Any],
) -> None:
    """Require the public Skill receipt plus one authoritative redirection."""

    row = matrix["挺身守护"]
    evidence = list(row.get("evidence") or [])
    turn_id = str(turn.get("turn_id") or "")
    successful_receipts = [
        receipt
        for receipt in list(turn.get("tool_receipts") or [])
        if isinstance(receipt, Mapping) and bool(receipt.get("ok"))
    ]

    def has_kind(kind: str) -> bool:
        return any(
            isinstance(item, Mapping) and str(item.get("kind") or "") == kind
            for item in evidence
        )

    # The HTTP receipt proves that the model submitted a successful Skill
    # transaction.  The player's wording is deliberately not searched.
    for path, item in _walk(successful_receipts):
        if not isinstance(item, Mapping):
            continue
        if (
            str(item.get("action_type") or "") == "Skill"
            and str(item.get("skill_name") or "") == "挺身守护"
            and str(item.get("target") or "") == SUPPORT_HERO
        ):
            if not has_kind("successful_skill_receipt"):
                evidence.append(
                    {
                        "kind": "successful_skill_receipt",
                        "source": "turn_receipt",
                        "turn_id": turn_id,
                        "path": ".".join(path),
                    }
                )
            break

    for index, record in enumerate(
        list(turn.get("authoritative_resolutions") or [])
    ):
        if not isinstance(record, Mapping) or not bool(record.get("ok")):
            continue
        action = dict(record.get("action") or {})
        parameters = dict(action.get("parameters") or {})
        payload = dict(record.get("payload") or {})
        if (
            payload.get("check_result_provisional") is True
            or payload.get("action_uncommitted") is True
            or payload.get("held_action")
        ):
            continue
        action_type = str(action.get("action_type") or action.get("type") or "")
        source_path = f"authoritative_resolutions.{index}"

        if (
            action_type == "Skill"
            and str(parameters.get("actor") or "") == FRONT_HERO
            and str(parameters.get("target") or "") == SUPPORT_HERO
            and str(parameters.get("skill_name") or "") == "挺身守护"
            and payload.get("protect_reaction_armed") is True
            and str(payload.get("protector") or "") == FRONT_HERO
            and str(payload.get("protected_target") or "") == SUPPORT_HERO
            and payload.get("out_of_turn") is True
            and payload.get("turn_consumed") is False
        ):
            if not has_kind("armed_out_of_turn"):
                evidence.append(
                    {
                        "kind": "armed_out_of_turn",
                        "source": "authoritative_resolution",
                        "turn_id": turn_id,
                        "path": source_path,
                        "out_of_turn": True,
                        "turn_consumed": False,
                    }
                )
            continue

        npc_action_type = str(parameters.get("npc_action_type") or "")
        if not (
            action_type in {"Attack", "Spell"}
            or (
                action_type == "NPCAct"
                and npc_action_type in {"Attack", "Spell"}
            )
        ):
            continue
        if str(parameters.get("target") or "") != SUPPORT_HERO:
            continue
        if parameters.get("targets") not in (None, [], [SUPPORT_HERO]):
            continue
        roll_targets = [
            str(item.get("target") or "")
            for item in extract_rolls(payload)
            if str(item.get("target") or "")
        ]
        reaction_text = " ".join(
            [
                str(record.get("rules_text") or ""),
                str(payload.get("protect_reaction_text") or ""),
                str(payload.get("cover_text") or ""),
                " ".join(
                    str(value) for value in list(payload.get("cover_texts") or [])
                ),
            ]
        )
        redirected = bool(
            roll_targets
            and all(target == FRONT_HERO for target in roll_targets)
            and "挺身守护" in reaction_text
        )
        if redirected:
            redirect_fingerprint = _canonical_hash(
                {
                    "turn_serial": int(
                        dict(turn.get("state_before") or {}).get("turn_serial")
                        or 0
                    ),
                    "action": action,
                    "roll_targets": roll_targets,
                    "rolls": extract_rolls(payload),
                }
            )
            seen_redirects = list(row.get("redirect_fingerprints") or [])
            if redirect_fingerprint not in seen_redirects:
                seen_redirects.append(redirect_fingerprint)
                row["redirect_fingerprints"] = seen_redirects
                row["observed_redirect_count"] = len(seen_redirects)
            if not has_kind("redirected"):
                evidence.append(
                    {
                        "kind": "redirected",
                        "source": "authoritative_resolution",
                        "turn_id": turn_id,
                        "path": source_path,
                        "declared_target": SUPPORT_HERO,
                        "actual_roll_targets": roll_targets,
                    }
                )
            continue
        if (
            has_kind("redirected")
            and roll_targets
            and all(target == SUPPORT_HERO for target in roll_targets)
            and "挺身守护" not in reaction_text
            and not has_kind("not_reused")
        ):
            evidence.append(
                {
                    "kind": "not_reused",
                    "source": "authoritative_resolution",
                    "turn_id": turn_id,
                    "path": source_path,
                    "declared_target": SUPPORT_HERO,
                    "actual_roll_targets": roll_targets,
                }
            )

    row["evidence"] = evidence
    kinds = {
        str(item.get("kind") or "")
        for item in evidence
        if isinstance(item, Mapping)
    }
    required = set(row.get("required_evidence_kinds") or [])
    redirect_count = int(row.get("observed_redirect_count") or 0)
    row["status"] = (
        "invalid"
        if redirect_count > 1
        else (
            "observed"
            if required.issubset(kinds) and redirect_count == 1
            else ("partial" if kinds else "missing")
        )
    )


def update_capability_evidence(
    matrix: dict[str, dict[str, object]],
    *,
    turn: Mapping[str, Any],
) -> None:
    """Validate mandatory capabilities from authoritative rule resolutions."""

    authoritative = [
        item
        for item in list(turn.get("authoritative_resolutions") or [])
        if isinstance(item, Mapping)
        and bool(item.get("ok"))
        and dict(item.get("payload") or {}).get("check_result_provisional")
        is not True
        and dict(item.get("payload") or {}).get("action_uncommitted") is not True
        and not dict(item.get("payload") or {}).get("held_action")
    ]
    turn_id = str(turn.get("turn_id") or "")
    before = dict(turn.get("state_before") or {})
    after = dict(turn.get("state_after") or {})
    searchable_sources: list[tuple[str, Any]] = [
        ("authoritative_resolution", authoritative),
    ]

    dual_row = matrix["dual_wield"]
    for source, values in searchable_sources:
        matched_dual = False
        for path, item in _walk(values):
            if not isinstance(item, Mapping) or item.get("dual_wield") is not True:
                continue
            # The captured resolution also contains the submitted action
            # parameters.  ``dual_wield=true`` there proves only intent, not
            # that Python resolved two legal attacks.  Accept only a mechanics
            # payload carrying authoritative dual-wield result fields.
            if not any(
                key in item
                for key in (
                    "dual_wield_attacks",
                    "dual_wield_weapons",
                    "multi_attack_suppressed",
                )
            ):
                continue
            attacks = list(item.get("dual_wield_attacks") or [])
            rolls = list(item.get("rolls") or [])
            if not rolls and attacks:
                rolls = [
                    attack.get("roll")
                    for attack in attacks
                    if isinstance(attack, Mapping) and attack.get("roll") is not None
                ]
            invariants = {
                "two_weapons": len(list(item.get("dual_wield_weapons") or [])) == 2,
                "two_targets": len(list(item.get("dual_wield_targets") or [])) == 2,
                "two_attacks": len(attacks) == 2,
                "two_rolls": len(rolls) == 2,
                # RAW makes the damage High Roll zero.  A missed check may
                # retain its natural highest die for audit/display because no
                # damage is calculated; successful strikes must expose HR=0.
                "zero_high_roll": len(rolls) == 2
                and item.get("dual_wield_high_roll_override") == 0
                and all(
                    isinstance(roll, Mapping)
                    and (
                        not bool(roll.get("success"))
                        or int(roll.get("high_roll") or 0) == 0
                    )
                    for roll in rolls
                ),
                "failed_strikes_no_damage": len(rolls) == 2
                and all(
                    isinstance(roll, Mapping)
                    and (
                        bool(roll.get("success"))
                        or int(roll.get("damage") or 0) == 0
                    )
                    for roll in rolls
                ),
                "multi_attack_suppressed": item.get("multi_attack_suppressed") is True,
            }
            dual_row["evidence"] = [
                {
                    "source": source,
                    "turn_id": turn_id,
                    "path": ".".join(path),
                    "invariants": invariants,
                }
            ]
            dual_row["status"] = (
                "observed" if all(invariants.values()) else "invalid"
            )
            matched_dual = True
            break
        if matched_dual:
            break

    minor_row = matrix["minor_action"]
    teamwork_row = matrix["team_assist"]
    for index, record in enumerate(authoritative):
        action = dict(record.get("action") or {})
        parameters = dict(action.get("parameters") or {})
        payload = dict(record.get("payload") or {})
        action_type = str(action.get("action_type") or action.get("type") or "")
        source_path = f"authoritative_resolutions.{index}"

        if (
            action_type == "MinorAction"
            and str(parameters.get("actor") or "") == FRONT_HERO
            and str(parameters.get("item_name") or parameters.get("item") or "")
            == "炉心安全栓"
        ):
            story_item = dict(payload.get("story_item") or {})
            before_serial = int(before.get("turn_serial") or 0)
            after_serial = int(after.get("turn_serial") or 0)
            invariants = {
                "minor_action": payload.get("minor_action") is True,
                "interact_mode": str(
                    payload.get("minor_action_mode")
                    or parameters.get("mode")
                    or ""
                ).lower()
                in {"interact", "operate", "互动", "操作"},
                "state_changed": str(story_item.get("current_state") or "")
                == "断开辅助燃料"
                and str(
                    dict(after.get("story_item") or {}).get("state_note") or ""
                )
                == "断开辅助燃料",
                "current_actor_unchanged": before.get("actor")
                == after.get("actor")
                == FRONT_HERO,
                "owner_lifecycle_at_most_once": after_serial - before_serial
                in {0, 1},
                "no_check_roll": not extract_rolls(record),
            }
            minor_evidence = list(minor_row.get("evidence") or [])
            if not any(
                isinstance(item, Mapping)
                and item.get("kind") == "settled_without_check"
                for item in minor_evidence
            ):
                minor_evidence.append(
                    {
                        "kind": "settled_without_check",
                        "source": "authoritative_resolution",
                        "turn_id": turn_id,
                        "path": source_path,
                        "invariants": invariants,
                        "turn_serial_after": after_serial,
                    }
                )
            minor_row["evidence"] = minor_evidence

        if (
            payload.get("team_assist_registered") is True
            and payload.get("out_of_turn") is True
            and str(payload.get("supporter") or "") == SUPPORT_HERO
            and str(payload.get("leader") or "") == FRONT_HERO
        ):
            pending = dict(after.get("pending_assists") or {})
            invariants = {
                "explicit_assist_target": str(
                    parameters.get("assist_target") or ""
                )
                == FRONT_HERO,
                "explicit_assist_reason": bool(
                    str(parameters.get("reasoning") or "").strip()
                ),
                "leader_remains_current": before.get("actor")
                == after.get("actor")
                == FRONT_HERO,
                "supporter_marked_acted": SUPPORT_HERO
                in list(after.get("acted_this_round") or []),
                "assist_pending": SUPPORT_HERO
                in list(pending.get(FRONT_HERO) or []),
                "supporter_penalized": int(
                    dict(after.get("action_penalties") or {}).get(SUPPORT_HERO, 0)
                    or 0
                )
                >= 1,
                "no_check_roll": not extract_rolls(record),
            }
            evidence = list(teamwork_row.get("evidence") or [])
            if not any(
                isinstance(item, Mapping)
                and item.get("kind") == "registered_and_turn_consumed"
                for item in evidence
            ):
                evidence.append(
                    {
                        "kind": "registered_and_turn_consumed",
                        "source": "authoritative_resolution",
                        "turn_id": turn_id,
                        "path": source_path,
                        "invariants": invariants,
                    }
                )
            teamwork_row["evidence"] = evidence
            if not all(invariants.values()):
                teamwork_row["status"] = "invalid"

        teamwork = dict(payload.get("conflict_teamwork") or {})
        # Post-check window resolutions copy the committed roll payload (and
        # can additionally expose ``before_roll``), but ResolveDecision /
        # InvokeTrait / TriggerOpportunity are administrative continuations,
        # not a second check.  Count teamwork only on the authoritative
        # gameplay action record itself.  This still lets a wrong Attack
        # consumption fail the Bladestorm invariant instead of being ignored.
        is_front_check = (
            action_type in {"Attack", "Skill"}
            and str(parameters.get("actor") or "") == FRONT_HERO
            and bool(extract_rolls(payload))
        )
        if not is_front_check:
            continue

        minor_evidence = list(minor_row.get("evidence") or [])
        minor_settlement = next(
            (
                item
                for item in minor_evidence
                if isinstance(item, Mapping)
                and item.get("kind") == "settled_without_check"
            ),
            None,
        )
        if (
            minor_settlement is not None
            and str(parameters.get("skill_name") or "") == "利刃风暴"
            and before.get("actor") == FRONT_HERO
            and after.get("actor") != FRONT_HERO
            and not any(
                isinstance(item, Mapping)
                and item.get("kind") == "main_action_preserved"
                for item in minor_evidence
            )
        ):
            main_invariants = {
                "same_turn_continued": int(before.get("turn_serial") or 0)
                - int(minor_settlement.get("turn_serial_after") or 0)
                in {0, 1},
                "real_check_resolved": bool(extract_rolls(payload)),
                "actor_advanced": bool(after.get("actor"))
                and after.get("actor") != FRONT_HERO,
                "actor_marked_acted": FRONT_HERO
                in list(after.get("acted_this_round") or []),
                "single_lifecycle_advance": int(after.get("turn_serial") or 0)
                - int(before.get("turn_serial") or 0)
                in {0, 1},
            }
            minor_evidence.append(
                {
                    "kind": "main_action_preserved",
                    "source": "authoritative_resolution",
                    "turn_id": turn_id,
                    "path": source_path,
                    "skill_name": "利刃风暴",
                    "invariants": main_invariants,
                }
            )
            minor_row["evidence"] = minor_evidence
            minor_row.pop("_pending_main_action_preservation", None)
        elif (
            minor_settlement is not None
            and str(parameters.get("skill_name") or "") == "利刃风暴"
            and before.get("actor") == after.get("actor") == FRONT_HERO
            and bool(turn.get("awaiting_rule_window"))
            and not any(
                isinstance(item, Mapping)
                and item.get("kind") == "main_action_preserved"
                for item in minor_evidence
            )
        ):
            # A critical/fumble opportunity can defer only the lifecycle
            # advance after the skill's real check has already settled.  Keep
            # a probe-local correlation marker so the following window
            # resolution can prove that the earlier MinorAction preserved the
            # same main action.  This is evidence bookkeeping only; it never
            # changes the game transaction or its authoritative state.
            minor_row["_pending_main_action_preservation"] = {
                "turn_id": turn_id,
                "path": source_path,
                "skill_name": "利刃风暴",
                "turn_serial_before": int(before.get("turn_serial") or 0),
                "real_check_resolved": bool(extract_rolls(payload)),
            }
        supporters = [str(value) for value in list(teamwork.get("supporters") or [])]
        evidence = list(teamwork_row.get("evidence") or [])
        has_consumed = any(
            isinstance(item, Mapping) and item.get("kind") == "consumed_by_check"
            for item in evidence
        )
        if SUPPORT_HERO in supporters:
            consume_invariants = {
                "bladestorm_check": str(parameters.get("skill_name") or "")
                == "利刃风暴",
                "exact_supporter": supporters == [SUPPORT_HERO],
                "pending_assist_consumed": SUPPORT_HERO
                not in list(
                    dict(after.get("pending_assists") or {}).get(
                        FRONT_HERO
                    )
                    or []
                ),
            }
            consumption_fingerprint = _canonical_hash(
                {
                    "leader_turn_serial": int(before.get("turn_serial") or 0),
                    "rolls": extract_rolls(payload),
                    "supporters": supporters,
                }
            )
            seen_consumptions = list(
                teamwork_row.get("consumption_fingerprints") or []
            )
            if consumption_fingerprint not in seen_consumptions:
                seen_consumptions.append(consumption_fingerprint)
                teamwork_row["consumption_fingerprints"] = seen_consumptions
                teamwork_row["observed_consumption_count"] = len(
                    seen_consumptions
                )
            if not has_consumed:
                evidence.append(
                    {
                        "kind": "consumed_by_check",
                        "source": "authoritative_resolution",
                        "turn_id": turn_id,
                        "path": source_path,
                        "skill_name": str(parameters.get("skill_name") or ""),
                        "supporters": supporters,
                        "pending_after": dict(after.get("pending_assists") or {}),
                        "invariants": consume_invariants,
                    }
                )
        elif has_consumed and not any(
            isinstance(item, Mapping) and item.get("kind") == "not_reused"
            for item in evidence
        ):
            evidence.append(
                {
                    "kind": "not_reused",
                    "source": "authoritative_resolution",
                    "turn_id": turn_id,
                    "path": source_path,
                    "supporters": supporters,
                }
            )
        teamwork_row["evidence"] = evidence

    pending_main_action = dict(
        minor_row.get("_pending_main_action_preservation") or {}
    )
    if pending_main_action:
        deferred_resolution: tuple[int, Mapping[str, Any]] | None = next(
            (
                (index, record)
                for index, record in enumerate(authoritative)
                if dict(record.get("payload") or {}).get(
                    "resume_deferred_action"
                )
                is True
                and str(
                    dict(record.get("payload") or {}).get(
                        "deferred_action_type"
                    )
                    or ""
                )
                == "Skill"
                and str(
                    dict(record.get("payload") or {}).get(
                        "deferred_action_owner"
                    )
                    or ""
                )
                == FRONT_HERO
            ),
            None,
        )
        minor_evidence = list(minor_row.get("evidence") or [])
        minor_settlement = next(
            (
                item
                for item in minor_evidence
                if isinstance(item, Mapping)
                and item.get("kind") == "settled_without_check"
            ),
            None,
        )
        if (
            deferred_resolution is not None
            and minor_settlement is not None
            and before.get("actor") == FRONT_HERO
            and bool(after.get("actor"))
            and after.get("actor") != FRONT_HERO
            and not any(
                isinstance(item, Mapping)
                and item.get("kind") == "main_action_preserved"
                for item in minor_evidence
            )
        ):
            resolution_index, _record = deferred_resolution
            pending_serial = int(
                pending_main_action.get("turn_serial_before") or 0
            )
            main_invariants = {
                "same_turn_continued": pending_serial
                - int(minor_settlement.get("turn_serial_after") or 0)
                in {0, 1},
                "real_check_resolved": pending_main_action.get(
                    "real_check_resolved"
                )
                is True,
                "actor_advanced": after.get("actor") != FRONT_HERO,
                "actor_marked_acted": FRONT_HERO
                in list(after.get("acted_this_round") or []),
                "single_lifecycle_advance": int(
                    after.get("turn_serial") or 0
                )
                - pending_serial
                in {0, 1},
            }
            minor_evidence.append(
                {
                    "kind": "main_action_preserved",
                    "source": "deferred_action_resolution",
                    "turn_id": turn_id,
                    "path": f"authoritative_resolutions.{resolution_index}",
                    "origin_turn_id": str(
                        pending_main_action.get("turn_id") or ""
                    ),
                    "origin_path": str(
                        pending_main_action.get("path") or ""
                    ),
                    "skill_name": str(
                        pending_main_action.get("skill_name") or ""
                    ),
                    "invariants": main_invariants,
                }
            )
            minor_row["evidence"] = minor_evidence
            minor_row.pop("_pending_main_action_preservation", None)

    minor_evidence = list(minor_row.get("evidence") or [])
    minor_kinds = {
        str(item.get("kind") or "")
        for item in minor_evidence
        if isinstance(item, Mapping)
    }
    minor_required = set(minor_row.get("required_evidence_kinds") or [])
    minor_invariants_valid = all(
        all(dict(item.get("invariants") or {}).values())
        for item in minor_evidence
        if isinstance(item, Mapping) and item.get("invariants") is not None
    )
    minor_row["status"] = (
        "invalid"
        if not minor_invariants_valid
        else (
            "observed"
            if minor_required.issubset(minor_kinds)
            else ("partial" if minor_kinds else "missing")
        )
    )

    teamwork_kinds = {
        str(item.get("kind") or "")
        for item in list(teamwork_row.get("evidence") or [])
        if isinstance(item, Mapping)
    }
    teamwork_required = set(teamwork_row.get("required_evidence_kinds") or [])
    teamwork_invariants_valid = all(
        all(dict(item.get("invariants") or {}).values())
        for item in list(teamwork_row.get("evidence") or [])
        if isinstance(item, Mapping) and item.get("invariants") is not None
    )
    consumption_count = int(teamwork_row.get("observed_consumption_count") or 0)
    teamwork_row["status"] = (
        "invalid"
        if not teamwork_invariants_valid or consumption_count > 1
        else (
            "observed"
            if teamwork_required.issubset(teamwork_kinds)
            and consumption_count == 1
            else ("partial" if teamwork_kinds else "missing")
        )
    )


def summarize_turn_order(
    turns: Sequence[Mapping[str, Any]],
    *,
    boss_name: str = BOSS_NAME,
    player_heroes: Sequence[str] = (FRONT_HERO, SUPPORT_HERO),
) -> dict[str, object]:
    settled_main_action_types = {
        "Attack",
        "Spell",
        "Guard",
        "Hinder",
        "Investigate",
        "Objective",
        "Skill",
        "UseInventory",
        "TinkererGadget",
        "NPCAct",
    }

    def completed_main_action(turn: Mapping[str, Any]) -> bool:
        if not bool(turn.get("action_completed")):
            return False
        raw_resolutions = turn.get("authoritative_resolutions")
        if not isinstance(raw_resolutions, list):
            # Keep compatibility with compact/synthetic turn rows that predate
            # authoritative resolution capture.
            return True
        action_types = {
            str(action.get("action_type") or "")
            for raw_resolution in raw_resolutions
            if isinstance(raw_resolution, Mapping)
            and _is_settled_authoritative_resolution(raw_resolution)
            and isinstance((action := raw_resolution.get("action")), Mapping)
        }
        return bool(action_types.intersection(settled_main_action_types))

    completed = [
        {
            "turn_id": str(turn.get("turn_id") or ""),
            "round_before": int(turn.get("round_before") or 0),
            "actor": str(turn.get("actor_before") or ""),
            "success": completed_main_action(turn),
            "players_can_act": bool(
                dict(turn.get("state_before") or {}).get(
                    "players_can_still_act_this_round",
                    bool(
                        dict(
                            dict(turn.get("state_before") or {}).get(
                                "resolution_status"
                            )
                            or {"active_player_side": list(player_heroes)}
                        ).get("active_player_side")
                    ),
                )
            ),
        }
        for turn in turns
        if turn.get("actor_before")
    ]
    boss_by_round: dict[int, int] = {}
    for turn in turns:
        round_number = int(turn.get("round_before") or 0)
        for raw_resolution in list(turn.get("authoritative_resolutions") or []):
            if not isinstance(
                raw_resolution, Mapping
            ) or not _is_settled_authoritative_resolution(raw_resolution):
                continue
            action = raw_resolution.get("action")
            if not isinstance(action, Mapping) or str(
                action.get("action_type") or ""
            ) != "NPCAct":
                continue
            parameters = action.get("parameters")
            if not isinstance(parameters, Mapping):
                continue
            if str(parameters.get("actor") or "") != boss_name:
                continue
            boss_by_round[round_number] = boss_by_round.get(round_number, 0) + 1
    illegal_consecutive: list[dict[str, object]] = []
    successful = [row for row in completed if row["success"]]
    for left, right in zip(successful, successful[1:]):
        if (
            left["actor"] == right["actor"] == boss_name
            and bool(right["players_can_act"])
        ):
            illegal_consecutive.append(
                {"left": left["turn_id"], "right": right["turn_id"]}
            )
    three_action_rounds = sorted(
        round_number for round_number, count in boss_by_round.items() if count == 3
    )
    return {
        "completed_turns": completed,
        "boss_actions_by_round": boss_by_round,
        "boss_three_action_rounds": three_action_rounds,
        "illegal_consecutive_boss_actions": illegal_consecutive,
        "alternation_passed": not illegal_consecutive,
        "three_action_round_observed": bool(three_action_rounds),
        "players": list(player_heroes),
    }


def latency_summary(
    turns: Sequence[Mapping[str, Any]],
    provider_calls: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Compact wall/provider/cache timing for the final live report."""

    http_values = sorted(
        int(turn.get("http_wall_ms") or 0)
        for turn in turns
        if int(turn.get("http_wall_ms") or 0) >= 0
    )
    provider_values = sorted(
        int(call.get("elapsed_ms") or 0)
        for call in provider_calls
        if int(call.get("elapsed_ms") or 0) >= 0
    )

    def percentile(values: Sequence[int], fraction: float) -> int | None:
        if not values:
            return None
        index = max(
            0,
            min(len(values) - 1, math.ceil(len(values) * fraction) - 1),
        )
        return int(values[index])

    usage = _usage_summary([dict(call) for call in provider_calls])
    return {
        "http_turn_count": len(http_values),
        "http_total_ms": sum(http_values),
        "http_p50_ms": percentile(http_values, 0.50),
        "http_p90_ms": percentile(http_values, 0.90),
        "http_max_ms": max(http_values) if http_values else None,
        "provider_call_count": len(provider_values),
        "provider_total_ms": sum(provider_values),
        "provider_p50_ms": percentile(provider_values, 0.50),
        "provider_p90_ms": percentile(provider_values, 0.90),
        "provider_max_ms": max(provider_values) if provider_values else None,
        "cache": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": usage.get("cached_tokens"),
            "cache_miss_tokens": usage.get("cache_miss_tokens"),
            "cache_token_hit_rate": usage.get("cache_token_hit_rate"),
            "cache_hit_calls": usage.get("cache_hit_calls"),
            "cache_usage_reported_calls": usage.get(
                "cache_usage_reported_calls"
            ),
        },
    }


def _pending_windows(runtime: Any) -> list[Any]:
    return list(runtime.app.interceptor.decision_window_manager.pending())


def _window_response(window: Any) -> tuple[str, str]:
    owner = str(getattr(window, "owner", "") or "")
    kind = str(getattr(window, "kind", "") or "")
    if kind == "skill_parameter" and owner == SUPPORT_HERO:
        return SUPPORT_PLAYER, (
            f"{SUPPORT_HERO}使用1级快速评估，只观察{BOSS_NAME}的【boss】特质；"
            "本次只选择这一项评估。"
        )
    if kind in {"critical_opportunity", "fumble_opportunity"}:
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        return speaker, (
            f"{owner}立即放弃本次机会，不产生机会效果，也不把机会保留到稍后。"
        )
    if kind in {"trait_invocation", "bond_invocation", "lucky_seven"}:
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        return speaker, f"{owner}接受当前结果，不援用特质、羁绊或幸运数字。"
    if kind == "check_roll_confirmation":
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        return speaker, f"{owner}确认现在投骰，按已经声明的属性和难度结算。"
    if kind in {"check_confirmation", "check_result"}:
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        return speaker, f"{owner}确认按当前检定结果继续结算。"
    if kind == "npc_fate" and owner in {FRONT_HERO, SUPPORT_HERO}:
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        payload = getattr(window, "payload", {})
        target = (
            str(payload.get("target") or "这名失去战斗力的NPC").strip()
            if isinstance(payload, Mapping)
            else "这名失去战斗力的NPC"
        )
        return speaker, (
            f"{owner}决定俘虏【{target}】，不杀死它；"
            "请只提交当前NPC命运窗口中的【俘虏】选项。"
        )
    if kind == "zero_hp" and owner in {FRONT_HERO, SUPPORT_HERO}:
        speaker = FRONT_PLAYER if owner == FRONT_HERO else SUPPORT_PLAYER
        return speaker, (
            f"{owner}选择放弃抵抗，接受分离后果：被熔炉防护门隔离在战场外，"
            "不宣告死亡，也不替另一名玩家作选择。"
        )
    raise RuntimeError(f"UNEXPECTED_DECISION_WINDOW: {kind}:{owner}")


def _priority_probe_request(
    *,
    actor: str,
    natural_end_ready: bool,
    blocking: Sequence[Any],
    quick: Any | None,
) -> tuple[str, str, str, str] | None:
    """Choose hard-window, natural-end, and quick-window requests in order.

    A conflict can become mechanically ready to end in the same transaction
    that creates an ``npc_fate`` window.  Readiness does not authorize the
    probe to skip that player's still-blocking choice.  Non-blocking
    ``skill_parameter`` windows retain their existing lower priority.
    """

    if blocking:
        speaker, message = _window_response(blocking[0])
        return speaker, message, "/v1/game/turn", actor
    if natural_end_ready:
        return (
            "系统",
            (
                "权威冲突状态显示一方已经没有可行动成员。"
                "请只调用end_conflict提交自然结局，保留败北、逃脱、投降、Boss阶段和终结点的既有结算；"
                "不要补做任何攻击。"
            ),
            "/v1/game/gm-beat",
            actor,
        )
    if quick is not None:
        speaker, message = _window_response(quick)
        return speaker, message, "/v1/game/turn", actor
    return None


def required_front_setup_step(
    *,
    actor: str,
    protect_armed: bool,
    minor_kinds: set[str],
    teamwork_kinds: set[str],
) -> str:
    """Enforce MinorAction -> Assist -> Bladestorm before normal PC plans."""

    if actor != FRONT_HERO or not protect_armed:
        return ""
    if "settled_without_check" not in minor_kinds:
        return "minor_action"
    if "registered_and_turn_consumed" not in teamwork_kinds:
        return "team_assist"
    if "consumed_by_check" not in teamwork_kinds:
        return "bladestorm"
    return ""


def expected_setup_evidence_failure(
    *,
    protect_arm_request: bool,
    minor_action_request: bool,
    team_assist_request: bool,
    teamwork_check_expected: bool,
    provisional_check_waiting: bool,
    protect_kinds: set[str],
    minor_kinds: set[str],
    teamwork_kinds: set[str],
    minor_status: str,
    teamwork_status: str,
) -> dict[str, object] | None:
    """Return an immediate setup failure only after evidence can be final.

    A failed combat check can legitimately pause on a trait/bond decision
    window. Its teamwork receipt is not committed until that window is
    resolved, so absence of ``consumed_by_check`` is not yet evidence of a
    capability failure. Structurally invalid evidence still fails
    immediately; only the missing settled marker is deferred.
    """

    if protect_arm_request and not {
        "successful_skill_receipt",
        "armed_out_of_turn",
    }.issubset(protect_kinds):
        return {
            "category": "SKILL_EVIDENCE_MISSING",
            "expected": [
                "successful_skill_receipt",
                "armed_out_of_turn",
            ],
            "observed": sorted(protect_kinds),
        }
    if minor_action_request and (
        "settled_without_check" not in minor_kinds
        or minor_status == "invalid"
    ):
        return {
            "category": "CAPABILITY_EVIDENCE_MISSING",
            "expected": ["settled_without_check"],
            "observed": sorted(minor_kinds),
        }
    if team_assist_request and (
        "registered_and_turn_consumed" not in teamwork_kinds
        or teamwork_status == "invalid"
    ):
        return {
            "category": "CAPABILITY_EVIDENCE_MISSING",
            "expected": ["registered_and_turn_consumed"],
            "observed": sorted(teamwork_kinds),
        }
    if teamwork_check_expected:
        if teamwork_status == "invalid":
            return {
                "category": "CAPABILITY_EVIDENCE_MISSING",
                "expected": ["consumed_by_check"],
                "observed": sorted(teamwork_kinds),
            }
        if (
            "consumed_by_check" not in teamwork_kinds
            and not provisional_check_waiting
        ):
            return {
                "category": "CAPABILITY_EVIDENCE_MISSING",
                "expected": ["consumed_by_check"],
                "observed": sorted(teamwork_kinds),
            }
    return None


def _bladestorm_message(runtime: Any) -> str:
    minion_alive = (
        runtime.app.character_manager.exists(MINION_NAME)
        and MINION_NAME in runtime.app.conflict_manager.state.enemy_side
        and runtime.app.character_manager.get(MINION_NAME).hp > 0
        and MINION_NAME
        not in runtime.app.conflict_manager.state.defeated_combatants
    )
    if minion_alive:
        return (
            f"{FRONT_HERO}发动职业技能【利刃风暴】：用主手【符文盾】进行一次"
            f"近战攻击，并因该技能获得多重(2)，本次选择【{BOSS_NAME}】和"
            f"【{MINION_NAME}】两个不同目标。这里不是双武器攻击；副手"
            "【青铜盾】只提供防御、不参与攻击，不得设置details.dual_wield，"
            "也不得改成两次武器攻击；请按真实骰子结算这次技能攻击的两个目标。"
        )
    return (
        f"{FRONT_HERO}发动职业技能【利刃风暴】：用主手【符文盾】进行一次"
        f"近战攻击，并因该技能获得多重(2)。当前只有【{BOSS_NAME}】这一个"
        "合法目标，所以本次只选择它，不复制第二个目标。这里不是双武器攻击；"
        "副手【青铜盾】只提供防御、不参与攻击，不得设置details.dual_wield；"
        "请按真实骰子结算。"
    )


def boss_protect_trigger_message(actor: str = BOSS_NAME) -> str:
    """Request an attack on 星澜 without letting the model pre-redirect it."""

    return (
        f"执行当前敌方NPC【{actor}】的一个且仅一个完整回合："
        f"用【熔核横扫】攻击{SUPPORT_HERO}。提交给工具的 intended target/target "
        f"必须仍为【{SUPPORT_HERO}】，不得在工具参数、selected_action或叙述中"
        f"预先改成【{FRONT_HERO}】。攻击提交后，只能由Python权威的挺身守护"
        f"反应在规则结算时把实际检定/伤害目标改为【{FRONT_HERO}】；"
        "不要手工代替Python重定向，不要替下一个行动者行动，使用真实骰子结算。"
    )


def _active_hostile_names(runtime: Any) -> list[str]:
    """Return living, present enemies in authoritative turn-order order."""

    conflict = runtime.app.conflict_manager.state
    unavailable = {
        *conflict.defeated_combatants,
        *conflict.escaped_combatants,
        *conflict.surrendered_combatants,
    }
    return [
        name
        for name in conflict.enemy_side
        if name not in unavailable
        and runtime.app.character_manager.exists(name)
        and runtime.app.character_manager.get(name).hp > 0
    ]


def _planned_player_message(runtime: Any, actor: str, counters: dict[str, int]) -> str:
    index = counters.get(actor, 0)
    counters[actor] = index + 1
    active_hostiles = _active_hostile_names(runtime)
    if not active_hostiles:
        raise RuntimeError("FIXTURE_INVALID: no active hostile remains for a player action")
    primary_target = (
        BOSS_NAME if BOSS_NAME in active_hostiles else active_hostiles[0]
    )
    minion_alive = MINION_NAME in active_hostiles
    if actor == FRONT_HERO:
        if index == 0:
            return _bladestorm_message(runtime)
        if index % 4 == 3:
            return f"{FRONT_HERO}执行防御，用双盾守住自己，本回合不代替他人承受攻击。"
        return (
            f"{FRONT_HERO}执行一次单目标普通Attack：仅用主手【符文盾】攻击"
            f"唯一目标【{primary_target}】。本行动不使用双武器、不发动任何技能、"
            "不施放任何法术；不得设置details.dual_wield，不得填写skill_name，"
            "也不得添加第二个目标。请按当前主手武器面板和真实骰子结算。"
        )
    if actor == SUPPORT_HERO:
        if index == 0:
            return (
                f"{SUPPORT_HERO}施放元素幕障，选择火元素，保护{FRONT_HERO}和{SUPPORT_HERO}。"
            )
        if index == 1:
            second_target = MINION_NAME if minion_alive else primary_target
            return (
                f"{SUPPORT_HERO}进行双武器攻击：主手晨星匕首攻击{primary_target}，"
                f"副手暮影匕首攻击{second_target}。这是一个完整Attack动作；"
                "请分别进行两次真实命中检定，两击伤害高值都按0，且不得获得多重攻击。"
            )
        if index == 2:
            return (
                f"{SUPPORT_HERO}利用知识就是力量，以INS+INS开放检定分析{primary_target}的炉心结构，"
                "难度10；这是非伤害检定，必须应用知识就是力量的技能等级修正。"
            )
        if runtime.app.character_manager.get(SUPPORT_HERO).mp >= 10:
            return f"{SUPPORT_HERO}施放炎弹攻击{primary_target}，选择火焰伤害并按真实骰子结算。"
        return (
            f"{SUPPORT_HERO}这次不施法，改用双武器攻击：主手晨星匕首和副手暮影匕首"
            f"都攻击{primary_target}。这是一个完整Attack动作；请分别进行两次真实命中检定，"
            "两击伤害高值都按0，且不得获得多重攻击。"
        )
    raise RuntimeError(f"FIXTURE_INVALID: no player plan for actor {actor}")


def _state_brief(runtime: Any) -> dict[str, object]:
    conflict = runtime.app.conflict_manager.state
    combatants = {
        actor.name: {
            "hp": actor.hp,
            "max_hp": actor.max_hp,
            "mp": actor.mp,
            "max_mp": actor.max_mp,
            "guarding": actor.guarding,
            "guarded_target": actor.guarded_target,
            "defenses": dict(actor.defenses),
            "skills": dict(actor.skills),
            "spells": list(actor.spells),
            "equipment": list(actor.equipment),
            "equipped_main_hand": actor.equipped_main_hand,
            "equipped_off_hand": actor.equipped_off_hand,
            "equipped_shield": actor.equipped_shield,
            "equipped_armor": actor.equipped_armor,
            "weapon_damage": actor.weapon_damage,
            "equipment_attack_damage_bonus": actor.equipment_attack_damage_bonus,
            "affinities": _jsonable(actor.affinities),
            "temporary_affinities": _jsonable(actor.temporary_affinities),
            "equipment_affinities": _jsonable(actor.equipment_affinities),
            "statuses": _jsonable(actor.statuses),
        }
        for actor in runtime.app.character_manager.all()
        if actor.name in {FRONT_HERO, SUPPORT_HERO, BOSS_NAME, MINION_NAME}
    }
    safety_pin = runtime.app.world_state.find_story_item(name="炉心安全栓")
    resolution_status = runtime.app.conflict_manager.resolution_status()
    active_players = list(resolution_status.get("active_player_side") or [])
    return {
        "active": conflict.active,
        "round": conflict.round_number,
        "actor": conflict.current_actor(),
        "turn_serial": conflict.turn_serial,
        "queued_turns": list(conflict.queued_turns),
        "queued_turn_kinds": list(conflict.queued_turn_kinds),
        "acted_this_round": list(conflict.acted_this_round),
        "players_can_still_act_this_round": any(
            actor not in conflict.acted_this_round
            and actor in conflict.turn_order
            for actor in active_players
        ),
        "pending_assists": _jsonable(conflict.pending_assists),
        "action_penalties": dict(conflict.action_penalties),
        "current_stage": conflict.current_escalation_stage.get(BOSS_NAME, -1),
        "ultima": conflict.ultima_points.get(BOSS_NAME, 0),
        "hp": {
            name: int(details["hp"])
            for name, details in combatants.items()
        },
        "mp": {
            name: int(details["mp"])
            for name, details in combatants.items()
        },
        "combatants": combatants,
        "defeated": sorted(conflict.defeated_combatants),
        "escaped": sorted(conflict.escaped_combatants),
        "surrendered": sorted(conflict.surrendered_combatants),
        "resolution_status": _jsonable(resolution_status),
        "protect_reactions": [
            _jsonable(effect)
            for effect in conflict.active_effects
            if effect.effect_type == "protect_reaction"
        ],
        "story_item": (
            {
                "name": str(getattr(safety_pin, "name", "") or ""),
                "holder": str(getattr(safety_pin, "holder", "") or ""),
                "location": str(getattr(safety_pin, "location", "") or ""),
                "state_note": str(getattr(safety_pin, "current_state", "") or ""),
            }
            if safety_pin is not None
            else None
        ),
    }


def _call_http_turn(
    *,
    host: str,
    port: int,
    route: str,
    speaker: str,
    message: str,
    message_id: str,
    timeout: float,
) -> tuple[int, dict[str, Any], int]:
    payload: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "channel_id": CHANNEL_ID,
        "speaker": speaker,
        "message": message,
        "message_id": message_id,
        "is_at_bot": False,
    }
    return request_json(host, port, "POST", route, payload, timeout=timeout)


def make_isolated_server(service: FUGMHttpService) -> Any:
    """Bind the probe service to an ephemeral loopback port.

    ``make_server`` takes host and port as positional parameters and the
    service as a keyword-only parameter.  Keeping that adapter in one tested
    helper prevents a fixture-only API mismatch from being mistaken for a
    provider or rules failure.
    """

    return make_server("127.0.0.1", 0, service=service)


def _turn_completed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return bool(
        before.get("actor") != after.get("actor")
        or int(after.get("turn_serial") or 0) > int(before.get("turn_serial") or 0)
        or not bool(after.get("active"))
    )


def run_live(args: argparse.Namespace, output_dir: Path) -> dict[str, object]:
    source = assert_live_source(args.expected_source_root)
    resolved_output = output_dir.expanduser().resolve()
    production_root = args.production_data_root.expanduser().resolve()
    if resolved_output == production_root or production_root in resolved_output.parents:
        raise RuntimeError(
            "FIXTURE_INVALID: artifact output must not be inside production data"
        )
    config = provider_config(read_dotenv(args.dotenv))
    if config.chat_completions_url().split("/chat/completions")[0].rstrip("/") != "https://api.deepseek.com":
        raise RuntimeError("FIXTURE_INVALID: official DeepSeek endpoint preflight failed")
    if config.thinking_enabled:
        raise RuntimeError("FIXTURE_INVALID: Thinking must be disabled")
    if tuple(config.backup_api_base_urls):
        raise RuntimeError("FIXTURE_INVALID: backup providers are forbidden in this probe")

    seed = secrets.randbits(63)
    client = OpenAICompatibleClient(config)
    bundle = TestLLMClientBundle.shared(client, model=config.action_model)
    service: FUGMHttpService | None = None
    runtime: Any | None = None
    server: Any | None = None
    server_thread: threading.Thread | None = None
    turns: list[dict[str, object]] = []
    rolls: list[dict[str, object]] = []
    roll_fingerprints: set[str] = set()
    errors: list[dict[str, object]] = []
    skill_matrix = selected_skill_matrix()
    capabilities = optional_capability_matrix(args.capability)
    production_before = production_sentinel(args.production_data_root)
    fixture: dict[str, object] = {}
    persistence_checks: list[dict[str, object]] = []
    resolution_records: list[dict[str, object]] = []
    rules_audit: dict[str, object] = {
        "status": "not_run",
        "roll_checks": [],
        "phase_checks": [],
        "terminal_ultima_check": {"status": "not_run"},
    }
    provider_calls_at_start = 0
    run_started = time.monotonic()

    try:
        with tempfile.TemporaryDirectory(prefix="fu-gm-boss-probe-") as temp:
            data_root = Path(temp).resolve()
            if data_root == production_root or production_root in data_root.parents:
                raise RuntimeError("FIXTURE_INVALID: temporary data root overlaps production")
            service = FUGMHttpService(
                data_root=data_root,
                use_llm=True,
                rules_seed=seed,
                public_expression_mode="core",
                capability_routing_mode="intent",
                state_context_mode="summary_delta",
                test_llm_bundle=bundle,
            )
            runtime, fixture = build_boss_fixture(service)
            randomness = install_randomness_guards(service, runtime)
            assert_no_pending_outcome_replay(runtime)
            resolution_records = install_resolution_capture(runtime)
            roles = role_snapshot(service, runtime)
            unexpected_role_models = {
                role: model
                for role, model in roles.items()
                if str(model or "").strip()
                and str(model or "").strip() != config.action_model
            }
            if unexpected_role_models:
                raise RuntimeError(
                    "FIXTURE_INVALID: non-DeepSeek role models: "
                    + json.dumps(unexpected_role_models, ensure_ascii=False)
                )
            update_skill_evidence(skill_matrix, fixture=fixture)
            provider_calls_at_start = len(sanitized_client_calls(client))
            server = make_isolated_server(service)
            probe_port = int(server.server_address[1])
            if probe_port == PRODUCTION_PORT:
                raise RuntimeError("FIXTURE_INVALID: ephemeral server selected production port")
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            counters: dict[str, int] = {}

            for exchange in range(1, max(1, args.max_exchanges) + 1):
                before = _state_brief(runtime)
                if not bool(before["active"]):
                    break
                pending = _pending_windows(runtime)
                blocking = [window for window in pending if bool(getattr(window, "blocking", False))]
                quick = next(
                    (
                        window
                        for window in pending
                        if str(getattr(window, "kind", "") or "") == "skill_parameter"
                        and str(getattr(window, "owner", "") or "") == SUPPORT_HERO
                    ),
                    None,
                )
                actor = str(before.get("actor") or "")
                protect_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        skill_matrix["挺身守护"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                protect_armed = "armed_out_of_turn" in protect_kinds
                protect_redirected = "redirected" in protect_kinds
                protect_not_reused = "not_reused" in protect_kinds
                minor_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        capabilities["minor_action"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                teamwork_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        capabilities["team_assist"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                setup_step = required_front_setup_step(
                    actor=actor,
                    protect_armed=protect_armed,
                    minor_kinds=minor_kinds,
                    teamwork_kinds=teamwork_kinds,
                )
                protect_arm_request = False
                minor_action_request = False
                team_assist_request = False
                teamwork_check_expected = False
                natural_end_ready = bool(
                    dict(before.get("resolution_status") or {}).get(
                        "ready_for_natural_end"
                    )
                )
                priority_request = _priority_probe_request(
                    actor=actor,
                    natural_end_ready=natural_end_ready,
                    blocking=blocking,
                    quick=quick,
                )
                if priority_request is not None:
                    speaker, message, route, expected_actor = priority_request
                elif not protect_armed:
                    speaker = FRONT_PLAYER
                    message = (
                        f"{FRONT_HERO}现在发动挺身守护，准备代替{SUPPORT_HERO}承受"
                        f"{BOSS_NAME}即将发动的下一次攻击；这是冲突中的显式反应，"
                        "不消耗主要行动，并且在真正代受前不虚构伤害。"
                    )
                    route = "/v1/game/turn"
                    expected_actor = actor
                    protect_arm_request = True
                elif setup_step == "minor_action":
                    speaker = FRONT_PLAYER
                    message = (
                        f"{FRONT_HERO}在自己的第一次主要行动前，先用一次不需要检定的次要行动"
                        "简单操作大家都看得见的【炉心安全栓】，把它扳到"
                        "【断开辅助燃料】状态；她随后仍要保留本回合的主要行动。"
                    )
                    route = "/v1/game/turn"
                    expected_actor = actor
                    minor_action_request = True
                elif setup_step == "team_assist":
                    speaker = SUPPORT_PLAYER
                    message = (
                        f"{SUPPORT_HERO}现在明确把自己本轮行动投入团队协助；"
                        f"协助对象是当前行动者{FRONT_HERO}，帮助她完成接下来的"
                        "【利刃风暴】命中检定。请把协助对象明确登记为诺艾尔，并把"
                        "“协助利刃风暴命中检定”登记为协助理由；星澜不防御、"
                        "不抢走诺艾尔的当前主要行动。"
                    )
                    route = "/v1/game/turn"
                    expected_actor = actor
                    team_assist_request = True
                elif actor in {FRONT_HERO, SUPPORT_HERO}:
                    speaker = FRONT_PLAYER if actor == FRONT_HERO else SUPPORT_PLAYER
                    if setup_step == "bladestorm":
                        message = _bladestorm_message(runtime)
                        counters[FRONT_HERO] = max(
                            1, int(counters.get(FRONT_HERO, 0))
                        )
                    else:
                        message = _planned_player_message(runtime, actor, counters)
                    route = "/v1/game/turn"
                    expected_actor = actor
                    teamwork_check_expected = setup_step == "bladestorm"
                else:
                    speaker = "系统"
                    if actor == BOSS_NAME and protect_armed and not protect_redirected:
                        message = boss_protect_trigger_message(actor)
                    elif (
                        actor == BOSS_NAME
                        and protect_redirected
                        and not protect_not_reused
                    ):
                        message = (
                            f"执行当前敌方NPC【{actor}】的一个且仅一个完整回合："
                            f"再次用【熔核横扫】攻击{SUPPORT_HERO}。此前挺身守护已经实际"
                            f"代受并消费一次；由Python权威状态决定本次实际目标，"
                            "使用真实骰子结算，不得再次虚构或补发反应，不要替下一个行动者行动。"
                        )
                    else:
                        message = (
                            f"执行当前敌方NPC【{actor}】的一个且仅一个完整回合。"
                            f"优先攻击{FRONT_HERO}，但必须遵守真实检定、目标与每轮行动次数；"
                            "不要替下一个行动者行动。若当前没有实际MP损失或异常状态，"
                            "不要浪费终结点执行UltimaRecover；保留终结点按反派零HP规则结算。"
                        )
                    route = "/v1/game/gm-beat"
                    expected_actor = actor

                turn_id = f"turn-{exchange:03d}"
                call_start = len(sanitized_client_calls(client))
                resolution_start = len(resolution_records)
                status: int | None = None
                response: dict[str, Any] = {}
                elapsed_ms = 0
                caught: BaseException | None = None
                try:
                    assert_no_pending_outcome_replay(runtime)
                    status, response, elapsed_ms = _call_http_turn(
                        host="127.0.0.1",
                        port=probe_port,
                        route=route,
                        speaker=speaker,
                        message=message,
                        message_id=f"boss-probe-{exchange:03d}",
                        timeout=args.request_timeout,
                    )
                except BaseException as exc:  # artifact must survive transport failures
                    caught = exc
                try:
                    assert_no_pending_outcome_replay(runtime)
                except BaseException as exc:
                    if caught is None:
                        caught = exc
                resolution_slice = _jsonable(
                    resolution_records[resolution_start:]
                )
                after = _state_brief(runtime)
                pending_after = _pending_windows(runtime)
                provider_slice = sanitized_client_calls(client)[call_start:]
                receipt_failures, recovered_receipt_failures = (
                    receipt_failure_report(response)
                )
                repeated_rule_retry = repeated_rule_action_rejection(
                    response,
                    receipt_failures=receipt_failures,
                )
                error_category, reviewer_rejection = turn_response_error(
                    caught,
                    http_status=status,
                    response=response,
                    state_before=before,
                    requested_message=message,
                    receipt_failures=receipt_failures,
                )
                raw_action_completed = _turn_completed(before, after)
                non_main_request = bool(
                    protect_arm_request
                    or minor_action_request
                    or team_assist_request
                )
                provisional_check_waiting = bool(
                    before.get("actor") == after.get("actor")
                    and pending_after
                    and any(
                        isinstance(record, Mapping)
                        and dict(record.get("payload") or {}).get(
                            "check_result_provisional"
                        )
                        is True
                        for record in resolution_slice
                    )
                )
                action_completed = (
                    False
                    if non_main_request or provisional_check_waiting
                    else raw_action_completed
                )
                turn = {
                    "turn_id": turn_id,
                    "route": route,
                    "speaker": speaker,
                    "actor_before": expected_actor,
                    "round_before": int(before.get("round") or 0),
                    "message": message,
                    "http_status": status,
                    "http_wall_ms": elapsed_ms,
                    "ok": bool(response.get("ok")) if response else False,
                    "reply": str(response.get("reply") or ""),
                    "agent_error": str(response.get("agent_error") or ""),
                    "provider_error_category": str(
                        response.get("provider_error_category") or ""
                    ),
                    "agent_loop": _jsonable(response.get("agent_loop") or {}),
                    "tool_trace": _jsonable(response.get("tool_trace") or response.get("agent_trace") or []),
                    "tool_receipts": _jsonable(response.get("tool_receipts") or []),
                    "authoritative_resolutions": resolution_slice,
                    "receipt_failures": receipt_failures,
                    "recovered_receipt_failures": recovered_receipt_failures,
                    "model_rule_retry_stuck": repeated_rule_retry,
                    "reviewer_false_rejection": reviewer_rejection,
                    "provider_calls": provider_slice,
                    "provider_usage": _usage_summary(provider_slice),
                    "state_before": before,
                    "state_after": after,
                    "action_completed": action_completed,
                    "raw_state_transition_detected": raw_action_completed,
                    "provisional_check_waiting": provisional_check_waiting,
                    "awaiting_rule_window": bool(pending_after),
                    "probe_expectation": {
                        "protect_arm": protect_arm_request,
                        "minor_action": minor_action_request,
                        "team_assist": team_assist_request,
                        "teamwork_check": teamwork_check_expected,
                    },
                    "error_category": error_category,
                    "error": f"{type(caught).__name__}: {caught}" if caught is not None else "",
                }
                turns.append(turn)
                turn_rolls = extract_rolls(
                    {
                        "tool_receipts": turn["tool_receipts"],
                        "authoritative_resolutions": resolution_slice,
                    },
                    turn_id=turn_id,
                )
                for row in turn_rolls:
                    row["turn_serial"] = int(before.get("turn_serial") or 0)
                append_unique_rolls(
                    rolls,
                    turn_rolls,
                    seen=roll_fingerprints,
                )
                update_skill_evidence(skill_matrix, turn=turn)
                update_protect_evidence(skill_matrix, turn=turn)
                update_capability_evidence(capabilities, turn=turn)
                if error_category:
                    errors.append(
                        {
                            "turn_id": turn_id,
                            "category": error_category,
                            "http_status": status,
                            "detail": (
                                turn["error"]
                                or repeated_rule_retry
                                or receipt_failures
                                or reviewer_rejection
                                or str(
                                    response.get("agent_error")
                                    or response.get("error")
                                    or response.get("provider_error_category")
                                    or dict(response.get("agent_loop") or {}).get(
                                        "terminal_reason"
                                    )
                                    or ""
                                )
                            ),
                        }
                    )
                    break
                expected_evidence_failure: dict[str, object] | None = None
                current_protect_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        skill_matrix["挺身守护"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                current_minor_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        capabilities["minor_action"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                current_teamwork_kinds = {
                    str(item.get("kind") or "")
                    for item in list(
                        capabilities["team_assist"].get("evidence") or []
                    )
                    if isinstance(item, Mapping)
                }
                expected_evidence_failure = expected_setup_evidence_failure(
                    protect_arm_request=protect_arm_request,
                    minor_action_request=minor_action_request,
                    team_assist_request=team_assist_request,
                    teamwork_check_expected=teamwork_check_expected,
                    provisional_check_waiting=provisional_check_waiting,
                    protect_kinds=current_protect_kinds,
                    minor_kinds=current_minor_kinds,
                    teamwork_kinds=current_teamwork_kinds,
                    minor_status=str(
                        capabilities["minor_action"].get("status") or ""
                    ),
                    teamwork_status=str(
                        capabilities["team_assist"].get("status") or ""
                    ),
                )
                if expected_evidence_failure is not None:
                    errors.append(
                        {
                            "turn_id": turn_id,
                            **expected_evidence_failure,
                            "detail": (
                                "The requested natural-language capability did not "
                                "produce its authoritative Python evidence."
                            ),
                        }
                    )
                    break
                action_progressed = bool(
                    action_completed
                    or pending_after
                    or extract_rolls(resolution_slice)
                )
                if (
                    quick is None
                    and not blocking
                    and not non_main_request
                    and not action_progressed
                ):
                    errors.append(
                        {
                            "turn_id": turn_id,
                            "category": "MODEL_TOOL_MISSING",
                            "detail": "The expected combat actor did not complete an action.",
                        }
                    )
                    break
                stage_before = int(before.get("current_stage", -1))
                stage_after = int(after.get("current_stage", -1))
                if stage_after != stage_before:
                    persistence = verify_persistence(data_root, CAMPAIGN_ID, runtime)
                    persistence["checkpoint"] = f"boss_phase_{stage_after}"
                    persistence_checks.append(persistence)
                    if not persistence["matched"]:
                        errors.append(
                            {
                                "turn_id": turn_id,
                                "category": "PERSISTENCE_MISMATCH",
                                "detail": persistence,
                            }
                        )
                        break
            else:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "TURN_LIMIT_EXCEEDED",
                        "detail": f"Conflict still active after {args.max_exchanges} exchanges.",
                    }
                )

            final_persistence = verify_persistence(data_root, CAMPAIGN_ID, runtime)
            final_persistence["checkpoint"] = "final"
            persistence_checks.append(final_persistence)
            if not final_persistence["matched"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "PERSISTENCE_MISMATCH",
                        "detail": final_persistence,
                    }
                )
            production_after = production_sentinel(args.production_data_root)
            production_compare = compare_production_sentinels(
                production_before, production_after
            )
            if not production_compare["unchanged"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "PRODUCTION_SENTINEL_DRIFT",
                        "detail": production_compare["interpretation"],
                    }
                )

            order = summarize_turn_order(turns)
            final_state = _state_brief(runtime)
            rules_audit = audit_authoritative_rules(turns)
            if rules_audit.get("status") == "failed":
                errors.append(
                    {
                        "turn_id": "",
                        "category": "BOSS_RULE_INVARIANT_FAILED",
                        "detail": {
                            "invariant": "authoritative roll/damage/HP/phase reconciliation",
                            "summary": rules_audit.get("summary"),
                            "note": (
                                "Unknown entries remain explicit and are not failures; "
                                "this error is present only because at least one check mismatched."
                            ),
                        },
                    }
                )
            phase_transition_turns = [
                str(turn.get("turn_id") or "")
                for turn in turns
                if int(
                    dict(turn.get("state_after") or {}).get("current_stage", -1)
                )
                > int(
                    dict(turn.get("state_before") or {}).get("current_stage", -1)
                )
            ]
            final_resolution = dict(final_state.get("resolution_status") or {})
            boss_outcome = {
                "phase_transition_observed": bool(phase_transition_turns),
                "phase_transition_turns": phase_transition_turns,
                "final_stage": int(final_state.get("current_stage", -1)),
                "final_ultima": int(final_state.get("ultima") or 0),
                "boss_defeated": BOSS_NAME in set(final_state.get("defeated") or []),
                "boss_escaped": BOSS_NAME in set(final_state.get("escaped") or []),
                "boss_surrendered": BOSS_NAME in set(final_state.get("surrendered") or []),
                "conflict_active": bool(final_state.get("active")),
                "natural_outcome": str(final_resolution.get("natural_outcome") or ""),
                "terminal_ultima_audit": rules_audit.get(
                    "terminal_ultima_check"
                ),
            }
            boss_outcome["terminal_outcome_observed"] = bool(
                not boss_outcome["conflict_active"]
                and (
                    boss_outcome["boss_defeated"]
                    or boss_outcome["boss_escaped"]
                    or boss_outcome["boss_surrendered"]
                    or boss_outcome["natural_outcome"]
                    in {"hostile_side_removed", "player_side_removed", "no_active_sides", "inactive"}
                )
            )
            if order["illegal_consecutive_boss_actions"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "BOSS_RULE_INVARIANT_FAILED",
                        "detail": {
                            "invariant": "boss actions must alternate while PCs can act",
                            "violations": order["illegal_consecutive_boss_actions"],
                        },
                    }
                )
            if not order["three_action_round_observed"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "RANDOM_COVERAGE_INCONCLUSIVE",
                        "detail": "No complete round with exactly three settled Boss actions was observed.",
                    }
                )
            if not boss_outcome["phase_transition_observed"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "RANDOM_COVERAGE_INCONCLUSIVE",
                        "detail": "The real-random battle ended or stopped before the configured Boss phase transition.",
                    }
                )
            if not boss_outcome["terminal_outcome_observed"]:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "RANDOM_COVERAGE_INCONCLUSIVE",
                        "detail": "No persisted terminal Boss/conflict outcome was observed.",
                    }
                )
            missing_skills = [
                name
                for name, row in skill_matrix.items()
                if bool(row.get("required")) and row.get("status") != "observed"
            ]
            if missing_skills:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "SKILL_EVIDENCE_MISSING",
                        "detail": missing_skills,
                    }
                )
            missing_capabilities = [
                name
                for name, row in capabilities.items()
                if bool(row.get("required")) and row.get("status") != "observed"
            ]
            if missing_capabilities:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "CAPABILITY_EVIDENCE_MISSING",
                        "detail": missing_capabilities,
                    }
                )
            successes = sum(1 for row in rolls if row.get("success") is True)
            failures = sum(1 for row in rolls if row.get("success") is False)
            if not rolls or not successes or not failures:
                errors.append(
                    {
                        "turn_id": "",
                        "category": "RANDOM_COVERAGE_INCONCLUSIVE",
                        "detail": {
                            "roll_count": len(rolls),
                            "successes": successes,
                            "failures": failures,
                            "note": "The run is retained as-is; no seed or result is selected away.",
                        },
                    }
                )

            all_calls = sanitized_client_calls(client)[provider_calls_at_start:]
            fatal_categories = {
                "PROVIDER_TIMEOUT",
                "PROVIDER_EMPTY_RESPONSE",
                "PROVIDER_HTTP",
                "PROVIDER_SCHEMA",
                "MODEL_TOOL_MISSING",
                "MODEL_TOOL_REJECTED",
                "MODEL_RULE_RETRY_STUCK",
                "REVIEWER_FALSE_REJECTION",
                "RULE_RECEIPT_ERROR",
                "FIXTURE_INVALID",
                "PERSISTENCE_MISMATCH",
                "PRODUCTION_SENTINEL_DRIFT",
                "SKILL_EVIDENCE_MISSING",
                "CAPABILITY_EVIDENCE_MISSING",
                "BOSS_RULE_INVARIANT_FAILED",
                "TURN_LIMIT_EXCEEDED",
                "UNEXPECTED_DECISION_WINDOW",
                "INTERNAL_ERROR",
            }
            result = {
                "mode": "live",
                "status": "failed" if any(item["category"] in fatal_categories for item in errors) else ("inconclusive" if errors else "passed"),
                "started_at": utc_now(),
                "wall_ms": int((time.monotonic() - run_started) * 1000),
                "source": source,
                "provider": {
                    "official": True,
                    "endpoint": config.chat_completions_url(),
                    "model": config.action_model,
                    "thinking_enabled": config.thinking_enabled,
                    "backup_provider_count": len(config.backup_api_base_urls),
                    "roles": roles,
                    "all_nonempty_roles_share_official_model": True,
                    "usage": _usage_summary(all_calls),
                    "calls": all_calls,
                },
                "latency": latency_summary(turns, all_calls),
                "isolation": {
                    "temporary_data_root": True,
                    "temporary_data_root_removed_after_run": True,
                    "loopback_host": "127.0.0.1",
                    "ephemeral_port": probe_port,
                    "production_port_used_for_gameplay": False,
                    "production_port": PRODUCTION_PORT,
                },
                "randomness": {
                    "seed": seed,
                    "seed_source": "secrets.randbits(63)",
                    "seed_cli_override_available": False,
                    "selected_or_retried_for_outcome": False,
                    "guards": randomness,
                },
                "fixture": fixture,
                "skill_matrix": skill_matrix,
                "capability_matrix": capabilities,
                "turn_order": order,
                "boss_outcome": boss_outcome,
                "roll_summary": {
                    "count": len(rolls),
                    "successes": successes,
                    "failures": failures,
                    "critical_successes": sum(1 for row in rolls if row.get("critical_success") is True),
                    "fumbles": sum(1 for row in rolls if row.get("fumble") is True),
                },
                "rules_audit": rules_audit,
                "persistence": persistence_checks,
                "production_sentinel": {
                    "before": production_before,
                    "after": production_after,
                    "comparison": production_compare,
                },
                "errors": errors,
                "artifact_counts": {"turns": len(turns), "rolls": len(rolls)},
            }
            _write_artifacts(
                output_dir,
                summary=result,
                turns=turns,
                rolls=rolls,
                skill_matrix=skill_matrix,
                capabilities=capabilities,
                rules_audit=rules_audit,
                api_key=config.api_key,
            )
            return result
    except BaseException as exc:
        category = classify_error(exc)
        errors.append(
            {
                "turn_id": str(turns[-1].get("turn_id") or "") if turns else "",
                "category": category,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
        try:
            production_after = production_sentinel(args.production_data_root)
            production_compare = compare_production_sentinels(
                production_before, production_after
            )
        except Exception as sentinel_error:
            production_after = {"error": type(sentinel_error).__name__}
            production_compare = {"unchanged": False, "error": str(sentinel_error)}
        partial_calls = sanitized_client_calls(client)[provider_calls_at_start:]
        failure = {
            "mode": "live",
            "status": "failed",
            "wall_ms": int((time.monotonic() - run_started) * 1000),
            "provider": {
                "official": True,
                "endpoint": config.chat_completions_url(),
                "model": config.action_model,
                "thinking_enabled": config.thinking_enabled,
                "usage": _usage_summary(partial_calls),
                "calls": partial_calls,
            },
            "latency": latency_summary(turns, partial_calls),
            "randomness": {
                "seed": seed,
                "seed_source": "secrets.randbits(63)",
                "seed_cli_override_available": False,
                "selected_or_retried_for_outcome": False,
            },
            "fixture": fixture,
            "skill_matrix": skill_matrix,
            "capability_matrix": capabilities,
            "rules_audit": audit_authoritative_rules(turns) if turns else rules_audit,
            "production_sentinel": {
                "before": production_before,
                "after": production_after,
                "comparison": production_compare,
            },
            "errors": errors,
            "exception": {
                "category": category,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=12),
            },
            "artifact_counts": {"turns": len(turns), "rolls": len(rolls)},
        }
        _write_artifacts(
            output_dir,
            summary=failure,
            turns=turns,
            rolls=rolls,
            skill_matrix=skill_matrix,
            capabilities=capabilities,
            rules_audit=(
                audit_authoritative_rules(turns) if turns else rules_audit
            ),
            api_key=config.api_key,
        )
        return failure
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=3.0)
        if service is not None:
            _shutdown_background_workers(service, runtime)
        _close_client(client)


def _write_artifacts(
    output_dir: Path,
    *,
    summary: Mapping[str, Any],
    turns: Sequence[dict[str, object]],
    rolls: Sequence[dict[str, object]],
    skill_matrix: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    rules_audit: Mapping[str, Any],
    api_key: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    paths = {
        "turns": output_dir / "turns.jsonl",
        "rolls": output_dir / "rolls.jsonl",
        "skill_matrix": output_dir / "skill_matrix.json",
        "capability_matrix": output_dir / "capability_matrix.json",
        "rules_audit": output_dir / "rules_audit.json",
        "summary": output_dir / "summary.json",
    }
    _write_jsonl_secure(paths["turns"], turns)
    _write_jsonl_secure(paths["rolls"], rolls)
    _write_json_secure(paths["skill_matrix"], skill_matrix)
    _write_json_secure(paths["capability_matrix"], capabilities)
    _write_json_secure(paths["rules_audit"], rules_audit)
    _write_json_secure(paths["summary"], summary)
    scan = _secret_scan(paths.values(), api_key=api_key)
    if not scan["passed"]:
        raise RuntimeError("INTERNAL_ERROR: artifact secret scan failed")
    _write_json_secure(output_dir / "secret_scan.json", scan)


def run_offline(args: argparse.Namespace, output_dir: Path) -> dict[str, object]:
    seed = secrets.randbits(63)
    skill_matrix = selected_skill_matrix()
    capabilities = optional_capability_matrix(args.capability)
    with tempfile.TemporaryDirectory(prefix="fu-gm-boss-fixture-") as temp:
        service = FUGMHttpService(
            data_root=Path(temp), use_llm=False, rules_seed=seed
        )
        runtime, fixture = build_boss_fixture(service)
        guards = install_randomness_guards(service, runtime)
        update_skill_evidence(skill_matrix, fixture=fixture)
        persistence = verify_persistence(Path(temp), CAMPAIGN_ID, runtime)
    summary = {
        "mode": "offline_fixture_validation",
        "status": "passed" if persistence["matched"] else "failed",
        "live_requests_allowed": False,
        "provider_call_count": 0,
        "randomness": {
            "seed": seed,
            "seed_source": "secrets.randbits(63)",
            "seed_cli_override_available": False,
            "guards": guards,
        },
        "fixture": fixture,
        "skill_matrix": skill_matrix,
        "capability_matrix": capabilities,
        "rules_audit": {
            "status": "not_run",
            "reason": "offline fixture validation performs no combat rolls",
            "roll_checks": [],
            "phase_checks": [],
            "terminal_ultima_check": {"status": "not_run"},
        },
        "persistence": persistence,
        "note": "Active-skill rows remain missing until an explicit --live run returns authoritative receipts.",
        "errors": [] if persistence["matched"] else [{"category": "PERSISTENCE_MISMATCH"}],
    }
    _write_artifacts(
        output_dir,
        summary=summary,
        turns=[],
        rolls=[],
        skill_matrix=skill_matrix,
        capabilities=capabilities,
        rules_audit=summary["rules_audit"],
        api_key="",
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_root.expanduser().resolve() / f"deepseek_boss_{stamp}"
    try:
        summary = run_live(args, output_dir) if args.live else run_offline(args, output_dir)
    except BaseException as exc:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        failure = {
            "mode": "live" if args.live else "offline_fixture_validation",
            "status": "failed",
            "error_category": classify_error(exc),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=12),
            "output_dir": str(output_dir),
        }
        _write_json_secure(output_dir / "summary.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": summary.get("status"),
                "mode": summary.get("mode"),
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary.get("status") in {"passed", "inconclusive"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
